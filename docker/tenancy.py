"""Postgres-backed control plane: tenants, their users, and everything the
portal shows.

Vanna's library stores are tenant-scoped but *file*-backed -- markdown knowledge,
a JSON catalog, a JSONL generation log. That is the right default for a single
container playing with a demo database. It stops being right the moment there is
more than one tenant, more than one API replica, or an operator who needs to
answer "who is in this tenant and what have they been asking".

So this module puts the control plane in PostgreSQL:

* ``tenants``           -- one row per customer, each bound to its own data source
* ``tenant_users``      -- membership and role, which is what the resolver trusts
* ``starter_questions`` -- per-tenant suggestions shown on an empty chat
* ``saved_queries``     -- a tenant's reusable question/SQL pairs
* ``generations``       -- every question asked, the SQL produced, and its rating

Deliberately a *separate* database from the one being queried. The analytics
connection is read-only by policy (see ``build_sql_runner`` in ``app.py``) and
points at whatever warehouse a tenant owns; writing our own bookkeeping there
would need write credentials on customer data and would put our tables in their
namespace. ``VANNA_APP_DATABASE_URL`` is a different URL on purpose.

Synchronous psycopg2 driven through ``asyncio.to_thread``. The alternative is an
async driver and a second connection library in the image; at control-plane
volumes -- a handful of queries per request, none of them hot -- a thread hop
costs less than the dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("vanna.tenancy")

#: Roles a member can hold. ``admin`` is the only one the API treats as
#: privileged; the split between analyst and viewer is enforced at the tool
#: layer (viewers get read-only chat, no knowledge writes).
ROLES = ("admin", "analyst", "viewer")

#: Tenant ids appear in file paths (the markdown knowledge store uses one
#: directory per tenant) and in SQL. Restricting the character set means neither
#: has to be escaped defensively at every use site.
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}[a-z0-9]$")

SCHEMA = "vanna_app"


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------

DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.tenants (
    id            text PRIMARY KEY,
    name          text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    -- NULL means "use the server default connection". Set per tenant to point
    -- a customer at their own database.
    database_url  text,
    is_active     boolean     NOT NULL DEFAULT true,
    daily_quota   integer,
    max_rows      integer,
    -- Write statements for this workspace's admins. Off by default, and only
    -- half the decision: VANNA_ALLOW_WRITES must be set too.
    allow_writes  boolean     NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Existing deployments predate the column; adding it here rather than in a
-- migration file keeps the whole schema in one readable place, and IF NOT
-- EXISTS makes it idempotent on every boot.
ALTER TABLE {SCHEMA}.tenants
    ADD COLUMN IF NOT EXISTS allow_writes boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS {SCHEMA}.tenant_users (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES {SCHEMA}.tenants(id) ON DELETE CASCADE,
    -- Always stored lowercased by the application, so the unique constraint
    -- below actually prevents Ada@x.com and ada@x.com being two members.
    email        text        NOT NULL,
    full_name    text        NOT NULL DEFAULT '',
    role         text        NOT NULL DEFAULT 'analyst',
    is_active    boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    UNIQUE (tenant_id, email),
    CONSTRAINT tenant_users_role_check CHECK (role IN ('admin', 'analyst', 'viewer'))
);

CREATE TABLE IF NOT EXISTS {SCHEMA}.starter_questions (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  text        NOT NULL REFERENCES {SCHEMA}.tenants(id) ON DELETE CASCADE,
    question   text        NOT NULL,
    sort_order integer     NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Credentials. Identity is global; `tenant_users` remains the membership-and-role
-- table, so one account can belong to several workspaces with a different role in each.
CREATE TABLE IF NOT EXISTS {SCHEMA}.users (
    email         text        PRIMARY KEY,
    password_hash text        NOT NULL,
    full_name     text        NOT NULL DEFAULT '',
    is_active     boolean     NOT NULL DEFAULT true,
    -- Forces a change on next login. Set when an admin issues a temporary password.
    must_change   boolean     NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Sessions are keyed by the SHA-256 of the token, never the token. A dump of this table
-- therefore yields nothing replayable -- unlike SQL Chat, which looks sessions up by the
-- raw value and hands over every live session with the database.
CREATE TABLE IF NOT EXISTS {SCHEMA}.sessions (
    token_hash text        PRIMARY KEY,
    email      text        NOT NULL REFERENCES {SCHEMA}.users(email) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    user_agent text        NOT NULL DEFAULT '',
    ip         text        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS sessions_email_idx ON {SCHEMA}.sessions (email);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON {SCHEMA}.sessions (expires_at);

-- Machine credentials: the CLI and `vanna mcp`, which cannot hold a cookie. Separate
-- from sessions so revoking a laptop does not sign out a scheduled job, and so the two
-- can have very different lifetimes.
CREATE TABLE IF NOT EXISTS {SCHEMA}.api_tokens (
    token_hash   text        PRIMARY KEY,
    email        text        NOT NULL REFERENCES {SCHEMA}.users(email) ON DELETE CASCADE,
    name         text        NOT NULL DEFAULT '',
    expires_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);

CREATE INDEX IF NOT EXISTS api_tokens_email_idx ON {SCHEMA}.api_tokens (email);

-- What a workspace pays for. Attached to the tenant rather than the user because the
-- quota is enforced per workspace; billing a person while metering a workspace would
-- disagree the first time a workspace has two members.
--
-- Rows accumulate: a renewal or plan change inserts, it does not update, so the history
-- reads correctly. The current subscription is the newest row.
CREATE TABLE IF NOT EXISTS {SCHEMA}.subscriptions (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES {SCHEMA}.tenants(id) ON DELETE CASCADE,
    -- Names a plan in vanna.core.billing.PLANS. Deliberately not a foreign key: plans
    -- are code constants that ship with a release, so there is no table to point at.
    plan         text        NOT NULL DEFAULT 'free',
    status       text        NOT NULL DEFAULT 'active',
    starts_at    timestamptz NOT NULL DEFAULT now(),
    -- NULL means open-ended, which is how an enterprise agreement with no end date is
    -- expressed. An expired subscription falls back to free, never to zero.
    expires_at   timestamptz,
    cancelled_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS subscriptions_tenant_idx
    ON {SCHEMA}.subscriptions (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS {SCHEMA}.payments (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES {SCHEMA}.tenants(id) ON DELETE CASCADE,
    provider     text        NOT NULL DEFAULT 'manual',
    -- The provider's own identifier for this payment, and the reason this table is
    -- safe to write from a webhook: UNIQUE means a replayed delivery cannot bill the
    -- same payment twice or extend a subscription twice.
    provider_ref text        NOT NULL UNIQUE,
    -- Integer cents. Money in a float is a rounding bug waiting for a large invoice.
    amount_cents integer     NOT NULL DEFAULT 0,
    currency     text        NOT NULL DEFAULT 'usd',
    status       text        NOT NULL DEFAULT 'succeeded',
    description  text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payments_tenant_idx
    ON {SCHEMA}.payments (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS {SCHEMA}.conversations (
    id         text        PRIMARY KEY,
    tenant_id  text        NOT NULL,
    -- Owner. A conversation is one person's working notes, so it is scoped by
    -- user as well as tenant -- unlike saved queries and dashboards, which are
    -- deliberately shared across the workspace.
    user_id    text        NOT NULL,
    title      text        NOT NULL DEFAULT '',
    -- The whole Conversation model, messages included. Read and written as one
    -- unit by the agent on every turn, so a table per message would buy a join
    -- and cost a write amplification.
    document   jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- The sidebar reads "mine, newest first" and nothing else.
CREATE INDEX IF NOT EXISTS conversations_owner_idx
    ON {SCHEMA}.conversations (tenant_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS {SCHEMA}.dashboards (
    id         text        PRIMARY KEY,
    tenant_id  text        NOT NULL REFERENCES {SCHEMA}.tenants(id) ON DELETE CASCADE,
    title      text        NOT NULL,
    -- The whole document as JSON rather than a table per tile. Tiles are read
    -- and written together, always, and a normalised layout would buy nothing
    -- but a join and a migration every time a tile grows a field.
    document   jsonb       NOT NULL,
    created_by text        NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dashboards_tenant_idx
    ON {SCHEMA}.dashboards (tenant_id, title);

CREATE TABLE IF NOT EXISTS {SCHEMA}.saved_queries (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  text        NOT NULL REFERENCES {SCHEMA}.tenants(id) ON DELETE CASCADE,
    created_by text        NOT NULL DEFAULT '',
    title      text        NOT NULL,
    question   text        NOT NULL DEFAULT '',
    sql        text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Not a foreign key to tenants. Generations are an audit trail: deleting a
-- tenant must not silently erase the record of what was asked under it.
CREATE TABLE IF NOT EXISTS {SCHEMA}.generations (
    id                    text        PRIMARY KEY,
    tenant_id             text        NOT NULL,
    data_source_id        text        NOT NULL DEFAULT 'default',
    user_id               text        NOT NULL DEFAULT '',
    conversation_id       text        NOT NULL DEFAULT '',
    request_id            text        NOT NULL DEFAULT '',
    question              text        NOT NULL DEFAULT '',
    sql                   text        NOT NULL DEFAULT '',
    status                text        NOT NULL DEFAULT 'valid',
    error                 text,
    error_kind            text,
    row_count             integer,
    truncated             boolean     NOT NULL DEFAULT false,
    execution_ms          double precision,
    repair_attempts       integer     NOT NULL DEFAULT 0,
    model                 text,
    prompt_tokens         integer,
    completion_tokens     integer,
    cost_usd              double precision,
    retrieved_example_ids jsonb       NOT NULL DEFAULT '[]'::jsonb,
    retrieved_table_names jsonb       NOT NULL DEFAULT '[]'::jsonb,
    retrieval_strategy    text,
    feedback              text,
    feedback_comment      text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    metadata              jsonb       NOT NULL DEFAULT '{{}}'::jsonb
);

-- The history view reads "this tenant, newest first" and nothing else, so one
-- composite index serves it exactly.
CREATE INDEX IF NOT EXISTS generations_tenant_created_idx
    ON {SCHEMA}.generations (tenant_id, created_at DESC);
-- Feedback arrives keyed by request_id, well after the row was written.
CREATE INDEX IF NOT EXISTS generations_request_idx
    ON {SCHEMA}.generations (tenant_id, request_id);
CREATE INDEX IF NOT EXISTS saved_queries_tenant_idx
    ON {SCHEMA}.saved_queries (tenant_id, created_at DESC);
"""


