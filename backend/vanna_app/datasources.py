"""The databases a workspace can query.

``tenants.database_url`` used to be the whole answer: one workspace, one
connection string. This is the registry that makes it a list.

Almost nothing else had to change, because the data source was already a
first-class dimension everywhere it mattered -- ``table_grants``,
``column_grants``, ``grant_policies`` and (since migration 0010) the schema
catalog are all keyed ``(tenant_id, data_source_id, ...)``. A workspace's second
database inherits the entire permission machinery without a translation layer.

Two rules the rest of the system leans on:

**A registered source is the only kind there is.** An id arriving from a browser
is a *preference*, checked here before anything is built from it -- the same
treatment ``x-tenant-id`` gets in ``identity.py``. Nothing downstream re-checks,
so nothing downstream may be handed an unvalidated id.

**There is always exactly one default, or none at all.** A partial unique index
enforces it. "Two defaults" is a state where the answer to "which database?"
depends on row order, which is the kind of bug that reproduces once a week.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .db import SCHEMA
from .secrets import Secret
from .tenancy import describe_data_source

logger = logging.getLogger("vanna.datasources")


class UnknownDataSource(LookupError):
    """A data source that this workspace has not registered.

    Raised rather than falling back to the default. A caller that named a
    database and silently got a different one is worse off than a caller that got
    an error: they would read the answer as being about the database they asked
    for.
    """


class DataSourceRegistry:
    """Which databases a workspace may query, and how to reach them."""

    def __init__(self, db: Any, cipher: Any) -> None:
        self.db = db
        self.cipher = cipher

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_sources(
        self, tenant_id: str, *, include_urls: bool = False
    ) -> List[Dict[str, Any]]:
        """Every registered source. Credentials omitted unless asked for."""
        rows = await self.db.fetch_all(
            f"""
            SELECT data_source_id, database_url, label, is_default, is_active
              FROM {SCHEMA}.tenant_datasources
             WHERE tenant_id = %s AND is_active
             ORDER BY is_default DESC, data_source_id
            """,
            (tenant_id,),
        )
        out = []
        for row in rows:
            item = {
                "data_source_id": row["data_source_id"],
                # Falls back to the derived id, which is already credential-free.
                "label": row["label"] or row["data_source_id"],
                "is_default": row["is_default"],
            }
            if include_urls:
                item["database_url"] = self._decrypt(row["database_url"])
            out.append(item)
        return out

    async def resolve(
        self, tenant_id: str, data_source_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """One source by id, or the workspace default when *data_source_id* is None.

        Returns None when the workspace has registered nothing -- the legacy path,
        where ``tenants.database_url`` is still the answer. Raises
        :class:`UnknownDataSource` when an id was named and is not registered:
        those are different situations and only one of them is an error.
        """
        if data_source_id:
            row = await self.db.fetch_one(
                f"""
                SELECT data_source_id, database_url, label
                  FROM {SCHEMA}.tenant_datasources
                 WHERE tenant_id = %s AND data_source_id = %s AND is_active
                """,
                (tenant_id, data_source_id),
            )
            if row is None:
                raise UnknownDataSource(
                    f"{data_source_id!r} is not a database this workspace can query."
                )
        else:
            row = await self.db.fetch_one(
                f"""
                SELECT data_source_id, database_url, label
                  FROM {SCHEMA}.tenant_datasources
                 WHERE tenant_id = %s AND is_active
                 ORDER BY is_default DESC, data_source_id
                 LIMIT 1
                """,
                (tenant_id,),
            )
            if row is None:
                return None

        return {
            "data_source_id": row["data_source_id"],
            "database_url": self._decrypt(row["database_url"]),
            "label": row["label"] or row["data_source_id"],
        }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def register(
        self,
        tenant_id: str,
        database_url: Any,
        *,
        label: str = "",
        is_default: bool = False,
    ) -> Dict[str, Any]:
        """Add or update one source, deriving its id from the URL."""
        raw = database_url.reveal() if isinstance(database_url, Secret) else str(database_url)
        data_source_id = describe_data_source(raw)

        if is_default:
            # Clear the old default first: the partial unique index would
            # otherwise refuse the insert, and "two defaults" is exactly what it
            # exists to prevent.
            await self.db.execute(
                f"UPDATE {SCHEMA}.tenant_datasources SET is_default = false "
                "WHERE tenant_id = %s AND is_default",
                (tenant_id,),
            )

        await self.db.execute(
            f"""
            INSERT INTO {SCHEMA}.tenant_datasources
                (tenant_id, data_source_id, database_url, label, is_default)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, data_source_id) DO UPDATE SET
                database_url = EXCLUDED.database_url,
                label        = EXCLUDED.label,
                is_default   = EXCLUDED.is_default,
                is_active    = true,
                updated_at   = now()
            """,
            (tenant_id, data_source_id, self.cipher.encrypt(raw), label, is_default),
        )
        return {"data_source_id": data_source_id, "label": label or data_source_id}

    async def remove(self, tenant_id: str, data_source_id: str) -> bool:
        removed = await self.db.execute(
            f"DELETE FROM {SCHEMA}.tenant_datasources "
            "WHERE tenant_id = %s AND data_source_id = %s",
            (tenant_id, data_source_id),
        )
        return bool(removed)

    async def backfill(self, tenant_id: str, database_url: Any) -> Optional[str]:
        """Register a workspace's existing single database, once.

        Runs from the application rather than from migration 0011 because the id
        comes from ``describe_data_source``, whose rules are dialect-specific --
        a file-backed engine has no host, so the path is the whole address. A
        second implementation in SQL would drift from this one the first time
        either changed.

        A no-op when anything is already registered, so it cannot overwrite a
        deliberate configuration.
        """
        raw = database_url.reveal() if isinstance(database_url, Secret) else (database_url or "")
        if not raw:
            # No URL means "use the server default", which is not a workspace
            # database and has nothing to register.
            return None

        existing = await self.db.fetch_one(
            f"SELECT 1 AS present FROM {SCHEMA}.tenant_datasources "
            "WHERE tenant_id = %s LIMIT 1",
            (tenant_id,),
        )
        if existing:
            return None

        registered = await self.register(tenant_id, raw, is_default=True)
        logger.info(
            "Registered %s as the default database for %s",
            registered["data_source_id"],
            tenant_id,
        )
        return registered["data_source_id"]

    # ------------------------------------------------------------------

    def _decrypt(self, stored: Optional[str]) -> Secret:
        return Secret(self.cipher.decrypt(stored) or "")
