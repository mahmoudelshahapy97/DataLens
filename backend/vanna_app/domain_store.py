"""Business domains: a named slice of one database, and the words people use for it.

A domain answers a question the schema cannot. ``invoice.total`` is a numeric
column; that it is *revenue*, that revenue excludes cancelled invoices, and that
"churn" means no order in ninety days are facts about the business that live in
somebody's head until they are written down somewhere the model can read.

Two things this deliberately is not.

**It is not an access control.** Membership is intersected with what the caller
may already read, in :func:`readable_tables_for`. A domain can narrow the tables
in play; it can never add one. A grouping that could widen access would be a
second permission system wearing a friendlier name, and the weaker of two
permission systems is the one that decides.

**It is not the provisioning file.** ``backend/domains/domains.yml`` seeds
workspaces at deploy time and has always been a one-shot script -- there was no
domain entity at request time, no table, and no API. This is that entity.

Scoped per (tenant, data source) rather than per tenant, because a domain names
tables and a table belongs to one database.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from vanna.core.grants import normalize_table

from .db import SCHEMA

logger = logging.getLogger("vanna.domains")


class DomainStore:
    """CRUD for business domains and their table membership."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_domains(
        self, tenant_id: str, *, data_source_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""
            SELECT d.id, d.data_source_id, d.name, d.description, d.terminology,
                   d.is_enabled, d.updated_at,
                   COALESCE(
                       ARRAY_AGG(m.table_key ORDER BY m.table_key)
                       FILTER (WHERE m.table_key IS NOT NULL),
                       '{{}}'
                   ) AS tables
              FROM {SCHEMA}.business_domains d
              LEFT JOIN {SCHEMA}.domain_tables m
                     ON m.tenant_id = d.tenant_id AND m.domain_id = d.id
             WHERE d.tenant_id = %s
               AND (%s::text IS NULL OR d.data_source_id = %s)
             GROUP BY d.id
             ORDER BY lower(d.name)
            """,
            (tenant_id, data_source_id, data_source_id),
        )
        return [self._out(row) for row in rows]

    async def get_domain(self, tenant_id: str, domain_id: str) -> Optional[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""
            SELECT d.id, d.data_source_id, d.name, d.description, d.terminology,
                   d.is_enabled, d.updated_at,
                   COALESCE(
                       ARRAY_AGG(m.table_key ORDER BY m.table_key)
                       FILTER (WHERE m.table_key IS NOT NULL),
                       '{{}}'
                   ) AS tables
              FROM {SCHEMA}.business_domains d
              LEFT JOIN {SCHEMA}.domain_tables m
                     ON m.tenant_id = d.tenant_id AND m.domain_id = d.id
             WHERE d.tenant_id = %s AND d.id = %s
             GROUP BY d.id
            """,
            (tenant_id, domain_id),
        )
        return self._out(rows[0]) if rows else None

    @staticmethod
    def _out(row: Dict[str, Any]) -> Dict[str, Any]:
        terminology = row.get("terminology") or {}
        if isinstance(terminology, str):
            terminology = json.loads(terminology)
        return {
            "id": str(row["id"]),
            "data_source_id": row["data_source_id"],
            "name": row["name"],
            "description": row["description"],
            "terminology": terminology,
            "is_enabled": row["is_enabled"],
            "tables": list(row.get("tables") or []),
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def create(
        self,
        tenant_id: str,
        *,
        data_source_id: str,
        name: str,
        description: str = "",
        terminology: Optional[Dict[str, str]] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            f"""
            INSERT INTO {SCHEMA}.business_domains
                (tenant_id, data_source_id, name, description, terminology, created_by)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (
                tenant_id,
                data_source_id,
                name,
                description,
                json.dumps(terminology or {}),
                created_by,
            ),
        )
        created = await self.get_domain(tenant_id, str(row["id"]))
        assert created is not None
        return created

    async def update(
        self,
        tenant_id: str,
        domain_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        terminology: Optional[Dict[str, str]] = None,
        is_enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Patch semantics: only the fields supplied are touched."""
        sets: List[str] = []
        params: List[Any] = []
        if name is not None:
            sets.append("name = %s")
            params.append(name)
        if description is not None:
            sets.append("description = %s")
            params.append(description)
        if terminology is not None:
            sets.append("terminology = %s::jsonb")
            params.append(json.dumps(terminology))
        if is_enabled is not None:
            sets.append("is_enabled = %s")
            params.append(is_enabled)

        if sets:
            sets.append("updated_at = now()")
            params.extend([tenant_id, domain_id])
            await self.db.execute(
                f"UPDATE {SCHEMA}.business_domains SET {', '.join(sets)} "
                "WHERE tenant_id = %s AND id = %s",
                tuple(params),
            )
        return await self.get_domain(tenant_id, domain_id)

    async def delete(self, tenant_id: str, domain_id: str) -> bool:
        removed = await self.db.execute(
            f"DELETE FROM {SCHEMA}.business_domains WHERE tenant_id = %s AND id = %s",
            (tenant_id, domain_id),
        )
        return bool(removed)

    async def replace_tables(
        self, tenant_id: str, domain_id: str, tables: Sequence[str], *, added_by: Optional[str] = None
    ) -> None:
        """Set membership to exactly *tables*.

        A full replace rather than a merge, because the caller is a screen showing
        the whole list: merging would silently keep a table the administrator had
        just unticked.
        """
        keys = [normalize_table(t) for t in tables if str(t).strip()]

        def run(cursor: Any) -> None:
            cursor.execute(
                f"DELETE FROM {SCHEMA}.domain_tables "
                "WHERE tenant_id = %s AND domain_id = %s",
                (tenant_id, domain_id),
            )
            for key in keys:
                cursor.execute(
                    f"""
                    INSERT INTO {SCHEMA}.domain_tables
                        (tenant_id, domain_id, table_key, added_by)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, domain_id, table_key) DO NOTHING
                    """,
                    (tenant_id, domain_id, key, added_by),
                )

        await self._transact(run)

    async def _transact(self, body: Callable[[Any], None]) -> None:
        def run() -> None:
            with self.db.transaction() as connection:
                with connection.cursor() as cursor:
                    body(cursor)

        await asyncio.to_thread(run)


async def readable_tables_for(
    store: "DomainStore",
    tenant_id: str,
    domain_id: str,
    *,
    readable: Optional[Set[str]],
) -> Optional[Set[str]]:
    """A domain's tables, intersected with what the caller may already read.

    ``readable`` is None when the caller is not under read enforcement, in which
    case membership stands on its own.

    The intersection is the whole reason this function exists rather than being a
    line at each call site. A domain is curation -- somebody grouping tables by
    what they are for -- and curation must not be able to hand out access. If a
    table is in the domain but not in the caller's grants, the grants win.
    """
    domain = await store.get_domain(tenant_id, domain_id)
    if domain is None or not domain["is_enabled"]:
        return None

    members = {normalize_table(t) for t in domain["tables"]}
    if readable is None:
        return members
    return members & {normalize_table(t) for t in readable}