def _admin_url(url: str) -> str:
    """Same server, but pointed at the always-present ``postgres`` database.

    Needed because ``CREATE DATABASE`` cannot run from inside the database being
    created, and the target may not exist yet on a first boot.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/") or "postgres"


def ensure_database(url: str) -> None:
    """Create the control-plane database if it is missing.

    Runs before the pool opens. A first boot against a fresh PostgreSQL should
    not require an operator to hand-create a database, and ``CREATE DATABASE``
    is not transactional, hence the explicit autocommit.
    """
    name = _database_name(url)
    try:
        conn = psycopg2.connect(url, connect_timeout=10)
        conn.close()
        return
    except psycopg2.OperationalError as exc:
        # Only "database does not exist" is recoverable here. A bad password or
        # an unreachable host must surface as itself rather than being retried
        # as a creation problem.
        if "does not exist" not in str(exc):
            raise

    logger.info("Control-plane database %r missing -- creating it", name)
    conn = psycopg2.connect(_admin_url(url), connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # psycopg2 cannot parameterise an identifier; the name comes from
            # our own configured URL, and quote_ident is the escaping path.
            cur.execute(f'CREATE DATABASE "{name}"')
    except psycopg2.errors.DuplicateDatabase:
        pass  # another replica won the race, which is fine
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Connection handling
# ----------------------------------------------------------------------


class AppDatabase:
    """Thin pooled wrapper over the control-plane database.

    Every public method is async and does its blocking work in a worker thread,
    so a slow control-plane query cannot stall the event loop that is streaming
    someone else's answer.
    """

    def __init__(self, url: str, *, minconn: int = 1, maxconn: int = 8) -> None:
        self.url = url
        ensure_database(url)
        self._pool = ThreadedConnectionPool(minconn, maxconn, url, connect_timeout=10)

    # -- plumbing ------------------------------------------------------

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _run(self, sql: str, params: Sequence[Any] = (), *, fetch: str = "none") -> Any:
        with self._connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                if fetch == "one":
                    row = cur.fetchone()
                    return dict(row) if row else None
                if fetch == "rowcount":
                    return cur.rowcount
                return None

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._run, sql, params, fetch="all")

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self._run, sql, params, fetch="one")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await asyncio.to_thread(self._run, sql, params, fetch="rowcount")

    def migrate(self) -> None:
        """Apply the schema. Idempotent -- safe on every boot."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(DDL)
        logger.info("Control-plane schema ready in %s", _database_name(self.url))

    def close(self) -> None:
        self._pool.closeall()


