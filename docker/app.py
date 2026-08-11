"""Runnable Vanna server wiring every capability built in this project.

Vanna is a library: it gives you an ``Agent`` and expects you to assemble it.
This module is that assembly, made runnable so the stack can start with one
command -- and it doubles as the worked example of how the pieces fit together.

Wired here:

* SQL policy enforcement on the tool registry  (read-only, AST-validated)
* Execution guardrails                          (row caps, timeouts, pooling)
* Schema catalog + scanner                      (structure and real enum values)
* Deterministic retrieval into the prompt       (rules, examples, schema)
* Golden examples and business rules            (markdown, version-controllable)
* Generation lineage + feedback                 (so quality is measurable)
* Quota and rate limiting                       (per user, per window)
* SQL repair with error classification          (targeted retry hints)

**Multi-tenancy.** Tenants are rows in a PostgreSQL control plane
(``tenancy.py``), each bound to its own data source, each with its own members
and roles. One ``Agent`` is built *per tenant*, lazily, and cached -- because
the SQL runner, and therefore every tool in the registry, is bound to a
connection at construction time. Sharing one agent across tenants would mean
sharing one database connection across tenants, which is not a thing you can
patch up later at the prompt layer.

Everything is driven by environment variables so the same image runs in demo
mode with no credentials and in production with real ones. See ``.env.example``.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("vanna.app")


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

DATA_DIR = Path(os.getenv("VANNA_DATA_DIR", "/data"))
KNOWLEDGE_DIR = Path(os.getenv("VANNA_KNOWLEDGE_DIR", str(DATA_DIR / "knowledge")))
DB_URL = os.getenv("VANNA_DATABASE_URL", "")
SQLITE_PATH = os.getenv("VANNA_SQLITE_PATH", str(DATA_DIR / "demo.db"))
LLM_PROVIDER = os.getenv("VANNA_LLM_PROVIDER", "auto").lower()
ADMIN_EMAILS = {
    e.strip().lower() for e in os.getenv("VANNA_ADMIN_EMAILS", "").split(",") if e.strip()
}
DEFAULT_TENANT = os.getenv("VANNA_DEFAULT_TENANT", "demo").strip().lower() or "demo"
SCAN_ON_START = os.getenv("VANNA_SCAN_ON_START", "true").lower() == "true"
MAX_ROWS = int(os.getenv("VANNA_MAX_ROWS", "1000"))
QUERY_TIMEOUT = int(os.getenv("VANNA_QUERY_TIMEOUT", "60"))
DAILY_QUOTA = int(os.getenv("VANNA_DAILY_QUOTA", "200"))
RATE_LIMIT = int(os.getenv("VANNA_RATE_LIMIT_PER_MIN", "20"))

#: Master switch for write statements. Both this *and* the tenant's own
#: `allow_writes` must be true before a workspace admin can run DML -- a single
#: global flag is how a capability like this ends up accidentally on in
#: production, and neither operator would have had to make a decision.
ALLOW_WRITES = os.getenv("VANNA_ALLOW_WRITES", "false").lower() == "true"

#: Name of the session cookie. httpOnly, so the page cannot read it -- which is what
#: lets the frontend stop holding an identity at all.
SESSION_COOKIE = "vanna_session"

#: Accept ``X-User-Email`` as proof of identity. Off by default and only consulted once
#: no account exists (bootstrap). Turn it on deliberately when this sits behind a
#: gateway that authenticates and sets the header itself -- in which case the gateway,
#: not this process, is the thing being trusted.
TRUST_HEADERS = os.getenv("VANNA_TRUST_HEADERS", "false").lower() == "true"

#: How long a browser session lasts.
SESSION_TTL_HOURS = int(os.getenv("VANNA_SESSION_TTL_HOURS", "72"))

#: Password for the seeded first admin. Unset means one is generated and logged once.
ADMIN_PASSWORD = os.getenv("VANNA_ADMIN_PASSWORD", "")

#: Send the session cookie only over HTTPS. Must be on in any real deployment; off by
#: default because the compose stack serves plain HTTP on localhost and a Secure cookie
#: there is simply never sent, which looks like a broken login.
SECURE_COOKIES = os.getenv("VANNA_SECURE_COOKIES", "false").lower() == "true"

#: Statements a write-enabled workspace may run. DML only, never DDL: loosening
#: `mode` alone would otherwise admit DROP and TRUNCATE, and no analytics
#: question has ever needed either.
WRITE_STATEMENTS = frozenset({"SELECT", "WITH", "UNION", "EXCEPT", "INTERSECT",
                              "INSERT", "UPDATE", "DELETE"})


# ----------------------------------------------------------------------
# Demo database
# ----------------------------------------------------------------------

DEMO_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    region       TEXT NOT NULL,
    tier         TEXT NOT NULL,
    signed_up_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    category TEXT NOT NULL,
    price_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    product_id   INTEGER NOT NULL REFERENCES products(id),
    status       TEXT NOT NULL,
    quantity     INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    ordered_on   TEXT NOT NULL
);
"""


