"""In-memory grant store.

For tests, examples and single-process deployments. Everything a SQL backend
gets from the database -- atomic version bumps, tenant isolation -- is provided
here by one lock and one tenant-keyed dict, so the semantics a test observes are
the semantics production observes.

Not durable. A deployment that restarts loses every grant, which for a store
whose empty state means "nothing is writable" is a safe way to fail.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from ...core.grants import (
    AUTOFILL,
    ColumnGrant,
    EffectiveGrants,
    GrantStore,
    TableGrant,
    normalize_identifier,
    normalize_table,
    resolve_grants,
)

if TYPE_CHECKING:  # pragma: no cover
    from ...core.tool import ToolContext


class MemoryGrantStore(GrantStore):
    """Grants held in process memory, scoped by tenant.

    ``key_columns`` and ``unassignable_columns`` are supplied by the caller
    rather than discovered, because this store has no database to introspect.
    Wire them from a scanned catalog with
    :func:`vanna.core.grants.catalog_write_facts`.
    """

    def __init__(
        self,
        *,
        key_columns: Optional[Dict[str, Sequence[str]]] = None,
        unassignable_columns: Optional[Dict[str, Sequence[str]]] = None,
    ) -> None:
        self._lock = asyncio.Lock()
        # (tenant, data_source) -> {(role, table_key): TableGrant}
        self._tables: Dict[Tuple[str, str], Dict[Tuple[str, str], TableGrant]] = {}
        # (tenant, data_source) -> {(role, table_key, column_key): ColumnGrant}
        self._columns: Dict[
            Tuple[str, str], Dict[Tuple[str, str, str], ColumnGrant]
        ] = {}
        self._versions: Dict[Tuple[str, str], int] = {}
        self.key_columns = dict(key_columns or {})
        self.unassignable_columns = dict(unassignable_columns or {})

    # -- catalog facts -------------------------------------------------

    def load_catalog_facts(self, tables) -> None:
        """Populate the key/generated column maps from scanned table metadata."""
        from ...core.grants import catalog_write_facts

        keys, unassignable = catalog_write_facts(tables)
        self.key_columns.update(keys)
        self.unassignable_columns.update(unassignable)

    # -- reads ---------------------------------------------------------

    async def resolve(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        roles: Sequence[str],
    ) -> EffectiveGrants:
        tenant = _tenant(context)
        async with self._lock:
            scope = (tenant, data_source_id)
            # Read the grants and the version under one lock: a version
            # observed after a concurrent mutation would claim these grants
            # were newer than they are, which is the one direction this check
            # must never fail in.
            return resolve_grants(
                tenant_id=tenant,
                data_source_id=data_source_id,
                roles=roles,
                table_grants=list(self._tables.get(scope, {}).values()),
                column_grants=list(self._columns.get(scope, {}).values()),
                version=self._versions.get(scope, 0),
                key_columns=self.key_columns,
                unassignable_columns=self.unassignable_columns,
            )

    async def version(self, context: "ToolContext", *, data_source_id: str) -> int:
        async with self._lock:
            return self._versions.get((_tenant(context), data_source_id), 0)

    async def list_table_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: Optional[str] = None,
    ) -> List[TableGrant]:
        async with self._lock:
            grants = self._tables.get((_tenant(context), data_source_id), {}).values()
            return [
                g for g in grants
                if role is None or g.role.casefold() == role.casefold()
            ]

    async def list_column_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: Optional[str] = None,
        table: Optional[str] = None,
    ) -> List[ColumnGrant]:
        table_key = normalize_table(table) if table else None
        async with self._lock:
            grants = self._columns.get((_tenant(context), data_source_id), {}).values()
            return [
                g for g in grants
                if (role is None or g.role.casefold() == role.casefold())
                and (table_key is None or g.table_key == table_key)
            ]

    # -- mutations -----------------------------------------------------

    async def set_table_grant(self, context: "ToolContext", grant: TableGrant) -> None:
        stamped = grant.model_copy(update={"tenant_id": _tenant(context)})
        scope = (stamped.tenant_id, stamped.data_source_id)
        async with self._lock:
            self._tables.setdefault(scope, {})[
                (stamped.role.casefold(), stamped.key)
            ] = stamped
            self._bump(scope)

    async def set_column_grant(self, context: "ToolContext", grant: ColumnGrant) -> None:
        stamped = grant.model_copy(update={"tenant_id": _tenant(context)})
        scope = (stamped.tenant_id, stamped.data_source_id)
        async with self._lock:
            self._columns.setdefault(scope, {})[
                (stamped.role.casefold(), stamped.table_key, stamped.key)
            ] = stamped
            self._bump(scope)

    async def replace_table_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        grants: Sequence[TableGrant],
    ) -> None:
        tenant = _tenant(context)
        scope = (tenant, data_source_id)
        async with self._lock:
            bucket = self._tables.setdefault(scope, {})
            for key in [k for k in bucket if k[0] == role.casefold()]:
                del bucket[key]
            for grant in grants:
                stamped = grant.model_copy(
                    update={
                        "tenant_id": tenant,
                        "data_source_id": data_source_id,
                        "role": role,
                    }
                )
                bucket[(role.casefold(), stamped.key)] = stamped
            self._bump(scope)

    async def replace_column_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        grants: Sequence[ColumnGrant],
    ) -> None:
        tenant = _tenant(context)
        scope = (tenant, data_source_id)
        async with self._lock:
            bucket = self._columns.setdefault(scope, {})
            for key in [k for k in bucket if k[0] == role.casefold()]:
                del bucket[key]
            for grant in grants:
                stamped = grant.model_copy(
                    update={
                        "tenant_id": tenant,
                        "data_source_id": data_source_id,
                        "role": role,
                    }
                )
                bucket[(role.casefold(), stamped.table_key, stamped.key)] = stamped
            self._bump(scope)

    async def auto_grant_columns(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        table: str,
        columns: Sequence[str],
        can_write: bool = False,
        unassignable: Sequence[str] = (),
    ) -> None:
        tenant = _tenant(context)
        scope = (tenant, data_source_id)
        table_key = normalize_table(table)
        blocked = {normalize_identifier(name) for name in unassignable} or {
            normalize_identifier(name)
            for name in self.unassignable_columns.get(table_key, ())
        }
        async with self._lock:
            bucket = self._columns.setdefault(scope, {})
            changed = False
            for column in columns:
                key = (role.casefold(), table_key, normalize_identifier(column))
                writable = can_write and normalize_identifier(column) not in blocked
                existing = bucket.get(key)

                if existing is not None:
                    if not existing.is_machine_managed:
                        continue  # a person chose this; autofill does not argue
                    if existing.can_write == writable:
                        continue
                    # Autofill's own row, and the table's access level moved.
                    # Bringing it along is what makes Read & write actually
                    # writable after a pass through Read only.
                    bucket[key] = existing.model_copy(update={"can_write": writable})
                    changed = True
                    continue

                bucket[key] = ColumnGrant(
                    tenant_id=tenant,
                    data_source_id=data_source_id,
                    role=role,
                    table=table,
                    column=column,
                    can_read=True,
                    can_filter=True,
                    can_aggregate=True,
                    can_write=writable,
                    granted_by=AUTOFILL,
                )
                changed = True
            if changed:
                self._bump(scope)

    def _bump(self, scope: Tuple[str, str]) -> None:
        self._versions[scope] = self._versions.get(scope, 0) + 1


def _tenant(context: "ToolContext") -> str:
    return getattr(context, "tenant_id", "default") or "default"