# ----------------------------------------------------------------------
# Directory
# ----------------------------------------------------------------------


def _row_to_tenant(row: Dict[str, Any], *, include_url: bool = False) -> Dict[str, Any]:
    """Serialise a tenant row for the API.

    ``database_url`` carries credentials, so it is omitted unless an admin route
    explicitly asks for it. ``data_source`` is the safe summary everyone can see.
    """
    out = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "is_active": row["is_active"],
        "daily_quota": row["daily_quota"],
        "allow_writes": row.get("allow_writes", False),
        "max_rows": row["max_rows"],
        "data_source": describe_data_source(row.get("database_url")),
        "created_at": _iso(row.get("created_at")),
    }
    if include_url:
        out["database_url"] = row.get("database_url")
    return out


def describe_data_source(url: Optional[str]) -> str:
    """Human-readable, credential-free label for a connection string.

    ``postgresql://vanna:hunter2@db_postgres:5432/ecommerce`` becomes
    ``postgresql://db_postgres/ecommerce``. Shown in the UI so a user can tell
    which database they are querying without ever being shown a password.
    """
    if not url:
        return "server default"
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        name = parts.path.lstrip("/") or "?"
        return f"{parts.scheme}://{host}/{name}"
    except Exception:
        return "configured"


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class Directory:
    """Reads and writes the tenant/user directory."""

    def __init__(self, db: AppDatabase) -> None:
        self.db = db

    # -- tenants -------------------------------------------------------

    async def list_tenants(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = f"SELECT * FROM {SCHEMA}.tenants"
        if active_only:
            sql += " WHERE is_active"
        sql += " ORDER BY name"
        return [_row_to_tenant(r) for r in await self.db.fetch_all(sql)]

    async def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.tenants WHERE id = %s", (tenant_id,)
        )

    async def count_tenants(self) -> int:
        row = await self.db.fetch_one(f"SELECT count(*) AS n FROM {SCHEMA}.tenants")
        return int(row["n"]) if row else 0

    async def create_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str = "",
        database_url: Optional[str] = None,
        daily_quota: Optional[int] = None,
        max_rows: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not TENANT_ID_RE.match(tenant_id):
            raise ValueError(
                "Tenant id must be 3-40 characters of lowercase letters, digits, "
                "hyphen or underscore, and start and end with a letter or digit."
            )
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.tenants
                    (id, name, description, database_url, daily_quota, max_rows)
                VALUES (%s, %s, %s, %s, %s, %s)""",
            (tenant_id, name, description, database_url or None, daily_quota, max_rows),
        )
        return await self.get_tenant(tenant_id)  # type: ignore[return-value]

    async def update_tenant(self, tenant_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = ("name", "description", "database_url", "is_active",
                   "daily_quota", "max_rows", "allow_writes")
        sets, params = [], []
        for key in allowed:
            if key in changes:
                sets.append(f"{key} = %s")
                params.append(changes[key] or None if key == "database_url" else changes[key])
        if not sets:
            return await self.get_tenant(tenant_id)
        sets.append("updated_at = now()")
        params.append(tenant_id)
        await self.db.execute(
            f"UPDATE {SCHEMA}.tenants SET {', '.join(sets)} WHERE id = %s", params
        )
        return await self.get_tenant(tenant_id)

    async def delete_tenant(self, tenant_id: str) -> bool:
        return bool(
            await self.db.execute(f"DELETE FROM {SCHEMA}.tenants WHERE id = %s", (tenant_id,))
        )

    # -- users ---------------------------------------------------------

    async def list_users(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT id, tenant_id, email, full_name, role, is_active,
                       created_at, last_seen_at
                  FROM {SCHEMA}.tenant_users
                 WHERE tenant_id = %s
                 ORDER BY role, email""",
            (tenant_id,),
        )
        for row in rows:
            row["id"] = str(row["id"])
            row["created_at"] = _iso(row["created_at"])
            row["last_seen_at"] = _iso(row["last_seen_at"])
        return rows

    async def get_member(self, tenant_id: str, email: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.tenant_users WHERE tenant_id = %s AND email = %s",
            (tenant_id, email.strip().lower()),
        )

    async def tenants_for_email(self, email: str) -> List[str]:
        """Every tenant this address belongs to -- powers the tenant switcher."""
        rows = await self.db.fetch_all(
            f"""SELECT tenant_id FROM {SCHEMA}.tenant_users
                 WHERE email = %s AND is_active ORDER BY tenant_id""",
            (email.strip().lower(),),
        )
        return [r["tenant_id"] for r in rows]

    async def add_user(
        self,
        tenant_id: str,
        email: str,
        *,
        full_name: str = "",
        role: str = "analyst",
    ) -> Dict[str, Any]:
        if role not in ROLES:
            raise ValueError(f"Role must be one of {', '.join(ROLES)}")
        email = email.strip().lower()
        if "@" not in email:
            raise ValueError("A valid email address is required")
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.tenant_users (tenant_id, email, full_name, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, email)
                DO UPDATE SET full_name = EXCLUDED.full_name,
                              role      = EXCLUDED.role,
                              is_active = true""",
            (tenant_id, email, full_name, role),
        )
        return await self.get_member(tenant_id, email)  # type: ignore[return-value]

    async def update_user(
        self, tenant_id: str, user_id: str, changes: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        allowed = ("full_name", "role", "is_active")
        sets, params = [], []
        for key in allowed:
            if key in changes:
                if key == "role" and changes[key] not in ROLES:
                    raise ValueError(f"Role must be one of {', '.join(ROLES)}")
                sets.append(f"{key} = %s")
                params.append(changes[key])
        if not sets:
            return None
        params.extend([tenant_id, user_id])
        await self.db.execute(
            f"""UPDATE {SCHEMA}.tenant_users SET {', '.join(sets)}
                 WHERE tenant_id = %s AND id = %s""",
            params,
        )
        return await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.tenant_users WHERE tenant_id = %s AND id = %s",
            (tenant_id, user_id),
        )

    async def remove_user(self, tenant_id: str, user_id: str) -> bool:
        return bool(
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.tenant_users WHERE tenant_id = %s AND id = %s",
                (tenant_id, user_id),
            )
        )

    async def count_admins(self, tenant_id: str) -> int:
        row = await self.db.fetch_one(
            f"""SELECT count(*) AS n FROM {SCHEMA}.tenant_users
                 WHERE tenant_id = %s AND role = 'admin' AND is_active""",
            (tenant_id,),
        )
        return int(row["n"]) if row else 0

    async def touch_last_seen(self, tenant_id: str, email: str) -> None:
        """Best-effort activity stamp. Never allowed to fail a request."""
        try:
            await self.db.execute(
                f"""UPDATE {SCHEMA}.tenant_users SET last_seen_at = now()
                     WHERE tenant_id = %s AND email = %s""",
                (tenant_id, email),
            )
        except Exception as exc:  # pragma: no cover - diagnostics only
            logger.debug("last_seen update failed: %s", exc)

    # -- starters ------------------------------------------------------

    async def list_starters(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT id, question, sort_order FROM {SCHEMA}.starter_questions
                 WHERE tenant_id = %s ORDER BY sort_order, created_at""",
            (tenant_id,),
        )
        for row in rows:
            row["id"] = str(row["id"])
        return rows

    async def add_starter(self, tenant_id: str, question: str, sort_order: int = 0) -> None:
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.starter_questions (tenant_id, question, sort_order)
                VALUES (%s, %s, %s)""",
            (tenant_id, question, sort_order),
        )

    async def delete_starter(self, tenant_id: str, starter_id: str) -> bool:
        return bool(
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.starter_questions WHERE tenant_id = %s AND id = %s",
                (tenant_id, starter_id),
            )
        )

    # -- saved queries -------------------------------------------------

    async def list_saved(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT id, title, question, sql, created_by, created_at
                  FROM {SCHEMA}.saved_queries
                 WHERE tenant_id = %s ORDER BY created_at DESC""",
            (tenant_id,),
        )
        for row in rows:
            row["id"] = str(row["id"])
            row["created_at"] = _iso(row["created_at"])
        return rows

    async def save_query(
        self, tenant_id: str, *, title: str, sql: str, question: str = "", created_by: str = ""
    ) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            f"""INSERT INTO {SCHEMA}.saved_queries (tenant_id, title, question, sql, created_by)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, title, question, sql, created_by, created_at""",
            (tenant_id, title, question, sql, created_by),
        )
        row["id"] = str(row["id"])  # type: ignore[index]
        row["created_at"] = _iso(row["created_at"])  # type: ignore[index]
        return row  # type: ignore[return-value]

    async def delete_saved(self, tenant_id: str, saved_id: str) -> bool:
        return bool(
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.saved_queries WHERE tenant_id = %s AND id = %s",
                (tenant_id, saved_id),
            )
        )

    # -- dashboards ----------------------------------------------------

    async def list_dashboards(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT id, title, document, created_by, created_at, updated_at
                  FROM {SCHEMA}.dashboards
                 WHERE tenant_id = %s ORDER BY title""",
            (tenant_id,),
        )
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
            row["updated_at"] = _iso(row["updated_at"])
        return rows

    async def get_dashboard(
        self, tenant_id: str, dashboard_id: str
    ) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.dashboards WHERE tenant_id = %s AND id = %s",
            (tenant_id, dashboard_id),
        )

    async def save_dashboard(
        self, tenant_id: str, document: Dict[str, Any], *, created_by: str = ""
    ) -> Dict[str, Any]:
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.dashboards
                    (id, tenant_id, title, document, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET title = EXCLUDED.title,
                        document = EXCLUDED.document,
                        updated_at = now()""",
            (
                document["id"],
                tenant_id,
                document.get("title", "Untitled"),
                json.dumps(document),
                created_by,
            ),
        )
        return await self.get_dashboard(tenant_id, document["id"])  # type: ignore[return-value]

    async def delete_dashboard(self, tenant_id: str, dashboard_id: str) -> bool:
        return bool(
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.dashboards WHERE tenant_id = %s AND id = %s",
                (tenant_id, dashboard_id),
            )
        )

    # -- usage ---------------------------------------------------------

    async def tenant_usage(self, tenant_id: str, *, days: int = 30) -> Dict[str, Any]:
        """Headline numbers for one tenant, for the admin overview."""
        row = await self.db.fetch_one(
            f"""SELECT count(*)                                             AS questions,
                       count(*) FILTER (WHERE status IN ('valid','empty'))  AS succeeded,
                       count(*) FILTER (WHERE feedback = 'positive')        AS liked,
                       count(*) FILTER (WHERE feedback = 'negative')        AS disliked,
                       count(DISTINCT user_id)                              AS active_users,
                       max(created_at)                                      AS last_activity
                  FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND created_at > now() - make_interval(days => %s)""",
            (tenant_id, days),
        ) or {}
        members = await self.db.fetch_one(
            f"SELECT count(*) AS n FROM {SCHEMA}.tenant_users WHERE tenant_id = %s",
            (tenant_id,),
        )
        return {
            "tenant_id": tenant_id,
            "window_days": days,
            "questions": int(row.get("questions") or 0),
            "succeeded": int(row.get("succeeded") or 0),
            "liked": int(row.get("liked") or 0),
            "disliked": int(row.get("disliked") or 0),
            "active_users": int(row.get("active_users") or 0),
            "members": int((members or {}).get("n") or 0),
            "last_activity": _iso(row.get("last_activity")),
        }