def seed_demo_database(path: str) -> None:
    """Create a small demo database if none exists.

    Deliberately shaped to exercise the features: ``region``, ``tier``,
    ``status``, and ``category`` are all low-cardinality, so the scanner
    captures their real values and the agent never has to guess a filter
    literal. ``amount_cents`` is in cents to give the instruction store
    something real to correct.
    """
    file = Path(path)
    if file.exists() and file.stat().st_size > 0:
        return

    logger.info("Seeding demo database at %s", path)
    file.parent.mkdir(parents=True, exist_ok=True)

    import random

    random.seed(42)  # reproducible demo data

    regions = ["EMEA", "AMER", "APAC"]
    tiers = ["free", "pro", "enterprise"]
    statuses = ["PENDING", "SHIPPED", "DELIVERED", "CANCELLED"]
    categories = ["Hardware", "Software", "Services"]

    conn = sqlite3.connect(path)
    conn.executescript(DEMO_SCHEMA)

    conn.executemany(
        "INSERT INTO customers VALUES (?,?,?,?,?)",
        [
            (
                i,
                f"Customer {i:03d}",
                regions[i % 3],
                tiers[i % 3],
                f"202{4 + i % 2}-{(i % 12) + 1:02d}-15",
            )
            for i in range(1, 121)
        ],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?)",
        [
            (i, f"Product {i:02d}", categories[i % 3], (i * 1999) % 250_000 + 999)
            for i in range(1, 25)
        ],
    )
    conn.executemany(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
        [
            (
                i,
                (i % 120) + 1,
                (i % 24) + 1,
                statuses[i % 4],
                (i % 5) + 1,
                ((i % 5) + 1) * (((i * 1999) % 250_000) + 999),
                f"2026-{(i % 8) + 1:02d}-{(i % 28) + 1:02d}",
            )
            for i in range(1, 2001)
        ],
    )
    conn.commit()
    conn.close()
    logger.info("Demo database seeded: 120 customers, 24 products, 2000 orders")


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def build_user_resolver(directory, accounts=None):
    """Resolve identity from headers, authorise from the directory.

    **The transport is demo-grade.** It trusts ``X-User-Email`` /
    ``X-Tenant-Id`` outright, which is fine behind a gateway that sets them and
    catastrophic if the header reaches the app straight from the internet.
    Replace *this half* with your real authentication before exposing the
    stack -- it is the single seam you must change.

    **The authorisation half is not demo-grade**, and that is the point of
    having a directory. Once tenants exist, claiming a header no longer grants
    anything: the address must be an active member of the tenant it claims, and
    the role that decides what it can do comes from the ``tenant_users`` row,
    not from the request. So a forged header can at worst impersonate a real
    member of a real tenant, rather than conjure an admin of any tenant.

    Until the first tenant exists there is nothing to check against, so the
    resolver stays open -- otherwise a fresh install would lock out the very
    person who has to create the first tenant.
    """
    from vanna.core.user import RequestContext, User, UserResolver

    class DirectoryUserResolver(UserResolver):
        async def _authenticate(self, headers: dict, cookies: dict) -> str:
            """Establish who is calling: session cookie, then API token, then header.

            The header is last and, once any account exists, refused unless a deployment
            has deliberately re-enabled it. That ordering is the whole point of this
            change: previously the header *was* the identity, so anyone who could reach
            the API could be anyone.

            There is no fallback identity. SQL Chat ends its equivalent with
            ``return requestIp.getClientIp(req)``, which turns an unauthenticated caller
            into a metered one -- an identity that changes when the address does. Here,
            no credential means no identity and the request is refused.
            """
            if accounts is not None:
                token = cookies.get(SESSION_COOKIE, "")
                if token:
                    user = await accounts.session_user(token)
                    if user is not None:
                        return user["email"]

                authorization = headers.get("authorization", "")
                if authorization.lower().startswith("bearer "):
                    user = await accounts.token_user(authorization[7:].strip())
                    if user is not None:
                        return user["email"]

            claimed = (headers.get("x-user-email") or "").strip().lower()

            # While no account exists, the header still works: a fresh install has to be
            # reachable by the person who will create the first one. The moment one
            # exists the door closes, which is the same bootstrap rule the directory
            # already uses for tenants.
            if accounts is not None and await accounts.count() > 0 and not TRUST_HEADERS:
                raise PermissionError(
                    "Sign in to continue."
                    if not claimed
                    else "Header authentication is disabled. Sign in, or use an API token."
                )

            if not claimed:
                if accounts is not None and await accounts.count() > 0:
                    raise PermissionError("Sign in to continue.")
                return "demo@example.com"
            return claimed

        async def resolve_user(self, request_context: RequestContext) -> User:
            headers = getattr(request_context, "headers", {}) or {}
            lowered = {k.lower(): v for k, v in headers.items()}
            cookies = getattr(request_context, "cookies", {}) or {}

            email = await self._authenticate(lowered, cookies)
            tenant = (lowered.get("x-tenant-id") or DEFAULT_TENANT).strip().lower()

            # Platform admins are configured out of band, so an operator can
            # always get in -- including to a tenant they are not a member of,
            # which is how they fix one that has locked itself out.
            is_platform_admin = (not ADMIN_EMAILS) or (email in ADMIN_EMAILS)

            if directory is None:
                # No control plane: single-tenant behaviour, unchanged from
                # before this file grew a directory.
                groups = ["user"] + (["admin"] if is_platform_admin else [])
                return User(id=email, email=email, tenant_id=tenant, group_memberships=groups)

            if await directory.count_tenants() == 0:
                groups = ["user", "admin"]  # bootstrap: someone has to set up
                return User(id=email, email=email, tenant_id=tenant, group_memberships=groups)

            row = await directory.get_tenant(tenant)
            if row is None or not row["is_active"]:
                if not is_platform_admin:
                    raise PermissionError(f"No active tenant named {tenant!r}")
                member = None
            else:
                member = await directory.get_member(tenant, email)

            if member is None and not is_platform_admin:
                raise PermissionError(
                    f"{email} is not a member of {tenant!r}. Ask an administrator "
                    "of that workspace for access."
                )
            if member is not None and not member["is_active"]:
                raise PermissionError(f"Access for {email} has been disabled.")

            role = member["role"] if member else "admin"
            groups = ["user"]
            if role == "admin" or is_platform_admin:
                groups.append("admin")

            return User(
                id=email,
                email=email,
                username=(member or {}).get("full_name") or "",
                tenant_id=tenant,
                group_memberships=groups,
                # Carried so route handlers can distinguish analyst from viewer
                # without a second directory round trip.
                metadata={"role": role, "platform_admin": is_platform_admin},
            )

    return DirectoryUserResolver()


