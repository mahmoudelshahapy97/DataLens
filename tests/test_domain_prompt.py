"""The business-domain glossary, as the model actually receives it.

Split from ``test_domains.py`` because none of this needs a database: the store is
faked, and what is asserted is the rendered prompt. That separation is the point.
The reference implementation this was adapted from models domains, stores them,
exposes an API and builds a UI -- then calls its context builder with
``domain_id=None`` unconditionally, so no domain has ever appeared in one of its
prompts in production. Every test it has still passes, because they all assert on
the store.

So these assert on the string that goes to the model.
"""

from __future__ import annotations

import pytest


class FakeStore:
    """Only what the enhancer uses."""

    def __init__(self, domains):
        self.domains = domains

    async def list_domains(self, tenant_id, *, data_source_id=None):
        return self.domains


class Inner:
    async def enhance_system_prompt(self, prompt, message, user):
        return f"{prompt}\n\n## Database schema\n\n(tables)"


class TestItReachesTheModel:
    """Asserted on the prompt, because that is the thing that was missing."""

    DOMAIN = {
        "name": "Revenue",
        "description": "Invoices and what they earned.",
        "terminology": {"churn": "no invoice in 90 days"},
        "tables": ["sales.invoice"],
        "is_enabled": True,
    }

    async def render(self, domains):
        from vanna_app.domain_prompt import DomainContextEnhancer

        enhancer = DomainContextEnhancer(
            FakeStore(domains), tenant_id="acme", data_source_id="db1", inner=Inner()
        )
        return await enhancer.enhance_system_prompt("You are an analyst.", "q", None)

    async def test_the_glossary_is_in_the_prompt(self):
        prompt = await self.render([self.DOMAIN])
        assert "## Business domains" in prompt
        assert "**Revenue**" in prompt
        assert "churn: no invoice in 90 days" in prompt
        assert "sales.invoice" in prompt

    async def test_it_comes_after_the_schema(self):
        """A glossary that displaces table definitions makes answers worse."""
        prompt = await self.render([self.DOMAIN])
        assert prompt.index("## Database schema") < prompt.index("## Business domains")

    async def test_a_disabled_domain_is_not_described(self):
        prompt = await self.render([{**self.DOMAIN, "is_enabled": False}])
        assert "## Business domains" not in prompt

    async def test_no_domains_means_no_section(self):
        prompt = await self.render([])
        assert "## Business domains" not in prompt

    async def test_a_store_failure_does_not_break_the_prompt(self):
        """A prompt without the glossary still answers; an exception answers nothing."""
        from vanna_app.domain_prompt import DomainContextEnhancer

        class Broken:
            async def list_domains(self, *a, **k):
                raise RuntimeError("control plane down")

        enhancer = DomainContextEnhancer(
            Broken(), tenant_id="acme", data_source_id="db1", inner=Inner()
        )
        prompt = await enhancer.enhance_system_prompt("You are an analyst.", "q", None)
        assert "## Database schema" in prompt
        assert "## Business domains" not in prompt

    async def test_the_glossary_is_capped(self):
        """Curation must not crowd out the schema."""
        from vanna_app.domain_prompt import MAX_CHARS

        many = [
            {
                "name": f"Domain{i}",
                "description": "x" * 200,
                "terminology": {},
                "tables": [],
                "is_enabled": True,
            }
            for i in range(50)
        ]
        prompt = await self.render(many)
        section = prompt.split("## Business domains", 1)[1]
        assert len(section) <= MAX_CHARS + 64