# ----------------------------------------------------------------------
# Generation store
# ----------------------------------------------------------------------


class PostgresGenerationStore:
    """``GenerationStore`` backed by the control-plane database.

    Implements the same contract as ``LocalGenerationStore`` -- see
    ``vanna.core.generation.base`` -- but shared across replicas and queryable,
    which is what turns "we log generations" into an actual history view.

    Writes are on the request path. The contract there is explicit that a
    failure must never propagate: an analytics write that can kill a user's
    answer has inverted its own cost/benefit. Every method below therefore
    degrades to a no-op and logs.
    """

    def __init__(self, db: AppDatabase) -> None:
        self.db = db

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _tenant(context: Any) -> str:
        return getattr(context, "tenant_id", None) or "default"

    @staticmethod
    def _to_model(row: Dict[str, Any]):
        from vanna.core.generation import SqlGeneration

        return SqlGeneration(
            id=row["id"],
            tenant_id=row["tenant_id"],
            data_source_id=row["data_source_id"],
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
            request_id=row["request_id"],
            question=row["question"],
            sql=row["sql"],
            status=row["status"],
            error=row["error"],
            error_kind=row["error_kind"],
            row_count=row["row_count"],
            truncated=row["truncated"],
            execution_ms=row["execution_ms"],
            repair_attempts=row["repair_attempts"],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cost_usd=row["cost_usd"],
            retrieved_example_ids=row["retrieved_example_ids"] or [],
            retrieved_table_names=row["retrieved_table_names"] or [],
            retrieval_strategy=row["retrieval_strategy"],
            feedback=row["feedback"],
            feedback_comment=row["feedback_comment"],
            created_at=row["created_at"],
            metadata=row["metadata"] or {},
        )

    # -- GenerationStore ----------------------------------------------

    async def record(self, context: Any, generation: Any) -> Any:
        generation.tenant_id = self._tenant(context)
        if not generation.user_id:
            generation.user_id = getattr(getattr(context, "user", None), "id", "") or ""
        if not generation.conversation_id:
            generation.conversation_id = getattr(context, "conversation_id", "") or ""
        if not generation.request_id:
            generation.request_id = getattr(context, "request_id", "") or ""

        try:
            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.generations (
                        id, tenant_id, data_source_id, user_id, conversation_id,
                        request_id, question, sql, status, error, error_kind,
                        row_count, truncated, execution_ms, repair_attempts,
                        model, prompt_tokens, completion_tokens, cost_usd,
                        retrieved_example_ids, retrieved_table_names,
                        retrieval_strategy, feedback, feedback_comment,
                        created_at, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING""",
                (
                    generation.id,
                    generation.tenant_id,
                    generation.data_source_id,
                    generation.user_id,
                    generation.conversation_id,
                    generation.request_id,
                    generation.question,
                    generation.sql,
                    str(getattr(generation.status, "value", generation.status)),
                    generation.error,
                    generation.error_kind,
                    generation.row_count,
                    generation.truncated,
                    generation.execution_ms,
                    generation.repair_attempts,
                    generation.model,
                    generation.prompt_tokens,
                    generation.completion_tokens,
                    generation.cost_usd,
                    json.dumps(list(generation.retrieved_example_ids or [])),
                    json.dumps(list(generation.retrieved_table_names or [])),
                    generation.retrieval_strategy,
                    getattr(generation.feedback, "value", generation.feedback),
                    generation.feedback_comment,
                    generation.created_at,
                    json.dumps(generation.metadata or {}, default=str),
                ),
            )
        except Exception as exc:
            logger.warning("Could not record generation: %s", exc)
        return generation

    async def get(self, context: Any, generation_id: str):
        row = await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.generations WHERE tenant_id = %s AND id = %s",
            (self._tenant(context), generation_id),
        )
        return self._to_model(row) if row else None

    async def find_by_request(self, context: Any, request_id: str) -> List[Any]:
        rows = await self.db.fetch_all(
            f"""SELECT * FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND request_id = %s
                 ORDER BY created_at""",
            (self._tenant(context), request_id),
        )
        return [self._to_model(r) for r in rows]

    async def set_feedback(
        self, context: Any, request_id: str, feedback: Any, comment: Optional[str] = None
    ) -> int:
        try:
            return await self.db.execute(
                f"""UPDATE {SCHEMA}.generations
                       SET feedback = %s, feedback_comment = %s
                     WHERE tenant_id = %s AND request_id = %s""",
                (
                    getattr(feedback, "value", feedback),
                    comment,
                    self._tenant(context),
                    request_id,
                ),
            )
        except Exception as exc:
            logger.warning("Could not record feedback: %s", exc)
            return 0

    async def list_recent(
        self,
        context: Any,
        *,
        limit: int = 100,
        status: Any = None,
        since: Optional[datetime] = None,
    ) -> List[Any]:
        sql = f"SELECT * FROM {SCHEMA}.generations WHERE tenant_id = %s"
        params: List[Any] = [self._tenant(context)]
        if status is not None:
            sql += " AND status = %s"
            params.append(getattr(status, "value", status))
        if since is not None:
            sql += " AND created_at >= %s"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        return [self._to_model(r) for r in await self.db.fetch_all(sql, params)]

    async def stats(self, context: Any, *, since: Optional[datetime] = None):
        from vanna.core.generation import GenerationStats

        sql = f"""
            SELECT count(*)                                                AS total,
                   count(*) FILTER (WHERE status = 'valid')                AS valid,
                   count(*) FILTER (WHERE status = 'invalid')              AS invalid,
                   count(*) FILTER (WHERE status = 'empty')                AS empty,
                   count(*) FILTER (WHERE status = 'rejected_by_policy')   AS rejected,
                   count(*) FILTER (WHERE status = 'timeout')              AS timed_out,
                   count(*) FILTER (WHERE feedback = 'positive')           AS positive,
                   count(*) FILTER (WHERE feedback = 'negative')           AS negative,
                   count(*) FILTER (WHERE repair_attempts > 0)             AS repaired,
                   coalesce(sum(cost_usd), 0)                              AS cost,
                   coalesce(avg(execution_ms), 0)                          AS avg_ms
              FROM {SCHEMA}.generations
             WHERE tenant_id = %s"""
        params: List[Any] = [self._tenant(context)]
        if since is not None:
            sql += " AND created_at >= %s"
            params.append(since)

        row = await self.db.fetch_one(sql, params) or {}
        total = int(row.get("total") or 0)
        return GenerationStats(
            total=total,
            valid=int(row.get("valid") or 0),
            invalid=int(row.get("invalid") or 0),
            empty=int(row.get("empty") or 0),
            rejected_by_policy=int(row.get("rejected") or 0),
            timeout=int(row.get("timed_out") or 0),
            positive_feedback=int(row.get("positive") or 0),
            negative_feedback=int(row.get("negative") or 0),
            total_cost_usd=float(row.get("cost") or 0.0),
            avg_execution_ms=float(row.get("avg_ms") or 0.0),
            repair_rate=(int(row.get("repaired") or 0) / total) if total else 0.0,
        )

    async def promotable(self, context: Any, *, limit: int = 50) -> List[Any]:
        rows = await self.db.fetch_all(
            f"""SELECT * FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND status = 'valid'
                   AND feedback = 'positive' AND length(btrim(sql)) > 0
                 ORDER BY created_at DESC
                 LIMIT %s""",
            (self._tenant(context), limit),
        )
        return [self._to_model(r) for r in rows]

    # -- portal helper -------------------------------------------------

    async def history(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        user_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """History rows shaped for the UI, without going through the model.

        The portal shows a table, not domain objects, and wants server-side
        filtering. Reusing ``list_recent`` would mean fetching everything and
        filtering in Python.
        """
        sql = f"""SELECT id, question, sql, status, error, row_count, execution_ms,
                         feedback, user_id, conversation_id, request_id, created_at
                    FROM {SCHEMA}.generations WHERE tenant_id = %s"""
        params: List[Any] = [tenant_id]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        if search:
            sql += " AND (question ILIKE %s OR sql ILIKE %s)"
            params.extend([f"%{search}%", f"%{search}%"])
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(min(max(limit, 1), 500))

        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
        return rows


# ----------------------------------------------------------------------
# Conversation store
# ----------------------------------------------------------------------


#: Longest auto-generated thread title. Long enough to tell two questions
#: apart in a sidebar, short enough not to wrap.
_TITLE_LENGTH = 60

#: What an untitled thread is called. Recognised on write so a conversation
#: saved before its first question still gets a real title later.
PLACEHOLDER_TITLE = "New conversation"


def title_from(text: str) -> str:
    """A thread title derived from its first question.

    SQL Chat titles threads with the creation time, which produces a sidebar of
    timestamps nobody can navigate. The first question is what the person was
    actually doing, and it is already in hand.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return PLACEHOLDER_TITLE
    if len(cleaned) <= _TITLE_LENGTH:
        return cleaned
    return cleaned[: _TITLE_LENGTH - 1].rstrip() + "…"


class PostgresConversationStore:
    """``ConversationStore`` backed by the control plane.

    Implements the interface in ``vanna.core.storage`` -- which the agent
    already calls on every turn -- so threads survive a restart with no change
    to the agent at all. The in-memory store it replaces was the only reason
    they did not.

    Ownership is enforced on read, not just on write: ``get_conversation``
    filters by user, so one person's thread id is useless to another even
    inside the same tenant.
    """

    def __init__(self, db: AppDatabase) -> None:
        self.db = db

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _tenant(user) -> str:
        return getattr(user, "tenant_id", None) or "default"

    @staticmethod
    def _to_model(row: Dict[str, Any]):
        from vanna.core.storage import Conversation

        return Conversation.model_validate(row["document"])

    async def _write(self, conversation) -> None:
        document = conversation.model_dump(mode="json")

        # The agent writes the conversation once before the user's message is
        # attached, so latching the title on the first write would leave every
        # thread called "New conversation" forever. The placeholder is treated
        # as "not titled yet" and recomputed until a real question exists; a
        # title someone typed is never overwritten.
        existing = (conversation.metadata or {}).get("title") or ""
        first_question = next(
            (m.content for m in conversation.messages if m.role == "user" and m.content),
            "",
        )
        title = (
            existing
            if existing and existing != PLACEHOLDER_TITLE
            else title_from(first_question)
        )
        # Keep the title inside the document too, so a restore from the JSONB
        # alone is complete and the column stays a pure index.
        conversation.metadata["title"] = title
        document["metadata"] = conversation.metadata

        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.conversations
                    (id, tenant_id, user_id, title, document)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET title = EXCLUDED.title,
                        document = EXCLUDED.document,
                        updated_at = now()""",
            (
                conversation.id,
                self._tenant(conversation.user),
                conversation.user.id,
                title,
                json.dumps(document, default=str),
            ),
        )

    # -- ConversationStore ---------------------------------------------

    async def create_conversation(self, conversation_id: str, user, initial_message: str):
        from vanna.core.storage import Conversation, Message

        conversation = Conversation(
            id=conversation_id,
            user=user,
            messages=[Message(role="user", content=initial_message)],
            metadata={"title": title_from(initial_message)},
        )
        await self._write(conversation)
        return conversation

    async def get_conversation(self, conversation_id: str, user):
        row = await self.db.fetch_one(
            f"""SELECT document FROM {SCHEMA}.conversations
                 WHERE id = %s AND tenant_id = %s AND user_id = %s""",
            (conversation_id, self._tenant(user), user.id),
        )
        return self._to_model(row) if row else None

    async def update_conversation(self, conversation) -> None:
        # The chat widget requests its starter UI with an empty message on every
        # mount, which reaches the agent as a turn and would persist a
        # conversation nobody started. Left alone, every page load adds an
        # untitled, empty thread to the sidebar.
        if not any(m.role == "user" and (m.content or "").strip()
                   for m in conversation.messages):
            return

        try:
            await self._write(conversation)
        except Exception as exc:
            # On the request path. Losing a transcript is bad; failing the
            # answer the person is waiting for is worse.
            logger.warning("Could not persist conversation %s: %s", conversation.id, exc)

    async def delete_conversation(self, conversation_id: str, user) -> bool:
        return bool(
            await self.db.execute(
                f"""DELETE FROM {SCHEMA}.conversations
                     WHERE id = %s AND tenant_id = %s AND user_id = %s""",
                (conversation_id, self._tenant(user), user.id),
            )
        )

    async def list_conversations(self, user, limit: int = 50, offset: int = 0) -> List:
        rows = await self.db.fetch_all(
            f"""SELECT document FROM {SCHEMA}.conversations
                 WHERE tenant_id = %s AND user_id = %s
                 ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
            (self._tenant(user), user.id, limit, offset),
        )
        return [self._to_model(row) for row in rows]

    # -- portal helpers ------------------------------------------------

    async def summaries(self, tenant_id: str, user_id: str, limit: int = 50):
        """Titles and timestamps only -- what the sidebar needs.

        Deliberately does not deserialise the documents: a thread list should
        not pay for every message in every thread.
        """
        rows = await self.db.fetch_all(
            f"""SELECT id, title, created_at, updated_at,
                       jsonb_array_length(document->'messages') AS message_count
                  FROM {SCHEMA}.conversations
                 WHERE tenant_id = %s AND user_id = %s
                 ORDER BY updated_at DESC LIMIT %s""",
            (tenant_id, user_id, limit),
        )
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
            row["updated_at"] = _iso(row["updated_at"])
        return rows

    async def rename(self, tenant_id: str, user_id: str, conversation_id: str, title: str) -> bool:
        clean = title_from(title)
        return bool(
            await self.db.execute(
                f"""UPDATE {SCHEMA}.conversations
                       SET title = %s,
                           document = jsonb_set(
                               document, '{{metadata,title}}', to_jsonb(%s::text), true
                           ),
                           updated_at = now()
                     WHERE id = %s AND tenant_id = %s AND user_id = %s""",
                (clean, clean, conversation_id, tenant_id, user_id),
            )
        )


