"""Putting business vocabulary in front of the model.

A schema says ``invoice.total numeric``. It does not say that total is revenue,
that revenue excludes cancelled invoices, or that "churn" means no order in
ninety days. Those are the facts that decide whether a syntactically perfect
query answers the question that was asked, and they exist only where somebody
wrote them down.

This is the last mile for :mod:`vanna_app.domain_store`: the domains an
administrator curates are appended to the system prompt as a short glossary.

Worth stating what it does *not* do, because the reference implementation this
was adapted from got it wrong in an instructive way. There, domains are modelled,
stored, given an API and a UI -- and the one call that would thread a domain into
the query path passes ``domain_id=None``, unconditionally, so the fields reserved
for the domain name and description are ``None`` on every request in production.
The feature exists everywhere except in the prompt. A decorator is used here
precisely so there is no such gap: if the enhancer is wired, the text is in the
prompt, and the test asserts on the prompt rather than on the store.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from vanna.core.enhancer import LlmContextEnhancer

logger = logging.getLogger("vanna.domains.prompt")

#: A glossary is worth little if it crowds out the schema. Domains are curation,
#: and curation that displaces the table definitions makes answers worse.
MAX_CHARS = 2_000


def _estimate_tokens(text: str) -> int:
    """The same character heuristic the budget assembler falls back to.

    Only used to report the glossary's size on the preview screen, so being a
    few tokens out is harmless -- being absent from the screen was not.
    """
    from vanna.core.enhancer.budget import estimate_tokens

    return estimate_tokens(text)


class DomainContextEnhancer(LlmContextEnhancer):
    """Appends the workspace's business domains to the system prompt.

    ``inner`` runs first, so retrieval context comes before the glossary.

    Every enabled domain contributes, rather than one selected per request: a
    term the question never uses costs a few tokens, while dropping a domain on
    a missed match costs an accuracy regression. What the question *does* use
    decides the order -- domains it touches first, and within each the terms it
    used first -- so when the glossary outgrows :data:`MAX_CHARS` it is the
    unrelated vocabulary that is cut, not whatever sorts last alphabetically.
    """

    def __init__(
        self,
        store: Any,
        *,
        tenant_id: str,
        data_source_id: str,
        inner: Optional[LlmContextEnhancer] = None,
    ) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.data_source_id = data_source_id
        self.inner = inner

    async def enhance_system_prompt(
        self, system_prompt: str, user_message: str, user: Any
    ) -> str:
        if self.inner is not None:
            system_prompt = await self.inner.enhance_system_prompt(
                system_prompt, user_message, user
            )

        try:
            domains = await self.store.list_domains(
                self.tenant_id, data_source_id=self.data_source_id
            )
        except Exception as exc:
            # A prompt without the glossary still answers the question; a raised
            # exception answers nothing. Nothing here is a permission check --
            # membership is intersected with grants elsewhere, and the SQL policy
            # is what actually refuses a table -- so degrading is safe.
            logger.debug("Business domains not described: %s", exc)
            return system_prompt

        section = self._render(domains, user_message)
        if not section:
            return system_prompt
        return f"{system_prompt}\n\n## Business domains\n\n{section}"

    async def enhance_user_message(self, message: str, user: Any, **kwargs: Any) -> str:
        if self.inner is not None and hasattr(self.inner, "enhance_user_message"):
            return await self.inner.enhance_user_message(message, user, **kwargs)
        return message

    def __getattr__(self, name: str) -> Any:
        """Pass anything else through to the wrapped enhancer.

        Same shape as ``ValueResolvingEnhancer``, and load-bearing for the same
        reason: ``/prompt-preview`` looks for ``build_context`` on whatever is
        outermost. Without this it found a decorator that did not have it and
        answered 503 -- the preview screen broke the moment this enhancer was
        added, which is how the omission was noticed.
        """
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    async def build_context(self, user_message: str, user: Any) -> Any:
        """Assemble the inner context, with the glossary added as a section.

        Forwarding to ``inner.build_context`` unchanged would be easier and would
        make the preview lie: the endpoint's whole promise is "not a
        reconstruction -- this is what would be sent", and a preview missing a
        section that the real prompt contains is worse than no preview, because it
        is trusted.
        """
        inner = self.__dict__.get("inner")
        if inner is None or not hasattr(inner, "build_context"):
            raise AttributeError("build_context")

        result = await inner.build_context(user_message, user)

        try:
            domains = await self.store.list_domains(
                self.tenant_id, data_source_id=self.data_source_id
            )
        except Exception as exc:
            logger.debug("Business domains not previewed: %s", exc)
            return result

        section = self._render(domains, user_message)
        if not section or result is None:
            return result

        # `AssemblyResult` carries the finished text and a per-section token
        # count -- not a list of sections to append to. Adding to a `sections`
        # attribute looked right and did nothing at all, because there is no such
        # attribute; the preview simply kept omitting the glossary. Mirror what
        # `enhance_system_prompt` does to the prompt instead.
        block = f"## Business domains\n\n{section}"
        counter = getattr(self.inner, "count_tokens", None) or _estimate_tokens
        try:
            tokens = int(counter(block))
        except Exception:
            tokens = _estimate_tokens(block)

        result.text = f"{result.text}\n\n{block}" if result.text else block
        result.tokens_used = getattr(result, "tokens_used", 0) + tokens
        section_tokens = getattr(result, "section_tokens", None)
        if section_tokens is not None:
            section_tokens["domains"] = tokens
        return result

    @staticmethod
    def _render(domains: list, question: str = "") -> str:
        from .knowledge_links import match_domains

        matched = {id(d): terms for d, terms in match_domains(domains, question)}
        ordered = [d for d in domains if id(d) in matched] + [
            d for d in domains if id(d) not in matched
        ]

        lines: list = []
        for domain in ordered:
            if not domain.get("is_enabled"):
                continue
            used = matched.get(id(domain)) or []

            head = f"**{domain['name']}**"
            if domain.get("description"):
                head = f"{head} -- {domain['description']}"
            lines.append(head)

            tables = domain.get("tables") or []
            if tables:
                # Named, not described: the schema section already carries the
                # columns, and repeating them here would spend the budget twice
                # to say the same thing.
                lines.append(f"  Tables: {', '.join(sorted(tables))}")

            glossary = domain.get("terminology") or {}
            for term in used + sorted(t for t in glossary if t not in used):
                lines.append(f"  {term}: {glossary[term]}")

            lines.append("")

        text = "\n".join(lines).strip()
        if len(text) > MAX_CHARS:
            # Truncate at a domain boundary rather than mid-definition: half a
            # definition is worse than none, because the model will use it.
            kept: list = []
            budget = MAX_CHARS
            for block in text.split("\n\n"):
                if len(block) + 2 > budget:
                    break
                kept.append(block)
                budget -= len(block) + 2
            text = "\n\n".join(kept)
        return text
