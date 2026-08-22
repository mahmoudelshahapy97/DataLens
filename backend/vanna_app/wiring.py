"""The composition root: everything is assembled here and nowhere else.

Reading this file top to bottom tells you what the deployment is. Nothing below
constructs its own dependencies, so there is one place to look when the question is
"where does this come from".

Startup order matters and is deliberate:

1. **Configuration is validated first.** ``load_and_validate`` raises with every
   problem listed, so a dangerous deployment never reaches the point of serving. The
   old behaviour -- permissive defaults, a warning in the log, and a running server
   -- is what turned an unset variable into a cross-tenant breach.
2. **The control plane is opened, and a failure is fatal** unless anonymous access
   was explicitly chosen. This used to be caught and swallowed, after which the
   whole application ran unauthenticated.
3. **Migrations run** under an advisory lock, so several replicas booting together
   is fine.
4. Only then are the stores, the platform and the routes built.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from . import __version__
from .locks import KEY_SEED, once

logger = logging.getLogger("vanna.wiring")


def create_app(settings: Optional[Any] = None) -> Any:
    """Build the FastAPI application served by uvicorn."""
    from fastapi import FastAPI

    from .config import get_settings
    from .observability import (
        configure_logging,
        configure_metrics,
        configure_sentry,
        register_metrics_endpoint,
    )

    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    configure_metrics(settings.metrics_enabled)
    configure_sentry(settings.sentry_dsn, settings.mode)

    services = _build_services(settings)

    app = FastAPI(
        title="Vanna",
        description="Natural-language querying over your database, per workspace",
        # Read, not repeated. `vanna_app.__version__` exists to be the one place
        # this is written down -- it even exports it in __all__ -- and a literal
        # here meant /health, /docs and the package constant were three copies free
        # to disagree. Nothing read the constant at all.
        version=__version__,
        lifespan=_lifespan(settings, services),
    )

    # Exposed so operational code -- the readiness probe's own test, a management
    # command, a debugger -- can reach the constructed services without rebuilding
    # them from the environment and getting a second, different set.
    app.state.services = services

    _install_middleware(app, settings)
    _register_routes(app, settings, services)
    register_metrics_endpoint(app)
    _register_probes(app, settings, services)

    return app


# ----------------------------------------------------------------------
# Services
# ----------------------------------------------------------------------


def _warn_on_newly_reachable_grant_roles(app_db: Any) -> None:
    """Name any grant that was inert and is now live.

    A caller's ``group_memberships`` used to be ``["user"]`` for everyone who was
    not an admin, so a grant row for ``analyst`` or ``viewer`` was accepted,
    stored, and matched nobody. The role is now part of that list, which is the
    point -- but it means such a row starts granting access on this boot, and an
    operator should hear about it from the log rather than from a user.

    Nothing wrote these rows automatically, so on almost every deployment this
    finds nothing and says nothing.
    """
    from .authz import ROLES
    from .db import SCHEMA

    try:
        rows = app_db.run_sync(
            f"SELECT tenant_id, data_source_id, role, count(*) AS n "
            f"FROM {SCHEMA}.table_grants WHERE lower(role) <> ALL(%s) "
            "GROUP BY tenant_id, data_source_id, role",
            (["user", "admin"],),
            fetch="all",
        )
    except Exception as exc:  # pragma: no cover - advisory only
        logger.debug("Could not audit grant roles: %s", exc)
        return

    for row in rows or []:
        role = str(row["role"])
        known = " (a known role)" if role in ROLES else " -- not a known role"
        logger.warning(
            "Grant role %r%s now resolves for real callers: %s row(s) on %s/%s. "
            "It matched nobody before this version.",
            role, known, row["n"], row["tenant_id"], row["data_source_id"],
        )


def _build_services(settings: Any) -> Dict[str, Any]:
    """Open the control plane and construct everything that hangs off it."""
    from .accounts import Accounts
    from .audit import AdminAudit, NullAdminAudit, PostgresAuditLogger
    from .billing import Billing
    from .db import build_app_database
    from .identity import build_user_resolver
    from .limits import Counters, LoginThrottle
    from .mail import build_mailer
    from .migrate import upgrade
    from .oidc import build_oidc_client
    from .platform import Platform
    from .secrets import Cipher
    from .grant_policy import GrantPolicyStore
    from .catalog_store import PostgresSchemaCatalog
    from .datasources import DataSourceRegistry
    from .domain_store import DomainStore
    from .grants_store import PostgresGrantStore
    from .instruction_library import InstructionLibrary
    from .instruction_store import PostgresInstructionStore
    from .stores import (
        PostgresConversationStore,
        PostgresDashboardStore,
        PostgresGenerationStore,
    )
    from .write_store import PostgresWriteApprovalStore
    from .tenancy import Directory

    # Raises unless the deployment has explicitly opted out of having one.
    app_db = build_app_database(settings)

    if app_db is not None and settings.auto_migrate:
        upgrade(app_db)
    elif app_db is not None:
        from .migrate import status

        report = status(app_db)
        if not report["up_to_date"]:
            raise RuntimeError(
                f"The schema is at version {report['current_version']:04d} but "
                f"{len(report['pending'])} migration(s) are pending: "
                f"{', '.join(report['pending'])}. Run `python -m vanna_app.migrate "
                "upgrade`, or set VANNA_AUTO_MIGRATE=true."
            )

    cipher = Cipher(settings.secret_key)
    directory = Directory(app_db, cipher) if app_db else None
    accounts = Accounts(app_db) if app_db else None
    billing = Billing(app_db) if app_db else None
    counters = Counters(app_db) if app_db else None
    generations = PostgresGenerationStore(app_db) if app_db else None
    conversations = PostgresConversationStore(app_db) if app_db else None
    # A factory, not a store: the catalog needs the retrieval index, which Platform
    # resolves. Falls back to the JSON catalog when there is no control plane, which
    # is the demo/no-database path.
    catalog_factory = (
        (lambda index: PostgresSchemaCatalog(app_db, index=index)) if app_db else None
    )
    # Both are None without a control plane, and a None grant store means
    # nothing is writable -- which is the right answer for a deployment that
    # has nowhere durable to record who granted what.
    grants = PostgresGrantStore(app_db) if app_db else None
    domain_store = DomainStore(app_db) if app_db else None
    datasources = DataSourceRegistry(app_db, cipher) if app_db else None
    write_approvals = PostgresWriteApprovalStore(app_db) if app_db else None
    instructions = PostgresInstructionStore(app_db) if app_db else None
    grant_policies = GrantPolicyStore(app_db) if app_db else None
    # Loaded once, at boot, and validated strictly: malformed baseline content
    # that starts the process and quietly applies nothing is the failure the
    # baseline exists to prevent.
    instruction_library = InstructionLibrary.load()
    dashboards = PostgresDashboardStore(directory) if directory else None
    agent_audit = PostgresAuditLogger(app_db) if app_db else None
    admin_audit = AdminAudit(app_db) if app_db else NullAdminAudit()

    if app_db is not None:
        _warn_on_newly_reachable_grant_roles(app_db)

    resolver = build_user_resolver(settings, directory, accounts)

    platform = Platform(
        settings,
        directory=directory,
        generation_store=generations,
        conversation_store=conversations,
        dashboard_store=dashboards,
        counters=counters,
        audit_logger=agent_audit,
        admin_audit=admin_audit,
        billing=billing,
        grant_store=grants,
        write_approval_store=write_approvals,
        instruction_store=instructions,
        instruction_library=instruction_library,
        grant_policy_store=grant_policies,
        catalog_factory=catalog_factory,
        domain_store=domain_store,
        datasource_registry=datasources,
    )
    # The agents are built lazily and each needs the resolver, so it is attached to
    # the platform rather than threaded through every call site.
    platform.user_resolver = resolver

    # Metering, installed before any runtime is built: agents bind their middleware
    # list at construction, so attaching this afterwards would meter nothing until
    # the next eviction. It is what finally writes the model, token and cost columns
    # `generations` has had since the beginning.
    from .llm import UsageMeteringMiddleware

    platform.usage_middleware = UsageMeteringMiddleware(generations)

    return {
        "db": app_db,
        "settings": settings,
        "directory": directory,
        "accounts": accounts,
        "billing": billing,
        "counters": counters,
        "generations": generations,
        "conversations": conversations,
        "agent_audit": agent_audit,
        "admin_audit": admin_audit,
        "platform": platform,
        "resolver": resolver,
        "mailer": build_mailer(settings),
        "oidc": build_oidc_client(settings),
        "throttle": LoginThrottle(
            counters,
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_window_seconds,
        ),
    }


# ----------------------------------------------------------------------
# Middleware
# ----------------------------------------------------------------------


def _install_middleware(app: Any, settings: Any) -> None:
    from fastapi.middleware.cors import CORSMiddleware

    from .csrf import CsrfMiddleware
    from .observability import RequestContextMiddleware

    # The web UI is normally same-origin (nginx proxies /api), so CORS matters only
    # for direct access to :8000. Credentials are allowed because identity rides on
    # cookies, which means an explicit origin list -- "*" is rejected by browsers
    # alongside credentials, and refused by config.validate.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )

    # Order matters: added last runs first. CSRF must reject before anything else
    # does work, and the request-id must be set before CSRF logs a rejection.
    app.add_middleware(
        CsrfMiddleware,
        secret=settings.secret_key,
        secure=settings.secure_cookies,
        enabled=not settings.is_demo,
    )
    app.add_middleware(RequestContextMiddleware)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


def _register_routes(app: Any, settings: Any, services: Dict[str, Any]) -> None:
    from vanna.servers.fastapi.admin_routes import register_admin_routes
    from vanna.servers.fastapi.routes import register_chat_routes

    from .routes import Deps, register_all

    deps = Deps(
        settings=settings,
        platform=services["platform"],
        user_resolver=services["resolver"],
        directory=services["directory"],
        accounts=services["accounts"],
        billing=services["billing"],
        generation_store=services["generations"],
        conversation_store=services["conversations"],
        counters=services["counters"],
        admin_audit=services["admin_audit"],
        agent_audit=services["agent_audit"],
        mailer=services["mailer"],
        login_throttle=services["throttle"],
        oidc=services["oidc"],
    )
    register_all(app, deps)

    # Knowledge review: examples, feedback and stats. Shipped by the library.
    #
    # `instruction_store=None` is deliberate. The library's instruction endpoints
    # gate on `"admin" in group_memberships` and take no tenant in the path, which
    # is right for a single-tenant deployment and wrong for this one in two ways:
    # a platform admin administering a customer's workspace is refused, and there
    # is nothing to check the path's tenant against. Instructions are served from
    # `routes/instructions.py` instead, behind `require_tenant_admin`. Leaving both
    # registered would keep the weaker gate reachable.
    platform = services["platform"]
    register_admin_routes(
        app,
        user_resolver=services["resolver"],
        example_store=platform.examples,
        instruction_store=None,
        generation_store=platform.generations,
        agent_memory=platform.memory,
        dialect=None,
    )

    register_chat_routes(app, _tenant_dispatch_handler(settings, services))


def _tenant_dispatch_handler(settings: Any, services: Dict[str, Any]) -> Any:
    """A ``ChatHandler`` that picks the agent per request.

    The library's routes take one handler bound to one agent, which is exactly the
    assumption multi-tenancy breaks: the agent owns the tool registry, which owns
    the SQL runner, which owns a connection to *one* database. Rather than fork the
    routes, this substitutes a handler that resolves the caller first and delegates
    to their workspace's agent -- so SSE, websocket and polling all become
    workspace-aware at once.
    """
    from vanna.servers.base import ChatHandler

    from .authz import require_full_session
    from .identity import build_byo_llm_service, close_llm_service

    resolver = services["resolver"]
    platform = services["platform"]

    async def _bound_data_source(user: Any, request: Any) -> Optional[str]:
        """Which database this conversation is about.

        The client may *propose* one in ``request.metadata`` when it starts a
        thread. It is a preference, not a credential -- the same standing
        ``x-tenant-id`` has -- so it is checked against the workspace's registry
        before anything is built from it, and the answer is then pinned to the
        conversation.

        After that the client's value is ignored entirely. A thread's history is a
        record of questions asked against one schema; letting a later message
        redirect it would show the model earlier turns describing tables that are
        no longer in scope, and would let a client move a conversation between
        databases by editing one field.

        Returns None for "the workspace default", which is every request that does
        not ask for anything.
        """
        conversations = services.get("conversations")
        conversation_id = getattr(request, "conversation_id", None)

        # An already-bound thread decides, whatever the client says now.
        if conversations is not None and conversation_id:
            try:
                bound = await conversations.data_source_of(user.tenant_id, conversation_id)
            except Exception as exc:
                logger.warning("Could not read the thread's database: %s", exc)
                bound = None
            if bound:
                return bound

        # Two ways in, because two kinds of client exist. An API caller controls
        # its own payload and puts it in `metadata`; the web UI's chat element
        # builds that payload itself and would need rebuilding to add a field, but
        # already forwards arbitrary headers. Both are equally untrusted and take
        # exactly the same route through the registry below.
        requested = (getattr(request, "metadata", None) or {}).get("data_source_id")
        if not requested:
            headers = getattr(getattr(request, "request_context", None), "headers", None) or {}
            lowered_headers = {str(k).lower(): v for k, v in headers.items()}
            requested = lowered_headers.get("x-data-source-id")
        if not requested:
            return None

        # Authorize before pinning: an id that is not registered to this workspace
        # must fail here rather than reach `runtime_for`.
        try:
            await platform.resolve_source(user.tenant_id, str(requested))
        except Exception as exc:
            logger.warning(
                "Ignoring an unavailable database %r for %s: %s",
                requested, user.tenant_id, exc,
            )
            return None

        if conversations is not None and conversation_id:
            try:
                return await conversations.bind_data_source(
                    user.tenant_id, conversation_id, str(requested), user.id
                )
            except Exception as exc:
                # The thread stays unbound and the request uses the choice once.
                # Losing the pin is a worse answer next turn, not a wrong one now.
                logger.warning("Could not pin the thread's database: %s", exc)
        return str(requested)

    class TenantDispatchChatHandler(ChatHandler):

        def __init__(self) -> None:  # deliberately no super().__init__
            self.agent = None

        async def _delegate(self, request: Any) -> tuple:
            """Resolve the caller, pick their workspace's handler, and their LLM.

            Returns ``(handler, token, service)``; the token must be released in a
            ``finally`` or the next request handled by this worker could inherit
            somebody else's API key.
            """
            from vanna.core.llm import use_llm_service

            # Read the key BEFORE resolving, because resolve_user strips it from the
            # request context on the way past -- see the comment there.
            headers = getattr(request.request_context, "headers", {}) or {}
            lowered = {k.lower(): v for k, v in headers.items()}

            user = await resolver.resolve_user(request.request_context)
            # A session issued for a temporary password must not be able to ask
            # questions, only to set a new password.
            require_full_session(user)

            data_source_id = await _bound_data_source(user, request)
            handler = (
                await platform.runtime_for(
                    user.tenant_id, data_source_id=data_source_id
                )
            ).handler

            token = None
            service = None
            # The resolver decides whether the key may be used at all -- it knows the
            # workspace, and a workspace can forbid personal keys.
            if (user.metadata or {}).get("byo_key"):
                service = build_byo_llm_service(lowered)
                if service is not None:
                    token = use_llm_service(service)
                    logger.info(
                        "Answering for %s on their own key (quota not charged)", user.email
                    )
            return handler, token, service

        async def handle_stream(self, request: Any) -> Any:
            # PermissionError from the resolver propagates into the route's error
            # handling, which turns it into an SSE `error` event. That is the right
            # place for it to land: the user is looking at the chat transcript, not
            # at a status code.
            from vanna.core.llm import release_llm_service

            handler, token, service = await self._delegate(request)
            try:
                async for chunk in handler.handle_stream(request):
                    yield chunk
            finally:
                # Also runs when the client disconnects mid-stream, which is the
                # case that would otherwise leak both the override and the socket.
                if token is not None:
                    release_llm_service(token)
                if service is not None:
                    close_llm_service(service)

        async def handle_poll(self, request: Any) -> Any:
            from vanna.core.llm import release_llm_service

            handler, token, service = await self._delegate(request)
            try:
                return await handler.handle_poll(request)
            finally:
                if token is not None:
                    release_llm_service(token)
                if service is not None:
                    close_llm_service(service)

    return TenantDispatchChatHandler()


# ----------------------------------------------------------------------
# Probes
# ----------------------------------------------------------------------


def _register_probes(app: Any, settings: Any, services: Dict[str, Any]) -> None:
    # JSONResponse rather than a `response: Response` parameter.
    #
    # This module has `from __future__ import annotations`, so every annotation is a
    # string that FastAPI resolves against the *module* namespace -- and a
    # function-local import is not in it. A `response: Response` parameter therefore
    # looked like an unresolvable type, which FastAPI treats as a required query
    # parameter: /ready answered 422 "field required" instead of running the check.
    # Returning the response explicitly has no such trap.
    from fastapi.responses import JSONResponse

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        """Liveness. Intentionally does not touch the database.

        A liveness probe that queries the warehouse turns a slow database into a
        restart loop, which is strictly worse than a slow database.
        """
        return {
            "status": "ok",
            "mode": settings.mode,
            "version": app.version,
        }

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Any:
        """Readiness. Does check the control plane.

        Distinct from liveness because they answer different questions. A pod whose
        control plane is unreachable is alive and must not receive traffic:
        authentication depends on it, and with only one probe an orchestrator would
        route users to a replica that cannot sign anybody in.
        """
        db = services["db"]
        checks = {"control_plane": True, "migrations": True}

        if db is not None:
            checks["control_plane"] = await asyncio.to_thread(db.health)
            if checks["control_plane"]:
                from .migrate import status

                try:
                    checks["migrations"] = (await asyncio.to_thread(status, db))["up_to_date"]
                except Exception:
                    checks["migrations"] = False

        ok = all(checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ready" if ok else "not-ready",
                "checks": checks,
                "tenants_loaded": services["platform"].cached_tenants(),
            },
        )


# ----------------------------------------------------------------------
# Lifespan
# ----------------------------------------------------------------------


def _lifespan(settings: Any, services: Dict[str, Any]) -> Any:
    """Startup and shutdown.

    Replaces ``@app.on_event``, which FastAPI deprecated, and gives shutdown a place
    to close the connection pools that a cached runtime holds.
    """

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        platform = services["platform"]

        # Seeding runs under an advisory lock, so exactly one worker does it.
        #
        # Without it all four workers found an empty directory, all four seeded, and
        # -- when the password is generated rather than configured -- each generated
        # a different one. Three of the four passwords printed in the log were dead
        # before anybody read them, and which survived depended on write ordering.
        await once(services["db"], KEY_SEED, lambda: _seed(settings, services))

        housekeeping = asyncio.create_task(_housekeeping(settings, services))
        warm = asyncio.create_task(_warm_default(settings, platform))

        try:
            yield
        finally:
            for task in (housekeeping, warm):
                task.cancel()
            platform.shutdown()
            if services["db"] is not None:
                services["db"].close()
            logger.info("Shutdown complete.")

    return lifespan


async def _seed(settings: Any, services: Dict[str, Any]) -> None:
    """Create the first workspace and the first administrator, once.

    Called with the seeding advisory lock held, so the "is anything here yet?" check
    inside each helper is no longer a race between workers. Both helpers are
    additionally guarded on "there is nothing at all" rather than "this row is
    missing", which is what guarantees a deliberately deleted demo workspace stays
    deleted.
    """
    directory = services["directory"]
    accounts = services["accounts"]

    if directory is not None:
        try:
            from .tenancy import seed_directory

            await seed_directory(
                directory,
                default_tenant=settings.default_tenant,
                default_database_url=settings.database_url or None,
                admin_emails=sorted(settings.admin_emails),
            )
        except Exception as exc:
            logger.error("Could not seed the directory: %s", exc, exc_info=True)

    if accounts is None:
        return

    try:
        from .accounts import seed_first_admin

        generated = await seed_first_admin(
            accounts,
            admin_emails=sorted(settings.admin_emails),
            password=settings.admin_password,
        )
        if generated:
            # Logged once, loudly, by the one worker that did the seeding. A
            # deployment nobody can sign in to is the failure this exists to
            # prevent, and there is no second chance to print it -- only the hash
            # is stored.
            banner = "=" * 66
            logger.warning(
                "%s\n  First-run administrator account\n"
                "    email:    %s\n    password: %s\n"
                "  Change it after signing in. This is shown only once.\n%s",
                banner,
                sorted(settings.admin_emails)[0]
                if settings.admin_emails
                else "demo@example.com",
                generated,
                banner,
            )
    except Exception as exc:
        logger.error("Could not seed the first account: %s", exc, exc_info=True)


async def _warm_default(settings: Any, platform: Any) -> None:
    """Build the default workspace's runtime so the first user does not pay for it.

    Backgrounded: a slow warehouse should delay good answers, not startup.
    """
    try:
        await platform.runtime_for(settings.default_tenant)
    except Exception as exc:
        logger.error("Could not warm the default workspace: %s", exc)


async def _housekeeping(settings: Any, services: Dict[str, Any]) -> None:
    """Expiry, retention and counter garbage collection, hourly.

    In-process rather than a cron container: the work is three DELETEs, and every
    replica running them is harmless because each is idempotent and bounded. The
    advisory-lock dance a scheduler would need costs more than the duplicate work.
    """
    accounts = services["accounts"]
    counters = services["counters"]
    generations = services["generations"]
    admin_audit = services["admin_audit"]

    if accounts is None:
        return

    while True:
        try:
            expired = await accounts.purge_expired()
            if expired:
                logger.info("Purged %d expired session(s)", expired)
            if counters is not None:
                await counters.purge()
            if generations is not None and settings.generation_retention_days:
                await generations.purge_older_than(settings.generation_retention_days)
                # The audit trail is kept longer than question text on purpose: it
                # holds no customer data and is what an investigation reads.
                await admin_audit.purge_older_than(
                    max(settings.generation_retention_days, 730)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Housekeeping pass failed: %s", exc)

        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise


# ----------------------------------------------------------------------
# ASGI entry point
# ----------------------------------------------------------------------
#
# A factory, not a module-level `app = create_app()`.
#
# Importing this module used to build a whole application from the process
# environment: open the control plane, run migrations, construct the platform. That
# made the module impossible to import for any other purpose -- a test that wanted
# `create_app(settings)` with its own configuration got a second, unwanted
# application built from whatever happened to be in os.environ first, and any tooling
# that imported it (mypy, a CLI, a docs build) paid the same cost.
#
# uvicorn is started with `--factory`, which calls this instead.


def application() -> Any:
    """Build the app from the process environment. The uvicorn entry point."""
    return create_app()
