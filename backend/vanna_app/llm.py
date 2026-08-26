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
import contextvars
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
    # Added because this deployment answers with gpt-5 and the column stayed
    # NULL: the table is matched by longest prefix, so the dated snapshot
    # `gpt-5-2025-08-07` resolves through `gpt-5`.
    #
    # These are list prices. A contracted rate is different, and a cost figure
    # that is quietly wrong is worse than one that is quietly absent -- so
    # override them rather than trusting them (see `_prices_from_env`).
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5": (1.25, 10.00),
}


def _prices_from_env() -> None:
    """Merge ``VANNA_LLM_PRICES`` over the table above.

    A JSON object of ``{"model-prefix": [input, output]}`` in USD per million
    tokens. Exists because the numbers above are list prices and the figure this
    feeds -- the spend shown to an operator -- has to match the actual bill. A
    deployment on negotiated rates can correct it without a code change, and a
    new model can be priced without waiting for one.

    Malformed input is logged and ignored: a bad price should not stop the
    process from answering questions.
    """
    import json
    import os

    raw = os.environ.get("VANNA_LLM_PRICES", "").strip()
    if not raw:
        return
    try:
        for prefix, pair in json.loads(raw).items():
            PRICES[str(prefix).lower()] = (float(pair[0]), float(pair[1]))
    except Exception as exc:  # noqa: BLE001 - a price list must not break startup
        logger.error("Ignoring VANNA_LLM_PRICES (%s): %s", type(exc).__name__, exc)
        return
    logger.info("LLM prices overridden from VANNA_LLM_PRICES")


_prices_from_env()


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


#: Usage for the request currently being served, accumulated across LLM turns.
#:
#: The middleware cannot write it to the generation row itself, for two reasons
#: that only show up in production:
#:
#: * **Order.** The LLM answers *before* the agent calls ``run_sql``, and the row
#:   is written by that tool. An UPDATE from here runs against a row that does
#:   not exist yet and reports zero rows changed.
#: * **Key.** The row is keyed by ``ToolContext.request_id`` -- a per-agent-run
#:   UUID -- while this layer only knows the ASGI request id from
#:   ``observability``. They are different values, so even a well-timed UPDATE
#:   matched nothing.
#:
#: Hence the same primitive ``_CURRENT_QUESTION`` uses in ``platform``: one task
#: per request, so the value this sets is visible to that request's tools and to
#: no other. Accumulated rather than replaced, because one question is several
#: LLM turns and the cost is their sum.
_CURRENT_USAGE: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "vanna_current_llm_usage", default=None
)


def note_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Add one LLM turn to this request's running total."""
    total = _CURRENT_USAGE.get() or {
        "model": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    # Last model wins: a request that escalated mid-flight is attributed to the
    # model that actually produced the answer.
    total = {
        "model": model or total["model"],
        "prompt_tokens": total["prompt_tokens"] + max(prompt_tokens, 0),
        "completion_tokens": total["completion_tokens"] + max(completion_tokens, 0),
    }
    _CURRENT_USAGE.set(total)


def consume_usage() -> Optional[Dict[str, Any]]:
    """This request's total so far, with its cost, or None if nothing was metered.

    Left in place rather than cleared: one question can write more than one
    generation row (a repair attempt, a follow-up query in the same turn), and
    each should carry the cost of the request it belongs to.
    """
    total = _CURRENT_USAGE.get()
    if not total:
        return None
    return {
        **total,
        "cost_usd": price_of(
            total["model"], total["prompt_tokens"], total["completion_tokens"]
        ),
    }


def reset_usage() -> None:
    """Start a fresh total. Called when a new question begins."""
    _CURRENT_USAGE.set(None)


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
        # Streamed responses are reassembled by the agent, which has no `model`
        # field to put it in -- so it travels in `metadata`. Checked here rather
        # than assumed, because the non-streaming path does set the attribute.
        model = _first(response, ("model", "model_name")) or ""
        if not model:
            metadata = getattr(response, "metadata", None) or {}
            model = str(metadata.get("model") or "")
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

        # The primary path: hand the numbers to the tool that writes the row.
        note_usage(model, prompt, completion)

        # And still try the UPDATE, for a row that already exists -- a follow-up
        # turn in a conversation whose generation was recorded a moment ago.
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
