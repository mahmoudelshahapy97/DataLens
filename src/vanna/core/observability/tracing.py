"""Ergonomic span helpers.

Instrumenting by hand produces this at every call site::

    span = None
    if self.observability_provider:
        span = await self.observability_provider.create_span("x", attributes={...})
    result = await do_work()
    if self.observability_provider and span:
        span.set_attribute("ok", True)
        await self.observability_provider.end_span(span)
        if span.duration_ms():
            await self.observability_provider.record_metric("x.duration", ...)

Repeated ~20 times it buries the control flow, and it is easy to get subtly
wrong: an early ``return`` or a raised exception skips ``end_span`` and leaks
the span, while a ``finally`` added later can end it twice.

:func:`traced` collapses all of that to::

    async with traced(provider, "x", attr=value) as span:
        result = await do_work()
        span.set_attribute("ok", True)

Spans are ended exactly once, on every exit path including exceptions, and the
duration metric is recorded automatically. When no provider is configured the
context manager yields a null span that accepts calls and does nothing, so
instrumented code needs no ``if provider`` guards at all.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from .base import ObservabilityProvider
from .models import Span

logger = logging.getLogger(__name__)


class NullSpan(Span):
    """A span that goes nowhere.

    Yielded when no provider is configured so callers can always treat the
    context variable as a real span.
    """

    def __init__(self, name: str = "null") -> None:
        super().__init__(name=name)

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D102
        return None


@asynccontextmanager
async def traced(
    provider: Optional[ObservabilityProvider],
    name: str,
    *,
    record_duration: bool = True,
    metric_tags: Optional[Dict[str, str]] = None,
    **attributes: Any,
) -> AsyncIterator[Span]:
    """Trace a block of work.

    Args:
        provider: Observability provider, or None to disable tracing.
        name: Span name. Also the metric name, suffixed with ``.duration``.
        record_duration: Emit a duration metric when the span ends.
        metric_tags: Tags for the duration metric. Span *attributes* describe
            one operation; metric *tags* are aggregation dimensions and must
            stay low-cardinality -- never put a user id or a query here.
        **attributes: Span attributes.

    Yields:
        The span, or a :class:`NullSpan` when no provider is configured.

    On exception the span is annotated with ``error`` and ``error_type`` and
    then ended before the exception propagates, so failures are traced rather
    than silently dropped.

    Telemetry failures never propagate: a broken metrics backend must not take
    down request handling.
    """
    if provider is None:
        yield NullSpan(name)
        return

    span: Optional[Span] = None
    try:
        span = await provider.create_span(name, attributes=dict(attributes))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to create span %r: %s", name, e)
        yield NullSpan(name)
        return

    try:
        yield span
    except Exception as e:
        try:
            span.set_attribute("error", str(e))
            span.set_attribute("error_type", type(e).__name__)
        except Exception:  # pragma: no cover - defensive
            pass
        raise
    finally:
        try:
            await provider.end_span(span)
            if record_duration:
                duration = span.duration_ms()
                if duration is not None:
                    await provider.record_metric(
                        f"{name}.duration", duration, "ms", tags=metric_tags or {}
                    )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to end span %r: %s", name, e)


async def record_metric(
    provider: Optional[ObservabilityProvider],
    name: str,
    value: float,
    unit: str = "count",
    tags: Optional[Dict[str, str]] = None,
) -> None:
    """Record a metric, tolerating a missing or failing provider."""
    if provider is None:
        return
    try:
        await provider.record_metric(name, value, unit, tags=tags or {})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to record metric %r: %s", name, e)
