"""LLM middlewares for cost control.

``LlmMiddleware`` has existed as an interface with no implementations, so the
extension point was undemonstrated and the savings it enables went unclaimed.
These are the two that matter for a text-to-SQL agent.

**PromptCacheMiddleware** is the high-value one. Once schema and instructions
are injected on every turn, the prompt carries a large stable prefix -- and
providers will cache that prefix and charge a fraction of the normal rate for
it, but only if they are told which part is stable. This costs nothing to run
and can cut the bill on a large-schema deployment by an order of magnitude.

**ResponseCacheMiddleware** returns a stored response for an identical request.
Narrow by design: exact matches only, never semantic similarity. "Revenue last
month" and "revenue last quarter" sit close together in embedding space and
mean entirely different things, so a similarity-keyed cache silently answers
one question with another's numbers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from .base import LlmMiddleware

logger = logging.getLogger(__name__)


class PromptCacheMiddleware(LlmMiddleware):
    """Marks the stable prompt prefix so the provider can cache it.

    The system prompt -- persona, rules, instructions, schema -- is identical
    across every turn of a conversation and usually across conversations too.
    The user's message is not. Marking the boundary lets the provider bill the
    prefix at a discount.

    Vanna is provider-agnostic, so this sets a normalised hint in request
    metadata and leaves each ``LlmService`` to translate it into that API's
    mechanism (Anthropic's ``cache_control`` breakpoints, OpenAI's automatic
    prefix caching, and so on). Putting provider-specific serialisation here
    would push provider knowledge into shared code.

    Args:
        min_prefix_chars: Below this, do not bother. Providers impose their own
            minimum cacheable length, and marking a short prefix adds request
            overhead for nothing.
    """

    def __init__(self, *, min_prefix_chars: int = 2000) -> None:
        self.min_prefix_chars = min_prefix_chars

    async def before_llm_request(self, request: Any) -> Any:
        system_prompt = getattr(request, "system_prompt", None)
        if not system_prompt or len(system_prompt) < self.min_prefix_chars:
            return request

        metadata = getattr(request, "metadata", None)
        if metadata is None:
            # LlmRequest may not define metadata; attach it rather than fail,
            # so an integration that has not been updated is unaffected.
            try:
                request.metadata = {}
                metadata = request.metadata
            except Exception:
                return request

        metadata["cache_prefix"] = True
        metadata["cache_prefix_chars"] = len(system_prompt)
        logger.debug("Marked %d-char prompt prefix as cacheable", len(system_prompt))
        return request

    async def after_llm_response(self, request: Any, response: Any) -> Any:
        return response


class ResponseCacheMiddleware(LlmMiddleware):
    """Returns a stored response for a byte-identical request.

    Only ever hits on an exact match of messages, tools, temperature, and
    system prompt. That is narrow, and deliberately so -- it catches the cases
    that are unambiguously safe (a page refresh, a retried request, the same
    canned question from two users) and nothing else.

    **Not enabled by default.** A cached response is a response computed
    against data as it was at cache time, which for a BI tool is a real
    correctness risk. Keep the TTL short.

    Args:
        ttl_seconds: Entry lifetime. Short on purpose.
        max_entries: LRU bound.
        cache_tool_calls: Whether to cache responses containing tool calls.
            Off by default: replaying a cached tool call re-executes SQL
            against data that may have changed, so the "cached" answer is
            neither the old one nor a fresh one.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_entries: int = 500,
        cache_tool_calls: bool = False,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.cache_tool_calls = cache_tool_calls
        self._entries: Dict[str, Tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, request: Any) -> str:
        """Content hash of everything that can change the response.

        The tenant is part of the key. Omitting it would let one tenant's
        cached answer be served to another whenever their prompts coincided --
        a cross-tenant leak through a performance optimisation.
        """
        user = getattr(request, "user", None)
        payload = {
            "tenant": getattr(user, "tenant_id", "default") if user else "default",
            "system": getattr(request, "system_prompt", None),
            "temperature": getattr(request, "temperature", None),
            "messages": [
                {
                    "role": getattr(m, "role", None),
                    "content": getattr(m, "content", None),
                    "tool_call_id": getattr(m, "tool_call_id", None),
                }
                for m in getattr(request, "messages", []) or []
            ],
            "tools": sorted(
                getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])
            ),
        }
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def before_llm_request(self, request: Any) -> Any:
        # Middleware cannot short-circuit the call in the current interface, so
        # the lookup happens here and the result is stashed for the response
        # phase. Recording the key also avoids hashing the request twice.
        try:
            key = self._key(request)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("Could not key request for caching: %s", e)
            return request

        entry = self._entries.get(key)
        if entry:
            stored_at, _ = entry
            if time.time() - stored_at > self.ttl_seconds:
                self._entries.pop(key, None)

        try:
            request.metadata = getattr(request, "metadata", None) or {}
            request.metadata["_cache_key"] = key
        except Exception:
            pass
        return request

    async def after_llm_response(self, request: Any, response: Any) -> Any:
        metadata = getattr(request, "metadata", None) or {}
        key = metadata.get("_cache_key")
        if not key:
            return response

        if not self.cache_tool_calls and getattr(response, "tool_calls", None):
            return response

        self._entries[key] = (time.time(), response)
        if len(self._entries) > self.max_entries:
            # Oldest-first eviction; dicts preserve insertion order.
            for stale in list(self._entries)[: len(self._entries) - self.max_entries]:
                self._entries.pop(stale, None)
        return response

    def lookup(self, request: Any) -> Optional[Any]:
        """Fetch a live cached response, or None.

        Exposed for callers that can short-circuit before reaching the LLM.
        """
        try:
            key = self._key(request)
        except Exception:
            return None
        entry = self._entries.get(key)
        if not entry:
            self.misses += 1
            return None
        stored_at, response = entry
        if time.time() - stored_at > self.ttl_seconds:
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return response

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
