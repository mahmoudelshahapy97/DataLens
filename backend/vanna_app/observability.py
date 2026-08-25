"""Logging, request correlation and metrics.

The original had informative log lines at the right seams and nothing else: no
request ids, no structured output, no metrics, and an ``ObservabilityProvider``
interface in the library that nothing implemented. Everything that went wrong in
production had to be diagnosed by grepping unstructured text with no way to tie a
user's report to the lines that describe their request.

Three pieces:

**Correlation.** An ASGI middleware assigns an ``X-Request-Id`` (or adopts the one a
proxy already set) and puts it, with the tenant and user, into ``contextvars``. Every
log record picks them up automatically, so filtering a day of logs down to one
person's afternoon is a field match rather than an archaeology exercise.

**Structured output.** ``VANNA_LOG_FORMAT=json`` emits one JSON object per line.
Text stays the default because a human tailing ``docker compose logs`` should not
have to read JSON, and a log shipper should not have to parse prose.

**Metrics.** A Prometheus endpoint when ``prometheus_client`` is installed, and a
no-op shim when it is not, so the import never becomes a hard dependency of a
deployment that does not scrape anything.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("vanna.observability")

# ----------------------------------------------------------------------
# Correlation
# ----------------------------------------------------------------------

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("vanna_request_id", default="")
tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("vanna_tenant_id", default="")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("vanna_user_id", default="")

#: Header carrying the id. Read as well as written, so a request traced through a
#: gateway keeps one id end to end.
REQUEST_ID_HEADER = "x-request-id"


def current_request_id() -> str:
    return request_id_var.get()


def bind_identity(tenant_id: str = "", user_id: str = "") -> None:
    """Attach the resolved identity to this request's log context.

    Called once identity is known -- which is after the middleware has run, because
    resolving it needs a database round trip that must not sit in the middleware.
    """
    if tenant_id:
        tenant_id_var.set(tenant_id)
    if user_id:
        user_id_var.set(user_id)


class ContextFilter(logging.Filter):
    """Copies the context variables onto every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.tenant_id = tenant_id_var.get()
        record.user_id = user_id_var.get()
        return True


class ProbeFilter(logging.Filter):
    """Drops access-log lines for the liveness and readiness probes.

    Docker health-checks /health every 30 seconds, per worker, forever. That is two
    lines a minute of pure noise in the one place somebody looks when something is
    wrong -- and it pushes the line they actually need off the screen. nginx already
    sets `access_log off` for the same paths; this is the other half.

    Only the *access* log is filtered. A probe that fails still logs through the
    application logger, which is the part worth keeping.
    """

    #: Matched against the request line, so `/health` filters and `/healthz-ish`
    #: does not.
    PATHS = (' /health ', ' /ready ', ' /metrics ')

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        message = record.getMessage()
        return not any(path in message for path in self.PATHS)


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Deliberately hand-rolled rather than pulling in a logging library: the shape is
    eight fields and a dict, and a dependency whose only job is ``json.dumps`` is a
    dependency to keep updated for no benefit.
    """

    #: Attributes ``logging`` puts on every record. Anything *not* here was added by
    #: the caller via ``extra=`` and is worth emitting.
    _STANDARD = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("request_id", "tenant_id", "user_id"):
            value = getattr(record, field, "")
            if value:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in self._STANDARD and key not in payload:
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install the formatter and the context filter on the root logger."""
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.addFilter(ProbeFilter())

    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)-22s [%(request_id)s] %(message)s"
            )
        )

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers; let ours own the output so every line has
    # the same shape and the same correlation fields.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv = logging.getLogger(name)
        uv.handlers[:] = []
        uv.propagate = True

    logging.getLogger("vanna").setLevel(level)


# ----------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------