# ----------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------


def build_llm_service():
    """Pick an LLM provider from the environment.

    Falls back to the mock service when no key is present, so ``docker compose
    up`` works with zero configuration. The mock produces canned responses --
    enough to exercise streaming, components, and the admin flows, not enough
    to write real SQL.
    """
    provider = LLM_PROVIDER

    if provider == "auto":
        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "mock"

    if provider == "anthropic":
        from vanna.integrations.anthropic import AnthropicLlmService

        logger.info("LLM provider: Anthropic (%s)", os.getenv("ANTHROPIC_MODEL", "default"))
        return AnthropicLlmService()

    if provider == "openai":
        from vanna.integrations.openai import OpenAILlmService

        logger.info("LLM provider: OpenAI")
        return OpenAILlmService()

    from vanna.integrations.mock import MockLlmService

    logger.warning(
        "No LLM API key found -- using the mock service. Set ANTHROPIC_API_KEY "
        "or OPENAI_API_KEY for real answers."
    )
    return MockLlmService()


# ----------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------


def build_sql_runner(
    database_url: str, *, max_rows: int = MAX_ROWS, read_only: bool = True
):
    """Build the SQL runner, with guardrails, for one connection string.

    ``read_only`` opens the connection in a read-only transaction, which is the
    last of three independent layers standing between a question and a write:
    the per-user SQL policy, the statement allow-list, and this. It is the only
    one the application cannot talk its way past, so it is left on unless the
    workspace has explicitly been granted writes.
    """
    from vanna.capabilities.sql_runner import ExecutionPolicy

    policy = ExecutionPolicy(max_rows=max_rows, timeout_seconds=QUERY_TIMEOUT)

    if database_url.startswith("postgres"):
        from vanna.integrations.postgres import PostgresRunner

        return PostgresRunner(
            connection_string=database_url, policy=policy, read_only=read_only
        )

    from vanna.integrations.sqlite import SqliteRunner

    seed_demo_database(SQLITE_PATH)
    return SqliteRunner(SQLITE_PATH, policy=policy, read_only=True)


def _build_memory():
    """In-memory agent memory, partitioned per tenant.

    The partition wrapper is what keeps one tenant's saved patterns out of
    another's retrieval, independent of whether the backing store filters.
    """
    from vanna.capabilities.agent_memory import (
        AgentMemory,
        TenantPartitionedAgentMemory,
    )

    class EphemeralMemory(AgentMemory):
        """Non-persistent memory, so the demo starts clean each run."""

        def __init__(self) -> None:
            self._text: list = []

        async def save_tool_usage(self, *a, **k) -> None:
            return None

        async def save_text_memory(self, content, context):
            from vanna.capabilities.agent_memory import TextMemory
            import uuid
            from datetime import datetime, timezone

            memory = TextMemory(
                memory_id=str(uuid.uuid4()),
                content=content,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self._text.append(memory)
            return memory

        async def search_similar_usage(self, *a, **k):
            return []

        async def search_text_memories(self, query, context, **k):
            return []

        async def get_recent_memories(self, context, limit=10):
            return []

        async def get_recent_text_memories(self, context, limit=10):
            return self._text[-limit:]

        async def delete_by_id(self, context, memory_id):
            return False

        async def delete_text_memory(self, context, memory_id):
            return False

        async def clear_memories(self, context, tool_name=None, before_date=None):
            count = len(self._text)
            self._text.clear()
            return count

    return TenantPartitionedAgentMemory(lambda _tenant: EphemeralMemory())


# ----------------------------------------------------------------------
# Generation lineage
# ----------------------------------------------------------------------
#
# The library defines a GenerationStore and an admin API over it, but nothing
# in the agent loop writes to it -- so out of the box the history view, the
# quality stats and the feedback endpoint all operate on an empty table. The
# two pieces below close that gap at the only seam where both halves of a
# generation are visible:
#
#   * the *question* is known to a lifecycle hook, before the LLM is called
#   * the *SQL and its outcome* are known to the run_sql tool, several LLM
#     turns later
#
# A ContextVar carries the first to the second. It is the right primitive here
# because the agent runs one request per asyncio task: the value a hook sets is
# visible to the tools of that request and to no other, with no keying by user
# or request id and no chance of two concurrent questions crossing over.

_CURRENT_QUESTION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "vanna_current_question", default=""
)


def _question_capture_hook():
    """Lifecycle hook that remembers the question being answered."""
    from vanna.core.lifecycle import LifecycleHook

    class QuestionCaptureHook(LifecycleHook):
        async def before_message(self, user, message):
            _CURRENT_QUESTION.set(message or "")
            return None  # never modifies the message

    return QuestionCaptureHook()


#: Statement kinds that change data. Matched on the parsed root rather than a
#: prefix: `WITH x AS (...) DELETE FROM y` starts with SELECT-ish text and is a
#: delete, which is precisely the case a `startsWith("SELECT")` check misses.
_WRITE_ROOTS = frozenset({"insert", "update", "delete", "merge",
                          "drop", "create", "alter", "truncate", "grant"})


def _is_write_statement(sql: str) -> bool:
    """Whether a statement modifies anything, by parsing it."""
    if not (sql or "").strip():
        return False
    try:
        import sqlglot

        statements = [s for s in sqlglot.parse(sql) if s]
    except Exception:
        # Unparseable: treat as a write so it is audited. The policy will
        # reject it anyway, and over-auditing costs a log line.
        return True

    for statement in statements:
        if type(statement).__name__.lower() in _WRITE_ROOTS:
            return True
        # A CTE wrapping a write: the root is `With`, the payload is not.
        for node in statement.walk():
            if type(node).__name__.lower() in _WRITE_ROOTS:
                return True
    return False