class PostgresDashboardStore:
    """``DashboardStore`` over the control plane.

    The agent's ``save_dashboard`` tool and the portal's Dashboards tab must
    write and read the *same* rows -- otherwise a dashboard the agent reports
    creating never appears in the list, which is exactly what happened when the
    tool was registered against a store nobody was reading.
    """

    def __init__(self, directory: "Directory") -> None:
        self.directory = directory

    async def list(self, tenant_id: str) -> List:
        from vanna.dashboards import Dashboard

        rows = await self.directory.list_dashboards(tenant_id)
        return [Dashboard.model_validate(row["document"]) for row in rows]

    async def get(self, tenant_id: str, dashboard_id: str):
        from vanna.dashboards import Dashboard

        row = await self.directory.get_dashboard(tenant_id, dashboard_id)
        return Dashboard.model_validate(row["document"]) if row else None

    async def save(self, dashboard):
        from vanna.dashboards.store import validated

        dashboard = validated(dashboard)
        await self.directory.save_dashboard(
            dashboard.tenant_id,
            dashboard.to_json_dict(),
            created_by=dashboard.created_by,
        )
        return dashboard

    async def delete(self, tenant_id: str, dashboard_id: str) -> bool:
        return await self.directory.delete_dashboard(tenant_id, dashboard_id)


