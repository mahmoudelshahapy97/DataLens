"""The tenant directory: workspaces, their members, and what each is bound to.

Storage only -- this module knows nothing about HTTP. The rule that a customer's
data stays inside their workspace is expressed here, in the fact that every query
takes a ``tenant_id`` and none of them has a code path that does not.

Two things changed from the original.

**Credentials are sealed.** ``tenants.database_url`` holds a warehouse password.
It is encrypted on write and returned as a :class:`~vanna_app.secrets.Secret`, which
renders as ``***`` in logs, f-strings and tracebacks. Getting at the value takes an
explicit ``.reveal()``, and there are five of those in the whole codebase.

**The tenant list no longer costs a query per tenant.** The admin console asked for
every workspace and then ran two aggregates per row in a Python loop; with a hundred
workspaces that is two hundred sequential round trips to render one page. One
grouped query replaces it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union
from urllib.parse import urlsplit

from .db import SCHEMA, AppDatabase
from .secrets import Cipher, Secret

logger = logging.getLogger("vanna.tenancy")

#: Roles a member can hold. ``admin`` is the only one the API treats as privileged;
#: the split between analyst and viewer is enforced at the tool and route layer.
ROLES = ("admin", "analyst", "viewer")

#: Tenant ids appear in file paths (the markdown knowledge store uses one directory
#: per tenant) and in SQL. Restricting the character set means neither has to be
#: escaped defensively at every use site.
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}[a-z0-9]$")

#: Ids that would collide with a route segment or a reserved word if a workspace
#: ever appears in a path. Cheaper to refuse now than to discover later.
RESERVED_TENANT_IDS = frozenset({
    "admin", "api", "auth", "health", "ready", "metrics", "static", "assets",
    "docs", "openapi", "system", "public", "internal", "null", "undefined",
})


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def describe_data_source(url: Union[str, Secret, None]) -> str:
    """Human-readable, credential-free label for a connection string.

    ``postgresql://vanna:hunter2@db_postgres:5432/ecommerce`` becomes
    ``postgresql://db_postgres/ecommerce``. Shown in the UI so a user can tell which
    database they are querying without ever being shown a password.
    """
    raw = url.reveal() if isinstance(url, Secret) else (url or "")
    if not raw:
        return "server default"
    try:
        parts = urlsplit(raw)

        # File-backed engines have no host, so the host/path split renders them as
        # `duckdb://?/:memory:`. The path is the whole address there.
        if parts.scheme in ("sqlite", "duckdb"):
            path = (parts.netloc + parts.path) or ":memory:"
            return f"{parts.scheme}://{path}"

        host = parts.hostname or "?"
        name = parts.path.lstrip("/") or "?"
        return f"{parts.scheme}://{host}/{name}"
    except Exception:
        return "configured"


class Directory:
    """Reads and writes the tenant/user directory."""

    def __init__(self, db: AppDatabase, cipher: Optional[Cipher] = None) -> None:
        self.db = db
        self.cipher = cipher or Cipher("")

    # -- serialisation -------------------------------------------------

    def _tenant_out(self, row: Dict[str, Any], *, include_url: bool = False) -> Dict[str, Any]:
        """A tenant row shaped for the API.

        ``database_url`` is omitted unless a route explicitly asks; ``data_source``
        is the safe summary everybody can see.
        """
        url = self._url_of(row)
        out = {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "is_active": row["is_active"],
            "daily_quota": row["daily_quota"],
            "max_rows": row["max_rows"],
            "allow_writes": row.get("allow_writes", False),
            # Who has to say yes to a change: the requester, or a second
            # administrator for destructive ones. Defaults to self-approval,
            # since an administrator has already decided which tables are
            # writable at all.
            "write_approval_mode": row.get("write_approval_mode") or "self",
            "allow_byo_key": row.get("allow_byo_key", True),
            "data_source": describe_data_source(url),
            "created_at": _iso(row.get("created_at")),
        }
        if include_url:
            # Still a Secret: a route that needs to *show* it must reveal it
            # deliberately, and none currently does.
            out["database_url"] = url
        return out

    def _url_of(self, row: Optional[Dict[str, Any]]) -> Secret:
        """The decrypted connection URL for a tenant row."""
        if not row:
            return Secret("")
        return Secret(self.cipher.decrypt(row.get("database_url")) or "")

    def _hydrate(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Replace the stored ciphertext with a ``Secret`` on a raw row.

        Every internal caller of ``get_tenant`` reads ``row["database_url"]``; making
        that a ``Secret`` rather than a string is what stops it being interpolated
        into a log line by accident.
        """
        if row is None:
            return None
        row = dict(row)
        row["database_url"] = self._url_of(row)
        return row

    # -- tenants -------------------------------------------------------

    async def list_tenants(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = f"SELECT * FROM {SCHEMA}.tenants"
        if active_only:
            sql += " WHERE is_active"
        sql += " ORDER BY name"
        return [self._tenant_out(row) for row in await self.db.fetch_all(sql)]

    async def list_tenants_with_usage(
        self, *, active_only: bool = False, days: int = 30
    ) -> List[Dict[str, Any]]:
        """Every workspace and its headline numbers, in one query.

        Replaces a loop that ran two aggregates per tenant. The lateral join keeps
        the aggregate scoped to one tenant at a time so the (tenant_id, created_at)
        index is usable, which a single grouped scan over all tenants would not be.
        """
        sql = f"""
            SELECT t.*,
                   coalesce(g.questions, 0)    AS questions,
                   coalesce(g.succeeded, 0)    AS succeeded,
                   coalesce(g.liked, 0)        AS liked,
                   coalesce(g.disliked, 0)     AS disliked,
                   coalesce(g.active_users, 0) AS active_users,
                   g.last_activity,
                   coalesce(m.members, 0)      AS members
              FROM {SCHEMA}.tenants t
              LEFT JOIN LATERAL (
                  SELECT count(*)                                            AS questions,
                         count(*) FILTER (WHERE status IN ('valid','empty')) AS succeeded,
                         count(*) FILTER (WHERE feedback = 'positive')       AS liked,
                         count(*) FILTER (WHERE feedback = 'negative')       AS disliked,
                         count(DISTINCT user_id)                             AS active_users,
                         max(created_at)                                     AS last_activity
                    FROM {SCHEMA}.generations
                   WHERE tenant_id = t.id
                     AND created_at > now() - make_interval(days => %s)
              ) g ON true
              LEFT JOIN LATERAL (
                  SELECT count(*) AS members
                    FROM {SCHEMA}.tenant_users WHERE tenant_id = t.id
              ) m ON true
        """
        if active_only:
            sql += " WHERE t.is_active"
        sql += " ORDER BY t.name"

        out = []
        for row in await self.db.fetch_all(sql, (days,)):
            tenant = self._tenant_out(row)
            tenant["usage"] = {
                "tenant_id": row["id"],
                "window_days": days,
                "questions": int(row["questions"] or 0),
                "succeeded": int(row["succeeded"] or 0),
                "liked": int(row["liked"] or 0),
                "disliked": int(row["disliked"] or 0),
                "active_users": int(row["active_users"] or 0),
                "members": int(row["members"] or 0),
                "last_activity": _iso(row["last_activity"]),
            }
            out.append(tenant)
        return out

    async def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """One tenant's raw row, with ``database_url`` decrypted into a ``Secret``."""
        return self._hydrate(
            await self.db.fetch_one(
                f"SELECT * FROM {SCHEMA}.tenants WHERE id = %s", (tenant_id,)
            )
        )

    async def count_tenants(self) -> int:
        return int(
            await self.db.fetch_value(f"SELECT count(*) FROM {SCHEMA}.tenants", default=0)
        )

    @staticmethod
    def validate_id(tenant_id: str) -> None:
        if not TENANT_ID_RE.match(tenant_id or ""):
            raise ValueError(
                "Workspace id must be 3-40 characters of lowercase letters, digits, "
                "hyphen or underscore, and start and end with a letter or digit."
            )
        if tenant_id in RESERVED_TENANT_IDS:
            raise ValueError(f"{tenant_id!r} is reserved and cannot be a workspace id.")

    async def create_tenant(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str = "",
        database_url: Union[str, Secret, None] = None,
        daily_quota: Optional[int] = None,
        max_rows: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.validate_id(tenant_id)
        raw = database_url.reveal() if isinstance(database_url, Secret) else database_url
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.tenants
                    (id, name, description, database_url, daily_quota, max_rows)
                VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                tenant_id,
                name,
                description,
                self.cipher.encrypt(raw) or None,
                daily_quota,
                max_rows,
            ),
        )
        return await self.get_tenant(tenant_id)  # type: ignore[return-value]

    async def update_tenant(
        self, tenant_id: str, changes: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        allowed = ("name", "description", "database_url", "is_active",
                   "daily_quota", "max_rows", "allow_writes", "allow_byo_key",
                   "write_approval_mode")
        sets, params = [], []
        for key in allowed:
            if key not in changes:
                continue
            value = changes[key]
            if key == "database_url":
                raw = value.reveal() if isinstance(value, Secret) else value
                value = self.cipher.encrypt(raw) or None
            sets.append(f"{key} = %s")
            params.append(value)

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

    async def seal_plaintext_urls(self) -> int:
        """Re-write any plaintext ``database_url`` as ciphertext.

        The backfill migration 0002 cannot do this -- it has no access to the key.
        Run from ``vanna-app seal-secrets``, and idempotent: a sealed value is
        returned unchanged by ``Cipher.encrypt``.
        """
        if not self.cipher.enabled:
            raise RuntimeError("VANNA_SECRET_KEY is not set; nothing can be sealed.")

        rows = await self.db.fetch_all(
            f"SELECT id, database_url FROM {SCHEMA}.tenants WHERE database_url IS NOT NULL"
        )
        sealed = 0
        for row in rows:
            if self.cipher.is_sealed(row["database_url"]):
                continue
            await self.db.execute(
                f"UPDATE {SCHEMA}.tenants SET database_url = %s WHERE id = %s",
                (self.cipher.encrypt(row["database_url"]), row["id"]),
            )
            sealed += 1
        logger.info("Sealed %d plaintext datasource credential(s)", sealed)
        return sealed

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
            (tenant_id, (email or "").strip().lower()),
        )

    async def tenants_for_email(self, email: str) -> List[str]:
        """Every workspace this address belongs to -- powers the switcher."""
        rows = await self.db.fetch_all(
            f"""SELECT tenant_id FROM {SCHEMA}.tenant_users
                 WHERE email = %s AND is_active ORDER BY tenant_id""",
            ((email or "").strip().lower(),),
        )
        return [row["tenant_id"] for row in rows]

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
        email = (email or "").strip().lower()
        if "@" not in email:
            raise ValueError("A valid email address is required")
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.tenant_users (tenant_id, email, full_name, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tenant_id, email)
                DO UPDATE SET full_name = CASE
                                  WHEN EXCLUDED.full_name = '' THEN {SCHEMA}.tenant_users.full_name
                                  ELSE EXCLUDED.full_name END,
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
        return int(
            await self.db.fetch_value(
                f"""SELECT count(*) FROM {SCHEMA}.tenant_users
                     WHERE tenant_id = %s AND role = 'admin' AND is_active""",
                (tenant_id,),
                default=0,
            )
        )

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
            f"""INSERT INTO {SCHEMA}.dashboards (id, tenant_id, title, document, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET title = EXCLUDED.title,
                        document = EXCLUDED.document,
                        updated_at = now()
                -- Scoped so a dashboard id from another workspace cannot be
                -- overwritten by guessing it.
                 WHERE {SCHEMA}.dashboards.tenant_id = EXCLUDED.tenant_id""",
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
        """Headline numbers for one workspace."""
        row = await self.db.fetch_one(
            f"""SELECT count(*)                                            AS questions,
                       count(*) FILTER (WHERE status IN ('valid','empty')) AS succeeded,
                       count(*) FILTER (WHERE feedback = 'positive')       AS liked,
                       count(*) FILTER (WHERE feedback = 'negative')       AS disliked,
                       count(DISTINCT user_id)                             AS active_users,
                       max(created_at)                                     AS last_activity
                  FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND created_at > now() - make_interval(days => %s)""",
            (tenant_id, days),
        ) or {}
        members = await self.db.fetch_value(
            f"SELECT count(*) FROM {SCHEMA}.tenant_users WHERE tenant_id = %s",
            (tenant_id,),
            default=0,
        )
        return {
            "tenant_id": tenant_id,
            "window_days": days,
            "questions": int(row.get("questions") or 0),
            "succeeded": int(row.get("succeeded") or 0),
            "liked": int(row.get("liked") or 0),
            "disliked": int(row.get("disliked") or 0),
            "active_users": int(row.get("active_users") or 0),
            "members": int(members or 0),
            "last_activity": _iso(row.get("last_activity")),
        }

    async def spend(self, tenant_id: str, *, days: int = 30) -> Dict[str, Any]:
        """What this workspace's questions cost.

        The columns have existed since the beginning and nothing populated or read
        them, so "what are we spending per customer" -- the number that decides
        pricing -- was uncollectable.
        """
        row = await self.db.fetch_one(
            f"""SELECT coalesce(sum(cost_usd), 0)          AS cost_usd,
                       coalesce(sum(prompt_tokens), 0)     AS prompt_tokens,
                       coalesce(sum(completion_tokens), 0) AS completion_tokens,
                       count(*)                            AS questions
                  FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND created_at > now() - make_interval(days => %s)""",
            (tenant_id, days),
        ) or {}
        by_model = await self.db.fetch_all(
            f"""SELECT coalesce(model, 'unknown')  AS model,
                       count(*)                    AS questions,
                       coalesce(sum(cost_usd), 0)  AS cost_usd
                  FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND created_at > now() - make_interval(days => %s)
                 GROUP BY 1 ORDER BY 3 DESC""",
            (tenant_id, days),
        )
        return {
            "window_days": days,
            "cost_usd": float(row.get("cost_usd") or 0.0),
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "questions": int(row.get("questions") or 0),
            "by_model": [
                {
                    "model": r["model"],
                    "questions": int(r["questions"]),
                    "cost_usd": float(r["cost_usd"] or 0.0),
                }
                for r in by_model
            ],
        }

    async def purge_tenant_data(self, tenant_id: str) -> Dict[str, int]:
        """Erase everything this workspace generated.

        Tenant deletion deliberately leaves ``generations`` behind so the record of
        what was asked survives -- correct for audit, wrong when somebody exercises
        a right to erasure. This is the explicit, separate operation for that.
        """
        deleted: Dict[str, int] = {}
        for table in ("generations", "conversations", "dashboards", "saved_queries",
                      "audit_events", "admin_audit"):
            deleted[table] = await self.db.execute(
                f"DELETE FROM {SCHEMA}.{table} WHERE tenant_id = %s", (tenant_id,)
            )
        logger.warning("Purged tenant data for %s: %s", tenant_id, deleted)
        return deleted


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
    """Create a first workspace and its admins on an empty directory.

    Only ever runs when there are zero tenants. A deployment that has been curated
    must not have seed rows reappear underneath it, and the check being "no tenants
    at all" rather than "this tenant is missing" is what guarantees a deliberately
    deleted demo workspace stays deleted.
    """
    if await directory.count_tenants() > 0:
        return

    logger.info("Empty directory -- seeding workspace %r", default_tenant)
    await directory.create_tenant(
        default_tenant,
        name=default_tenant.replace("-", " ").replace("_", " ").title(),
        description="Created automatically on first start.",
        database_url=default_database_url,
    )

    for email in [e for e in admin_emails if e] or ["demo@example.com"]:
        await directory.add_user(
            default_tenant, email, role="admin", full_name="Administrator"
        )

    for order, question in enumerate(
        [
            "Which tables are available and how do they relate?",
            "Show me the 10 most recent records in the largest table.",
            "What are the totals by month for the last year?",
        ]
    ):
        await directory.add_starter(default_tenant, question, order)