def _recording_run_sql_tool(sql_runner, generation_store, data_source: str):
    """``RunSqlTool`` that records every execution as a generation.

    Wrapping the tool rather than post-processing the stream is what makes the
    record trustworthy: this sees the exact SQL that reached the database and
    the exact outcome, including the executions that failed and the ones the
    repair strategy retried -- none of which necessarily appear in the answer
    the user reads.
    """
    from vanna.core.generation import GenerationStatus, SqlGeneration
    from vanna.core.tool import ToolContext, ToolResult
    from vanna.tools import RunSqlTool

    class RecordingRunSqlTool(RunSqlTool):
        async def execute(self, context: ToolContext, args) -> ToolResult:
            statement = getattr(args, "sql", "") or ""
            is_write = _is_write_statement(statement)

            result = await super().execute(context, args)

            if is_write:
                # A write is audited whether it succeeded or not: an attempt
                # that failed is exactly as interesting to whoever reads this
                # later. SQL Chat shows a yellow banner and keeps no record.
                meta = result.metadata or {}
                logger.warning(
                    "WRITE user=%s tenant=%s success=%s rows=%s statement=%s",
                    getattr(context.user, "id", "?"),
                    getattr(context, "tenant_id", "default"),
                    result.success,
                    meta.get("rows_affected", meta.get("row_count")),
                    " ".join(statement.split())[:400],
                )
                context.metadata.setdefault("write_statements", []).append(
                    {
                        "sql": " ".join(statement.split())[:400],
                        "success": result.success,
                        "rows_affected": meta.get("rows_affected"),
                    }
                )

            try:
                await self._record(context, args, result)
            except Exception as exc:
                # Bookkeeping must never break the answer.
                logger.debug("Generation not recorded: %s", exc)
            return result

        async def _record(self, context, args, result) -> None:
            meta = result.metadata or {}
            row_count = meta.get("row_count")

            if not result.success:
                status = GenerationStatus.INVALID
            elif row_count == 0:
                status = GenerationStatus.EMPTY
            else:
                status = GenerationStatus.VALID

            await generation_store.record(
                context,
                SqlGeneration(
                    tenant_id=context.tenant_id,
                    data_source_id=data_source,
                    user_id=getattr(context.user, "id", ""),
                    conversation_id=context.conversation_id,
                    request_id=context.request_id,
                    question=_CURRENT_QUESTION.get(""),
                    sql=getattr(args, "sql", "") or "",
                    status=status,
                    error=(result.error or None),
                    row_count=row_count,
                    truncated=bool(meta.get("truncated")),
                    execution_ms=meta.get("execution_ms"),
                ),
            )

    return RecordingRunSqlTool(sql_runner=sql_runner)


def _policy_for_user(read_only: Any, settings: Dict[str, Any]):
    """Resolve the SQL policy for one caller.

    This is the seam ``ToolRegistry`` documented for exactly this and left
    empty. Writes require **three** independent things to be true -- the
    deployment allows them, this workspace allows them, and the caller is an
    admin of it -- because any one of them alone is something that gets turned
    on for a reason nobody remembers six months later.

    Returns the read-only policy in every other case, which is the default and
    the answer for every anonymous, analyst and viewer request.
    """
    from vanna.core.sql_policy import SqlPolicy
    from vanna.tools import TIME_FUNCTION_NAMES

    tenant_allows = bool(settings.get("allow_writes"))

    if not (ALLOW_WRITES and tenant_allows):
        return None  # no hook needed; the registry uses its read-only policy

    write_policy = SqlPolicy(
        mode="read_write",
        allowed_statements=set(WRITE_STATEMENTS),
        denied_functions=frozenset(TIME_FUNCTION_NAMES),
        default_limit=read_only.default_limit,
        # Left at its default (True) deliberately: `read_write` must not
        # re-admit read_csv/pg_read_file. The check keys off the function list,
        # not the mode, so this only had to not be switched off.
        block_data_readers=True,
    )

    def resolve(user, context):
        if "admin" in (getattr(user, "group_memberships", None) or []):
            logger.info(
                "Write policy granted user=%s tenant=%s",
                getattr(user, "id", "?"),
                getattr(context, "tenant_id", "default"),
            )
            return write_policy
        return read_only

    return resolve


