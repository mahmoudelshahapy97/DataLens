"""What a workspace grants by default, per role, per connection.

Intent, never permission. Nothing here is read when a caller's access is
resolved: applying a policy writes ordinary rows into ``table_grants`` and
``column_grants``, and those are what the resolver reads. See
``0009_grant_defaults.sql`` for why that indirection is the design rather than an
implementation detail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .db import SCHEMA

logger = logging.getLogger("vanna.grants.policy")


@dataclass
class RolePolicy:
    """One role's default in one workspace, on one connection."""

    role: str
    preset: str = "none"
    apply_to_new_tables: bool = False
    enforce_reads: bool = False
    last_applied_at: Optional[datetime] = None
    last_applied_version: Optional[int] = None

    def as_json(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "preset": self.preset,
            "apply_to_new_tables": self.apply_to_new_tables,
            "enforce_reads": self.enforce_reads,
            "last_applied_at": (
                self.last_applied_at.isoformat() if self.last_applied_at else None
            ),
            "last_applied_version": self.last_applied_version,
        }


def _to_policy(row: Dict[str, Any]) -> RolePolicy:
    return RolePolicy(
        role=row["role"],
        preset=row.get("preset") or "none",
        apply_to_new_tables=bool(row.get("apply_to_new_tables")),
        enforce_reads=bool(row.get("enforce_reads")),
        last_applied_at=row.get("last_applied_at"),
        last_applied_version=row.get("last_applied_version"),
    )


class GrantPolicyStore:
    """Reads and writes ``vanna_app.grant_policies``."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def get(self, tenant_id: str, data_source_id: str) -> Dict[str, RolePolicy]:
        rows = await self.db.fetch_all(
            f"SELECT * FROM {SCHEMA}.grant_policies "
            "WHERE tenant_id = %s AND data_source_id = %s ORDER BY role",
            (tenant_id, data_source_id),
        )
        return {r["role"]: _to_policy(r) for r in rows or []}

    async def set(
        self,
        tenant_id: str,
        data_source_id: str,
        policies: Sequence[RolePolicy],
        *,
        updated_by: str = "",
    ) -> None:
        for policy in policies:
            await self.db.execute(
                f"""
                INSERT INTO {SCHEMA}.grant_policies
                    (tenant_id, data_source_id, role, preset,
                     apply_to_new_tables, enforce_reads, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, data_source_id, role) DO UPDATE SET
                    preset              = EXCLUDED.preset,
                    apply_to_new_tables = EXCLUDED.apply_to_new_tables,
                    enforce_reads       = EXCLUDED.enforce_reads,
                    updated_by          = EXCLUDED.updated_by,
                    updated_at          = now()
                """,
                (
                    tenant_id,
                    data_source_id,
                    policy.role,
                    policy.preset,
                    policy.apply_to_new_tables,
                    policy.enforce_reads,
                    updated_by,
                ),
            )

    async def mark_applied(
        self, tenant_id: str, data_source_id: str, role: str, *, version: int
    ) -> None:
        await self.db.execute(
            f"UPDATE {SCHEMA}.grant_policies "
            "SET last_applied_at = now(), last_applied_version = %s "
            "WHERE tenant_id = %s AND data_source_id = %s AND role = %s",
            (version, tenant_id, data_source_id, role),
        )

    async def pending_for_new_tables(
        self, tenant_id: str, data_source_id: str
    ) -> List[RolePolicy]:
        """Roles whose default should reach a table that has just appeared."""
        rows = await self.db.fetch_all(
            f"SELECT * FROM {SCHEMA}.grant_policies "
            "WHERE tenant_id = %s AND data_source_id = %s "
            "AND apply_to_new_tables AND preset <> 'none'",
            (tenant_id, data_source_id),
        )
        return [_to_policy(r) for r in rows or []]

    async def enforced_read_roles(
        self, tenant_id: str, data_source_id: str
    ) -> List[str]:
        """Roles for which grants also decide what may be read.

        Read enforcement is per role rather than per workspace so it can be
        rolled out to viewers before analysts. A role with no row is not
        enforced, which is what keeps every existing deployment unchanged.
        """
        rows = await self.db.fetch_all(
            f"SELECT role FROM {SCHEMA}.grant_policies "
            "WHERE tenant_id = %s AND data_source_id = %s AND enforce_reads",
            (tenant_id, data_source_id),
        )
        return [str(r["role"]) for r in rows or []]
