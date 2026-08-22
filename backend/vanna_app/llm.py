"""Choosing the LLM provider, and metering what it costs.

Provider selection is unchanged: ``auto`` picks whichever key is present and falls
back to a mock service so the stack runs with zero configuration.

What is new is the metering. ``generations`` has always had ``model``,
``prompt_tokens``, ``completion_tokens`` and ``cost_usd`` columns, and nothing ever
wrote to them -- so the one number that decides pricing, cost per workspace, was
uncollectable. A middleware sits on the LLM call, reads the usage the provider
already returns, and hands it to the generation store keyed by request id.

Prices are a table in code, not a lookup service. They change rarely, they should
change in a reviewed diff, and a wrong number here produces a wrong report rather
than a failed request -- so an unknown model records tokens and no cost, which is
visibly incomplete instead of silently wrong.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from vanna.core.middleware import LlmMiddleware

from .observability import current_request_id, get_metrics

logger = logging.getLogger("vanna.llm")


def build_llm_service(settings: Any) -> Any:
    """Pick an LLM provider from the configuration."""
    provider = settings.llm_provider

    if provider == "auto":
        import os

        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "mock"

    if provider == "anthropic":
        from vanna.integrations.anthropic import AnthropicLlmService

        import os

        logger.info("LLM provider: Anthropic (%s)", os.getenv("ANTHROPIC_MODEL", "default"))
        return AnthropicLlmService()

    if provider == "openai":
        from vanna.integrations.openai import OpenAILlmService

        logger.info("LLM provider: OpenAI")
        return OpenAILlmService()

    from vanna.integrations.mock import MockLlmService

    if settings.llm_provider == "mock":
        # Asked for by name: tests and offline demos want this and do not want to be
        # told off for it.
        logger.info("LLM provider: mock (requested explicitly)")
    elif not settings.is_demo:
        # In demo mode falling back is expected. Anywhere else it means a deployment
        # is answering questions with canned text and nobody has noticed.
        logger.error(
            "No LLM API key found and VANNA_LLM_PROVIDER is %r -- falling back to "
            "the mock service. Answers will be canned. Set ANTHROPIC_API_KEY or "
            "OPENAI_API_KEY.",
            settings.llm_provider,
        )
    else:
        logger.warning(
            "No LLM API key found -- using the mock service. Set ANTHROPIC_API_KEY "
            "or OPENAI_API_KEY for real answers."
        )
    return MockLlmService()


# ----------------------------------------------------------------------
# Cost
# ----------------------------------------------------------------------

#: USD per million tokens, as (input, output). Matched by longest prefix so a dated
#: model id resolves to its family without an entry per snapshot.
#:
#: Deliberately incomplete rather than guessed: a model absent from this table
#: records its token counts with no cost, so the gap shows up in a report instead of
#: being papered over with a plausible number.
PRICES: Dict[str, Tuple[float, float]] = {
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o3-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
}


def price_of(model: Optional[str], prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    """What this call cost, or None when the model's price is unknown."""
    if not model:
        return None
    name = model.lower()
    match = max(
        (prefix for prefix in PRICES if name.startswith(prefix)), key=len, default=""
    )
    if not match:
        logger.debug("No price for model %r; recording tokens only.", model)
        return None
    prompt_price, completion_price = PRICES[match]
    return (prompt_tokens * prompt_price + completion_tokens * completion_price) / 1_000_000


class UsageMeteringMiddleware(LlmMiddleware):
    """Records model, tokens and cost for every LLM call.

    Subclasses the library's ``LlmMiddleware`` rather than duck-typing it. An earlier
    version implemented ``before_request`` / ``after_response`` from memory; the real
    interface is ``before_llm_request`` / ``after_llm_response``, so the agent raised
    ``AttributeError`` on the first message and every question failed with "an
    unexpected error occurred". Inheriting means a wrong name is a missing override
    rather than a runtime surprise -- the base class supplies working defaults.

    Tolerates providers that report usage differently by reading several spellings
    and giving up quietly on the rest: a metering layer that raises on an unfamiliar
    response object would take the product down to protect a statistic.
    """

    def __init__(self, generation_store: Any = None) -> None:
        self.generation_store = generation_store
        self._started: Dict[str, float] = {}

    async def before_llm_request(self, request: Any) -> Any:
        self._started[current_request_id() or "-"] = time.perf_counter()
        return request

    async def after_llm_response(self, request: Any, response: Any) -> Any:
        try:
            await self._meter(response)
        except Exception as exc:  # noqa: BLE001 - never fail a request over a metric
            logger.debug("Could not meter LLM usage: %s", type(exc).__name__)
        return response

    async def _meter(self, response: Any) -> None:
        model = _first(response, ("model", "model_name")) or ""
        usage = _first(response, ("usage", "token_usage", "_usage")) or {}

        prompt = int(_usage_field(usage, ("prompt_tokens", "input_tokens")) or 0)
        completion = int(_usage_field(usage, ("completion_tokens", "output_tokens")) or 0)
        if not (prompt or completion):
            return

        cost = price_of(model, prompt, completion)
        request_id = current_request_id()
        started = self._started.pop(request_id or "-", None)

        metrics = get_metrics()
        if started is not None:
            metrics.llm_seconds.labels(model or "unknown").observe(time.perf_counter() - started)

        from .observability import tenant_id_var

        tenant = tenant_id_var.get() or "unknown"
        metrics.llm_tokens.labels(tenant, model or "unknown", "prompt").inc(prompt)
        metrics.llm_tokens.labels(tenant, model or "unknown", "completion").inc(completion)
        if cost is not None:
            metrics.llm_cost.labels(tenant, model or "unknown").inc(cost)

        if self.generation_store is not None and request_id:
            await self.generation_store.attach_usage(
                request_id,
                tenant,
                model=model or None,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=cost,
            )


def _first(obj: Any, names: Tuple[str, ...]) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name)
        if value is not None:
            return value
    return None


def _usage_field(usage: Any, names: Tuple[str, ...]) -> Any:
    return _first(usage, names)