class _NoopMetric:
    """Stands in when ``prometheus_client`` is absent, so call sites need no guard."""

    def labels(self, *_args: Any, **_kwargs: Any) -> "_NoopMetric":
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def observe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class Metrics:
    """The metric set, or a set of no-ops.

    Cardinality is the thing to be careful about here: ``tenant_id`` is a label on
    the metrics where per-customer numbers are the point (questions, cost) and
    deliberately *not* on request latency, where it would multiply every bucket by
    the number of workspaces.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = False
        self.requests = _NoopMetric()
        self.request_seconds = _NoopMetric()
        self.questions = _NoopMetric()
        self.llm_seconds = _NoopMetric()
        self.llm_tokens = _NoopMetric()
        self.llm_cost = _NoopMetric()
        self.sql_seconds = _NoopMetric()
        self.limit_rejections = _NoopMetric()
        self.login_failures = _NoopMetric()
        self.tenant_runtimes = _NoopMetric()
        self.pool_waiters = _NoopMetric()
        self.pool_in_use = _NoopMetric()
        self.pool_ceiling = _NoopMetric()
        self.pool_wait_seconds = _NoopMetric()
        self.pool_saturated = _NoopMetric()
        self.warehouse_pool_in_use = _NoopMetric()
        self.warehouse_pool_ceiling = _NoopMetric()
        self.loop_lag_seconds = _NoopMetric()

        if not enabled:
            return
        try:
            from prometheus_client import Counter, Gauge, Histogram
        except ImportError:
            logger.info("prometheus_client is not installed; /metrics will be empty.")
            return

        self.enabled = True
        self.requests = Counter(
            "vanna_http_requests_total", "HTTP requests", ["method", "route", "status"]
        )
        self.request_seconds = Histogram(
            "vanna_http_request_seconds", "HTTP request duration", ["method", "route"]
        )
        self.questions = Counter(
            "vanna_questions_total", "Questions asked", ["tenant", "status"]
        )
        self.llm_seconds = Histogram(
            "vanna_llm_seconds", "LLM call duration", ["model"]
        )
        self.llm_tokens = Counter(
            "vanna_llm_tokens_total", "Tokens consumed", ["tenant", "model", "kind"]
        )
        self.llm_cost = Counter(
            "vanna_llm_cost_usd_total", "LLM spend in USD", ["tenant", "model"]
        )
        self.sql_seconds = Histogram(
            "vanna_sql_seconds", "Warehouse query duration", ["dialect"]
        )
        self.limit_rejections = Counter(
            "vanna_limit_rejections_total", "Requests refused by a limit",
            ["tenant", "limit"],
        )
        self.login_failures = Counter(
            "vanna_login_failures_total", "Failed sign-in attempts", ["reason"]
        )
        self.tenant_runtimes = Gauge(
            "vanna_tenant_runtimes", "Tenant runtimes currently cached"
        )
        # Connection accounting.
        #
        # `pool_waiters` has existed since metrics were added and was never set by
        # anything, so the one gauge that would have shown control-plane saturation
        # read zero however saturated it was. It is wired in `db.py` now, alongside
        # the four below.
        #
        # The distinction these keep separate is the whole point: `ceiling` is what
        # the configuration permits, `in_use` is what is happening. Reporting only
        # one of them is how a deployment ends up arguing about whether it is close
        # to a limit.
        self.pool_waiters = Gauge(
            "vanna_control_plane_pool_waiters", "Requests waiting for a control-plane connection"
        )
        self.pool_in_use = Gauge(
            "vanna_control_plane_pool_in_use", "Control-plane connections checked out"
        )
        self.pool_ceiling = Gauge(
            "vanna_control_plane_pool_ceiling", "Control-plane connections this worker may open"
        )
        self.pool_wait_seconds = Histogram(
            "vanna_control_plane_pool_wait_seconds",
            "Time spent waiting for a control-plane connection",
            # Deliberately fine at the short end: the interesting question is
            # whether waiting has started at all, not how long a saturated pool
            # keeps someone. Anything past a second is already a problem.
            buckets=(0.001, 0.005, 0.02, 0.1, 0.5, 1, 5, 10),
        )
        self.pool_saturated = Counter(
            "vanna_control_plane_pool_saturated_total",
            "Requests that gave up waiting for a control-plane connection",
        )
        # Labelled by data source, which is bounded by what a deployment has
        # registered -- unlike `tenant`, which the class docstring warns about.
        self.warehouse_pool_in_use = Gauge(
            "vanna_warehouse_pool_in_use", "Warehouse connections checked out", ["data_source"]
        )
        self.warehouse_pool_ceiling = Gauge(
            "vanna_warehouse_pool_ceiling", "Warehouse connections a runtime may open",
            ["data_source"],
        )
        # The number that says whether blocking work is starving other users. A
        # p99 request latency can be bad for a dozen reasons; loop lag can only be
        # one.
        self.loop_lag_seconds = Histogram(
            "vanna_event_loop_lag_seconds",
            "How late the event loop ran a callback scheduled for now",
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 5),
        )


_metrics: Optional[Metrics] = None


def get_metrics() -> Metrics:
    global _metrics
    if _metrics is None:
        _metrics = Metrics(enabled=False)
    return _metrics


def configure_metrics(enabled: bool) -> Metrics:
    global _metrics
    _metrics = Metrics(enabled=enabled)
    return _metrics


# ----------------------------------------------------------------------
# Middleware
# ----------------------------------------------------------------------


class RequestContextMiddleware:
    """Pure-ASGI middleware: assigns a request id, times the request, records it.

    ASGI rather than ``BaseHTTPMiddleware`` on purpose. Starlette's base class wraps
    the response in an anyio task group, which buffers streaming responses -- and
    streaming *is* the product here. This touches the scope and the response start
    message and nothing else.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        incoming = headers.get(REQUEST_ID_HEADER, "").strip()
        # Adopt an upstream id, but only if it looks like one: an unbounded
        # attacker-controlled string ends up in every log line otherwise.
        request_id = incoming[:64] if incoming.isascii() and 8 <= len(incoming) <= 64 else uuid.uuid4().hex[:16]

        request_id_var.set(request_id)
        tenant_id_var.set("")
        user_id_var.set("")

        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        metrics = get_metrics()
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", [])
                message["headers"].append(
                    (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            # The templated path, not the concrete one: `/tenants/{id}` keeps the
            # label set bounded where `/tenants/acme` would grow it per workspace.
            route = scope.get("route")
            label = getattr(route, "path", None) or scope.get("path", "unknown")
            method = scope.get("method", "?")
            metrics.requests.labels(method, label, str(status_holder["status"])).inc()
            metrics.request_seconds.labels(method, label).observe(elapsed)

            if elapsed > 5.0:
                logger.warning(
                    "Slow request: %s %s took %.1fs", method, label, elapsed,
                    extra={"duration_seconds": round(elapsed, 3)},
                )


def register_metrics_endpoint(app: Any) -> None:
    """Expose ``/metrics`` when the client library is present."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return

    from fastapi import Response

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def configure_sentry(dsn: str, environment: str) -> None:
    """Optional error reporting. Absent DSN or package is a no-op."""
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("VANNA_SENTRY_DSN is set but sentry-sdk is not installed.")
        return
    sentry_sdk.init(dsn=dsn, environment=environment, traces_sample_rate=0.1)
    logger.info("Sentry error reporting enabled (%s)", environment)