# ----------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------


async def seed_directory(
    directory: Directory,
    *,
    default_tenant: str,
    default_database_url: Optional[str],
    admin_emails: Sequence[str],
) -> None:
    """Create a first tenant and its admins on an empty directory.

    Only ever runs when there are zero tenants. A deployment that has been
    curated must not have seed rows reappear underneath it, and the check being
    "no tenants at all" rather than "this tenant is missing" is what guarantees
    a deliberately deleted demo tenant stays deleted.
    """
    if await directory.count_tenants() > 0:
        return

    logger.info("Empty directory -- seeding tenant %r", default_tenant)
    await directory.create_tenant(
        default_tenant,
        name=default_tenant.replace("-", " ").replace("_", " ").title(),
        description="Created automatically on first start.",
        database_url=default_database_url,
    )

    seeded_admins = [e for e in admin_emails if e] or ["demo@example.com"]
    for email in seeded_admins:
        await directory.add_user(default_tenant, email, role="admin", full_name="Administrator")

    for order, question in enumerate(
        [
            "Which tables are available and how do they relate?",
            "Show me the 10 most recent records in the largest table.",
            "What are the totals by month for the last year?",
        ]
    ):
        await directory.add_starter(default_tenant, question, order)


def build_app_database() -> Optional[AppDatabase]:
    """Open the control plane, or return None if it is not configured.

    Returning None rather than raising keeps the "just run it" path alive: with
    no ``VANNA_APP_DATABASE_URL`` the stack still serves single-tenant chat, it
    simply has no directory, no history and no saved queries. The portal detects
    that and says so instead of showing empty screens.
    """
    url = os.getenv("VANNA_APP_DATABASE_URL", "").strip()
    if not url:
        logger.warning(
            "VANNA_APP_DATABASE_URL is not set -- running without a control "
            "plane. Tenants, users, history and saved queries are disabled."
        )
        return None
    try:
        db = AppDatabase(url)
        db.migrate()
        return db
    except Exception as exc:
        logger.error("Control-plane database unavailable (%s): %s", describe_data_source(url), exc)
        return None