class TenantRuntime:
    """One tenant's agent and everything bound to its data source."""

    def __init__(
        self,
        *,
        tenant_id: str,
        database_url: str,
        dialect: str,
        runner: Any,
        catalog: Any,
        policy: Any,
        agent: Any,
        handler: Any,
        data_source: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.database_url = database_url
        self.dialect = dialect
        self.runner = runner
        self.catalog = catalog
        self.policy = policy
        self.agent = agent
        self.handler = handler
        self.data_source = data_source


class Platform:
    """Owns the shared services and the per-tenant runtime cache.

    Shared across tenants: the LLM client, the knowledge stores, the catalog and
    the generation store -- all of which are tenant-scoped internally, by
    contract, on ``ToolContext.tenant_id``.

    Not shared: the SQL runner and the tool registry built around it. Those hold
    a connection to one tenant's database.
    """

    def __init__(
        self,
        directory,
        generation_store,
        conversation_store=None,
        dashboard_store=None,
    ) -> None:
        from vanna.core.generation import LocalGenerationStore
        from vanna.integrations.local import (
            LocalSchemaCatalog,
            MarkdownExampleStore,
            MarkdownInstructionStore,
        )

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.directory = directory
        # Set by create_app when there is a control plane. Absent means every
        # workspace gets the deployment defaults, which is the correct behaviour
        # for a deployment that is not billing anyone.
        self.billing: Any = None
        self.llm = build_llm_service()
        # Ranked retrieval. `lexical` is dependency-free BM25 and the default;
        # naming a vector integration fuses it with lexical via RRF. A backend
        # that cannot be built downgrades *loudly* -- a silent fall back to
        # keyword search is an unclosable "accuracy dropped after the deploy"
        # ticket.
        from vanna.capabilities.index import resolve_index

        self.index = resolve_index(os.getenv("VANNA_INDEX_BACKEND", "lexical"))
        logger.info("Retrieval index: %s", self.index.name)

        self.catalog = LocalSchemaCatalog(
            str(DATA_DIR / "catalog.json"), index=self.index
        )
        # No `dialect=` here, unlike the single-tenant version: one store now
        # serves tenants on different databases, so there is no single dialect
        # to validate writes against. The per-tenant dialect still reaches the
        # agent through the system prompt and the tool registry.
        self.examples = MarkdownExampleStore(str(KNOWLEDGE_DIR), index=self.index)
        self.instructions = MarkdownInstructionStore(str(KNOWLEDGE_DIR))
        # Falls back to the JSONL store when there is no control plane, so the
        # feedback loop keeps working in the zero-configuration case.
        self.generations = generation_store or LocalGenerationStore(
            str(DATA_DIR / "generations.jsonl")
        )
        # Threads survive a restart only if this is the Postgres store; the
        # in-memory one is the zero-configuration fallback and loses them.
        self.conversations = conversation_store
        self.memory = _build_memory()

        # -- Semantic layer -------------------------------------------
        #
        # Optional by design, and resolved **per tenant**: a manifest describes
        # one database, so it is bound to the workspace whose database it
        # describes and to no other. With no project the stack behaves exactly
        # as it did before the semantic layer existed.
        self.session_properties: Dict[str, str] = {}

        # Dashboards the agent can create. Tenant scoping comes from the
        # ToolContext, as with every other store.
        self.dashboards = dashboard_store

        self._runtimes: Dict[str, TenantRuntime] = {}
        # One lock per tenant would be tidier; one lock is simpler and building
        # a runtime takes milliseconds unless a scan is triggered.
        self._lock = asyncio.Lock()

    # -- semantic layer ------------------------------------------------

    def _project_dir_for(self, tenant_id: str) -> Optional[str]:
        """Which project directory, if any, describes this tenant's database.

        A manifest describes *one* database. Applying a single global manifest
        to every tenant -- which is what this did originally -- shows a
        workspace another workspace's models and makes every query fail against
        a schema that does not contain them.

        Two ways to bind one, both explicit:

        * ``<VANNA_PROJECTS_DIR>/<tenant_id>/`` -- a project per workspace.
        * ``VANNA_PROJECT_DIR`` -- applies to the **default tenant only**,
          which is the single-workspace deployment it was written for.
        """
        projects_root = os.getenv("VANNA_PROJECTS_DIR", "").strip()
        if projects_root:
            candidate = Path(projects_root) / tenant_id
            if (candidate / "vanna_project.yml").is_file():
                return str(candidate)

        project_dir = os.getenv("VANNA_PROJECT_DIR", "").strip()
        if project_dir and tenant_id == DEFAULT_TENANT:
            return project_dir

        return None

    def load_project(self, tenant_id: str):
        """Load one tenant's semantic project. Returns (project, manifest).

        Reads the *built* manifest rather than the YAML tree, so what runs is
        what someone deliberately compiled with ``vanna project build``. Picking
        up half-edited YAML on a container restart is how a deployment starts
        answering questions from a definition nobody approved.
        """
        project_dir = self._project_dir_for(tenant_id)
        if not project_dir:
            return None, None

        from vanna.core.errors import VannaError
        from vanna.project import Project
        from vanna.semantic import load_built_manifest

        try:
            project = Project.load(Path(project_dir))
            manifest = load_built_manifest(project.paths)
        except VannaError as exc:
            logger.error("Could not load the project at %s: %s", project_dir, exc)
            return None, None

        if manifest is None:
            logger.warning(
                "Project %s has no built manifest. Run `vanna project build`; "
                "until then %s uses the physical catalog.",
                project_dir,
                tenant_id,
            )
            return project, None

        logger.info(
            "Semantic layer for %s: %d models, %d relationships, %d cubes "
            "(fanout_guard=%s)",
            tenant_id,
            len(manifest.models),
            len(manifest.relationships),
            len(manifest.cubes),
            project.config.fanout_guard,
        )
        return project, manifest

    # -- runtimes ------------------------------------------------------

    async def runtime_for(self, tenant_id: str, *, invalidate: bool = False) -> TenantRuntime:
        """Get (or build) the runtime for a tenant.

        ``invalidate`` drops the cached runtime first, which is what an admin
        repointing a tenant at a different database needs -- otherwise the old
        connection would keep serving until the process restarted.
        """
        tenant_id = tenant_id or DEFAULT_TENANT

        if invalidate:
            async with self._lock:
                stale = self._runtimes.pop(tenant_id, None)
            if stale is not None:
                logger.info("Dropped cached runtime for %s", tenant_id)

            # The catalog describes the *old* database. Left in place, the agent
            # is shown tables that no longer exist on the connection it is now
            # using -- the same failure as a mismatched manifest, arriving by a
            # different route. Clearing it makes the next request rescan.
            try:
                removed = await self.catalog.clear(self._system_context(tenant_id))
                if removed:
                    logger.info(
                        "Cleared %d stale catalog entries for %s; it will rescan.",
                        removed,
                        tenant_id,
                    )
            except Exception as exc:
                logger.warning("Could not clear the catalog for %s: %s", tenant_id, exc)

        existing = self._runtimes.get(tenant_id)
        if existing is not None:
            return existing

        async with self._lock:
            # Re-check: another request may have built it while we waited.
            if tenant_id in self._runtimes:
                return self._runtimes[tenant_id]
            runtime = await self._build_runtime(tenant_id)
            self._runtimes[tenant_id] = runtime

        # Outside the lock: a first scan can take seconds and must not block
        # every other tenant's first request behind it.
        await self._prepare_tenant(runtime)
        return runtime

    async def _build_runtime(self, tenant_id: str) -> TenantRuntime:
        from vanna.core.agent import Agent, AgentConfig
        from vanna.core.enhancer import BudgetPolicy, RetrievalContextEnhancer
        from vanna.core.lifecycle import InMemoryQuotaHook, RateLimitHook
        from vanna.core.middleware import PromptCacheMiddleware
        from vanna.core.recovery import SqlRepairStrategy
        from vanna.core.sql_policy import SqlPolicy, SqlPolicyToolRegistry
        from vanna.core.system_prompt import AnalystSystemPromptBuilder
        from vanna.integrations.local import MemoryConversationStore
        from vanna.servers.base import ChatHandler
        from vanna.tools import (
            CheckColumnValuesTool,
            SystemTimeTool,
            TIME_FUNCTION_NAMES,
            ValidateSqlTool,
            VisualizeDataTool,
            create_schema_tools,
        )

        from tenancy import describe_data_source

        settings: Dict[str, Any] = {}
        if self.directory is not None:
            settings = await self.directory.get_tenant(tenant_id) or {}

        database_url = settings.get("database_url") or DB_URL

        # Limits come from the subscription unless the workspace has an explicit
        # override. Resolved here, once, rather than at each use: the row cap is
        # baked into the SQL runner and the policy's default LIMIT when they are
        # built, so a plan change takes effect when the runtime is next built --
        # which is what `invalidate` on the subscription routes is for.
        from vanna.core.billing import resolve_limits

        subscription = None
        billing = getattr(self, "billing", None)
        if billing is not None:
            subscription = await billing.get_subscription(tenant_id)

        limits = resolve_limits(
            settings,
            subscription,
            default_quota=DAILY_QUOTA,
            default_max_rows=MAX_ROWS,
        )
        max_rows = limits.max_rows
        quota = limits.daily_quota
        logger.info(
            "Tenant %s limits: plan=%s quota=%s (%s) max_rows=%s (%s)",
            tenant_id,
            limits.plan.name,
            quota,
            limits.quota_source,
            max_rows,
            limits.rows_source,
        )

        # A write-enabled workspace needs a connection that can write. The
        # policy still decides *who* may -- a viewer in this workspace is
        # restricted by policy_for_user, not by the connection.
        writes_enabled = ALLOW_WRITES and bool(settings.get("allow_writes"))
        runner = build_sql_runner(
            database_url, max_rows=max_rows, read_only=not writes_enabled
        )
        if writes_enabled:
            logger.warning(
                "Tenant %s has WRITES ENABLED; its connection is not read-only.",
                tenant_id,
            )
        dialect = getattr(runner, "dialect", "sqlite")

        # Read-only, and non-deterministic time functions denied because
        # SystemTimeTool supplies real date literals instead. Denying them
        # without providing the alternative would just make every date question
        # fail.
        policy = SqlPolicy(
            denied_functions=frozenset(TIME_FUNCTION_NAMES),
            default_limit=max_rows,
        )

        # -- Semantic layer, when the project has one --------------------
        #
        # Two registries, one interface. With a manifest, the agent writes SQL
        # against model names and every tool call is compiled and access-checked
        # in `transform_args`; without one, this is exactly the stack that ran
        # before the semantic layer existed. The swap is here and nowhere else,
        # so nothing downstream needs to know which mode it is in.
        project, manifest = self.load_project(tenant_id)
        fanout_guard = project.config.fanout_guard if project else "warn"
        session_properties = dict(self.session_properties)
        if project is not None:
            # Where each `@session_property` in an access rule reads its value
            # from. Per project, so two workspaces can map different attributes.
            raw = project.config.extra.get("session_properties") or {}
            session_properties = {str(k): str(v) for k, v in raw.items()}

        # A manifest describes one database. Repointing a workspace at another
        # one -- easy to do from the console -- leaves the models describing
        # tables the new database does not have, and every semantic query then
        # fails with "relation does not exist". Check it once, here, and fall
        # back to the physical catalog rather than serving a broken layer.
        if manifest is not None:
            missing = await self._models_missing_from(manifest, runner, dialect)
            if missing:
                logger.error(
                    "Tenant %s is bound to %s, but its semantic project expects "
                    "tables that database does not have (%s). Falling back to "
                    "the scanned catalog. Point the workspace back at the right "
                    "database, or rebuild the project for this one.",
                    tenant_id,
                    describe_data_source(database_url),
                    ", ".join(sorted(missing)[:5]),
                )
                manifest = None

        catalog = self.catalog
        if manifest is not None:
            from vanna.capabilities.schema_catalog.semantic import SemanticSchemaCatalog
            from vanna.core.access import (
                AccessControlToolRegistry,
                SessionPropertyResolver,
            )

            catalog = SemanticSchemaCatalog(manifest, physical=self.catalog)
            registry = AccessControlToolRegistry(
                policy=policy,
                dialect=dialect,
                catalog=catalog,
                manifest=manifest,
                fanout_guard=fanout_guard,
                session_resolver=SessionPropertyResolver(session_properties),
                policy_for_user=_policy_for_user(policy, settings),
            )
            logger.info(
                "Tenant %s uses its semantic layer (%d models) over %s",
                tenant_id,
                len(manifest.models),
                describe_data_source(database_url),
            )
        else:
            registry = SqlPolicyToolRegistry(
                policy=policy,
                dialect=dialect,
                catalog=catalog,
                policy_for_user=_policy_for_user(policy, settings),
            )

        registry.register_local_tool(
            _recording_run_sql_tool(runner, self.generations, describe_data_source(database_url)),
            [],
        )
        registry.register_local_tool(
            ValidateSqlTool(runner, policy=policy, catalog=catalog), []
        )
        registry.register_local_tool(CheckColumnValuesTool(runner), [])
        registry.register_local_tool(SystemTimeTool(), [])
        registry.register_local_tool(VisualizeDataTool(), [])

        # Dashboard tools. Without these the agent has no way to *save* one, so
        # "build me a dashboard" could only ever produce a chart in the
        # transcript -- which is what it did, convincingly enough that the
        # missing tool was not obvious.
        if self.dashboards is not None:
            from vanna.tools import create_dashboard_tools

            for tool in create_dashboard_tools(self.dashboards):
                # Viewers may read a dashboard but not author one.
                registry.register_local_tool(tool, ["admin", "analyst"])
        for tool in create_schema_tools(catalog):
            registry.register_local_tool(tool, [])

        agent = Agent(
            llm_service=self.llm,
            tool_registry=registry,
            user_resolver=self.user_resolver,
            agent_memory=self.memory,
            conversation_store=self.conversations or MemoryConversationStore(),
            config=AgentConfig(max_tool_iterations=12, temperature=0.0),
            system_prompt_builder=AnalystSystemPromptBuilder(dialect=dialect),
            llm_context_enhancer=RetrievalContextEnhancer(
                catalog=catalog,
                example_store=self.examples,
                instruction_store=self.instructions,
                budget=BudgetPolicy(total_tokens=120_000),
            ),
            llm_middlewares=[PromptCacheMiddleware()],
            lifecycle_hooks=[
                # Order matters only in that the quota check should reject
                # before anything else does work.
                InMemoryQuotaHook(max_messages=quota),
                RateLimitHook(max_requests=RATE_LIMIT),
                _question_capture_hook(),
            ],
            error_recovery_strategy=SqlRepairStrategy(catalog=catalog),
        )

        logger.info(
            "Built runtime for tenant %s -> %s (%s)",
            tenant_id,
            describe_data_source(database_url),
            dialect,
        )

        return TenantRuntime(
            tenant_id=tenant_id,
            database_url=database_url,
            dialect=dialect,
            runner=runner,
            catalog=catalog,
            policy=policy,
            agent=agent,
            handler=ChatHandler(agent),
            data_source=describe_data_source(database_url),
        )

    async def _models_missing_from(self, manifest, runner, dialect: str) -> set:
        """Model tables the connected database does not contain.

        A single cheap probe rather than a per-model check: one query listing
        the tables, compared against what the manifest expects. Returns an empty
        set when the check itself cannot run -- an unreachable database is a
        different problem, and refusing the semantic layer over it would swap a
        clear error for a confusing one.
        """
        from vanna.capabilities.sql_runner import RunSqlToolArgs

        expected = {
            (model.table_reference or "").split(".")[-1].strip('"').lower()
            for model in manifest.models
            if model.table_reference
        }
        if not expected:
            return set()

        try:
            frame = await runner.run_sql(
                RunSqlToolArgs(
                    sql=(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                    )
                ),
                self._system_context("__probe__"),
            )
            present = {str(v).lower() for v in frame.iloc[:, 0].tolist()}
        except Exception as exc:
            logger.debug("Could not verify the manifest against the database: %s", exc)
            return set()

        return expected - present

    async def _prepare_tenant(self, runtime: TenantRuntime) -> None:
        """Scan and seed a tenant's knowledge, once, on first use.

        Lazy rather than at boot: with several tenants pointed at several
        databases, scanning all of them at startup turns one slow warehouse into
        a container that never reports healthy.
        """
        ctx = self._system_context(runtime.tenant_id)

        try:
            if await runtime.catalog.get_tables(ctx):
                return  # already scanned
        except Exception:
            pass

        if SCAN_ON_START:
            from vanna.capabilities.schema_catalog import SchemaScanner

            try:
                report = await SchemaScanner(runtime.runner, dialect=runtime.dialect).scan(
                    ctx, runtime.catalog
                )
                logger.info("Schema scan for %s: %s", runtime.tenant_id, report.summary())
            except Exception as exc:
                logger.error("Schema scan failed for %s: %s", runtime.tenant_id, exc)

        await self._seed_knowledge(runtime, ctx)

    async def _seed_knowledge(self, runtime: TenantRuntime, ctx) -> None:
        """Seed starter examples and one instruction, only when empty.

        Only when the store is empty -- a rescan must not resurrect seeds a
        reviewer deleted, or bury the curated examples accumulated since.
        """
        from vanna.capabilities.knowledge import Instruction, InstructionScope

        try:
            from vanna.capabilities.knowledge import seed_example_store

            added = await seed_example_store(
                ctx,
                self.examples,
                await runtime.catalog.get_tables(ctx),
                await runtime.catalog.get_relationships(ctx),
                dialect=runtime.dialect,
            )
            if added:
                logger.info("Seeded %d starter examples for %s", added, runtime.tenant_id)
        except Exception as exc:
            logger.warning("Could not seed starter examples: %s", exc)

        try:
            if not await self.instructions.list_all(ctx):
                await self.instructions.add(
                    ctx,
                    Instruction(
                        text=(
                            "Monetary columns ending in _cents store integer cents. "
                            "Divide by 100.0 and label the result in dollars."
                        ),
                        scope=InstructionScope.GLOBAL,
                        priority=10,
                    ),
                )
        except Exception as exc:
            logger.warning("Could not seed starter instruction: %s", exc)

    def _system_context(self, tenant_id: str):
        """A privileged context for background work on one tenant."""
        import uuid

        from vanna.core.tool import ToolContext
        from vanna.core.user import User

        return ToolContext(
            user=User(id="system", tenant_id=tenant_id, group_memberships=["admin"]),
            conversation_id="system",
            request_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            agent_memory=self.memory,
        )


# ----------------------------------------------------------------------
# ASGI application
# ----------------------------------------------------------------------


def create_app():
    """Build the FastAPI application served by uvicorn."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from accounts import Accounts, seed_first_admin
    from auth_routes import register_auth_routes
    from billing import Billing
    from portal_routes import register_portal_routes
    from tenancy import (
        Directory,
        PostgresConversationStore,
        PostgresDashboardStore,
        PostgresGenerationStore,
        build_app_database,
        seed_directory,
    )
    from vanna.servers.base import ChatHandler
    from vanna.servers.fastapi.admin_routes import register_admin_routes
    from vanna.servers.fastapi.routes import register_chat_routes

    app_db = build_app_database()
    directory = Directory(app_db) if app_db else None
    generations = PostgresGenerationStore(app_db) if app_db else None

    accounts = Accounts(app_db) if app_db else None
    billing = Billing(app_db) if app_db else None
    conversations = PostgresConversationStore(app_db) if app_db else None
    dashboards = PostgresDashboardStore(directory) if directory else None
    platform = Platform(directory, generations, conversations, dashboards)
    # Limits are resolved from the subscription when building a tenant's runtime, so
    # the platform needs to be able to read one.
    platform.billing = billing  # type: ignore[attr-defined]
    resolver = build_user_resolver(directory, accounts)
    # The agents are built lazily and each needs the resolver, so it is attached
    # to the platform rather than threaded through every call site.
    platform.user_resolver = resolver  # type: ignore[attr-defined]

    app = FastAPI(
        title="Vanna",
        description="Natural-language querying over your database, per tenant",
        version="2.1.0",
    )

    # The web UI is normally same-origin (nginx proxies /api), so CORS matters
    # only for direct access to :8000. Credentials are allowed because identity
    # rides on headers/cookies, which means an explicit origin list -- "*" is
    # rejected by browsers alongside credentials.
    origins = [
        o.strip()
        for o in os.getenv("VANNA_CORS_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Chat, dispatched to the caller's tenant
    # ------------------------------------------------------------------

    class TenantDispatchChatHandler(ChatHandler):
        """A ``ChatHandler`` that picks the agent per request.

        The library's routes take one handler bound to one agent, which is
        exactly the assumption multi-tenancy breaks: the agent owns the tool
        registry, which owns the SQL runner, which owns a connection to *one*
        database. Rather than fork the routes, this substitutes a handler that
        resolves the caller first and delegates to their tenant's agent -- so
        SSE, websocket and polling all become tenant-aware at once.
        """

        def __init__(self) -> None:  # deliberately no super().__init__
            self.agent = None

        async def _delegate(self, request) -> ChatHandler:
            user = await resolver.resolve_user(request.request_context)
            return (await platform.runtime_for(user.tenant_id)).handler

        async def handle_stream(self, request):
            # PermissionError from the resolver propagates into the route's
            # error handling, which turns it into an SSE `error` event. That is
            # the right place for it to land: the user is looking at the chat
            # transcript, not at a status code.
            handler = await self._delegate(request)
            async for chunk in handler.handle_stream(request):
                yield chunk

        async def handle_poll(self, request):
            handler = await self._delegate(request)
            return await handler.handle_poll(request)

    register_auth_routes(
        app,
        accounts=accounts,
        directory=directory,
        session_cookie=SESSION_COOKIE,
        session_ttl_hours=SESSION_TTL_HOURS,
        secure_cookies=SECURE_COOKIES,
        user_resolver=resolver,
        platform_admin_emails=ADMIN_EMAILS,
    )

    register_portal_routes(
        app,
        directory=directory,
        generation_store=generations,
        user_resolver=resolver,
        runtime_for=platform.runtime_for,
        agent_memory=platform.memory,
        conversation_store=platform.conversations,
        accounts=accounts,
        billing=billing,
        platform_admin_emails=ADMIN_EMAILS,
        default_tenant=DEFAULT_TENANT,
    )

    register_admin_routes(
        app,
        user_resolver=resolver,
        example_store=platform.examples,
        instruction_store=platform.instructions,
        generation_store=platform.generations,
        agent_memory=platform.memory,
        dialect=None,
    )

    @app.get("/health")
    async def health() -> dict:
        """Liveness probe. Intentionally does not touch the database.

        A health check that queries the warehouse turns a slow database into a
        restart loop, which is strictly worse than a slow database.
        """
        return {
            "status": "ok",
            "control_plane": directory is not None,
            "tenants_loaded": len(platform._runtimes),
        }

    @app.on_event("startup")
    async def _startup() -> None:
        if directory is not None:
            try:
                await seed_directory(
                    directory,
                    default_tenant=DEFAULT_TENANT,
                    default_database_url=DB_URL or None,
                    admin_emails=sorted(ADMIN_EMAILS),
                )
            except Exception as exc:
                logger.error("Could not seed the directory: %s", exc, exc_info=True)

        if accounts is not None:
            try:
                generated = await seed_first_admin(
                    accounts,
                    admin_emails=sorted(ADMIN_EMAILS),
                    password=ADMIN_PASSWORD,
                )
                if generated:
                    # Logged once, loudly. A deployment nobody can sign in to is the
                    # failure this exists to prevent, and there is no second chance to
                    # print it -- only the hash is stored.
                    banner = "=" * 66
                    logger.warning(
                        "%s\n  First-run administrator account\n"
                        "    email:    %s\n    password: %s\n"
                        "  Change it after signing in. This is shown only once.\n%s",
                        banner,
                        sorted(ADMIN_EMAILS)[0] if ADMIN_EMAILS else "demo@example.com",
                        generated,
                        banner,
                    )
                await accounts.purge_expired()
            except Exception as exc:
                logger.error("Could not seed the first account: %s", exc, exc_info=True)

        # Warm the default tenant so the first user does not pay for the scan.
        # Backgrounded: a slow warehouse should delay good answers, not startup.
        asyncio.create_task(_warm_default())

    async def _warm_default() -> None:
        try:
            await platform.runtime_for(DEFAULT_TENANT)
        except Exception as exc:
            logger.error("Could not warm the default tenant: %s", exc)

    register_chat_routes(app, TenantDispatchChatHandler())

    return app


app = create_app()
