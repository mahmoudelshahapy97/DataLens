"""``GrantStore`` over the control plane.

Deliberately not in :mod:`stores`, whose contract is that a write on the request
path never propagates a failure. That is right for analytics -- a generation log
that can kill a user's answer has inverted its own cost/benefit -- and exactly
wrong here. Those stores record what happened; this one decides what is allowed
to happen. A permission lookup that degrades to a no-op degrades to a stale
answer, and letting the exception out is the only honest option.

Every mutation runs in one transaction with the version bump, so a resolver can
never observe a changed grant under an unchanged version. That comparison is what
makes revoking a grant stop a write that was already approved.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from vanna.core.grants import (
    AUTOFILL,
    MACHINE_PREFIX,
    ColumnGrant,
    EffectiveGrants,
    GrantStore,
    TableGrant,
    catalog_write_facts,
    normalize_identifier,
    normalize_table,
    resolve_grants,
)

from .db import SCHEMA

logger = logging.getLogger("vanna.grants")


class PostgresGrantStore(GrantStore):
    """Grants shared across replicas, versioned for the write approval path."""

    def __init__(self, db: Any, *, catalog: Any = None) -> None:
        self.db = db
        # Optional, and only ever a narrowing. Supplying it lets resolution drop
        # write verbs a table cannot support -- no key column means UPDATE
        # cannot name its rows, a generated column cannot be assigned. Omitting
        # it costs nothing structural: `build_write_policy` applies the same
        # narrowing from the live catalog before any statement is built, so
        # this is defence in depth rather than the only line.
        self.catalog = catalog

    @staticmethod
    def _tenant(context: Any) -> str:
        return getattr(context, "tenant_id", None) or "default"

    # -- reads ---------------------------------------------------------

    async def resolve(
        self,
        context: Any,
        *,
        data_source_id: str,
        roles: Sequence[str],
    ) -> EffectiveGrants:
        tenant = self._tenant(context)
        held = [r for r in (roles or []) if r]
        if not held:
            # No roles is not an error. It is a caller with nothing granted,
            # which is the same answer as a caller whose grants are all absent.
            return resolve_grants(
                tenant_id=tenant,
                data_source_id=data_source_id,
                roles=[],
                table_grants=[],
                column_grants=[],
                version=0,
            )

        lowered = [r.lower() for r in held]
        # One round trip for both grant sets and the version. Reading the
        # version in the same statement as the grants is the point: fetched
        # afterwards it could describe a later state than the rows returned,
        # and an approved write would then be checked against a version its
        # own grants never had.
        payload = await self.db.fetch_one(
            f"""
            SELECT
              (SELECT coalesce(json_agg(t), '[]') FROM {SCHEMA}.table_grants t
                 WHERE t.tenant_id = %s AND t.data_source_id = %s
                   AND lower(t.role) = ANY(%s))                       AS table_rows,
              (SELECT coalesce(json_agg(c), '[]') FROM {SCHEMA}.column_grants c
                 WHERE c.tenant_id = %s AND c.data_source_id = %s
                   AND lower(c.role) = ANY(%s))                       AS column_rows,
              coalesce((SELECT v.version FROM {SCHEMA}.grant_versions v
                 WHERE v.tenant_id = %s AND v.data_source_id = %s), 0) AS version
            """,
            (
                tenant, data_source_id, lowered,
                tenant, data_source_id, lowered,
                tenant, data_source_id,
            ),
        ) or {}

        key_columns, unassignable = await self._catalog_facts(context, data_source_id)
        return resolve_grants(
            tenant_id=tenant,
            data_source_id=data_source_id,
            roles=held,
            table_grants=[_table_grant(r) for r in _rows(payload.get("table_rows"))],
            column_grants=[_column_grant(r) for r in _rows(payload.get("column_rows"))],
            version=int(payload.get("version") or 0),
            key_columns=key_columns,
            unassignable_columns=unassignable,
        )

    async def _catalog_facts(self, context: Any, data_source_id: str) -> tuple:
        """Key and generated columns, from the injected catalog.

        A table whose keys are unknown keeps its granted verbs here and is
        caught one layer later by the validator, which refuses any predicate it
        cannot prove addresses a key. The failure direction is therefore a
        refusal, never a wider UPDATE, which is why this is allowed to be
        best-effort.
        """
        if self.catalog is None:
            return {}, {}
        try:
            tables = await self.catalog.get_tables(
                context, data_source_id=data_source_id
            )
        except Exception as exc:
            logger.debug("Catalog facts unavailable for %r: %s", data_source_id, exc)
            return {}, {}
        return catalog_write_facts(tables or [])

    async def version(self, context: Any, *, data_source_id: str) -> int:
        value = await self.db.fetch_value(
            f"SELECT version FROM {SCHEMA}.grant_versions "
            "WHERE tenant_id = %s AND data_source_id = %s",
            (self._tenant(context), data_source_id),
            default=0,
        )
        return int(value or 0)

    async def list_table_grants(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: Optional[str] = None,
    ) -> List[TableGrant]:
        params: List[Any] = [self._tenant(context), data_source_id]
        clause = ""
        if role:
            clause = "AND lower(role) = %s"
            params.append(role.lower())
        rows = await self.db.fetch_all(
            f"SELECT * FROM {SCHEMA}.table_grants "
            f"WHERE tenant_id = %s AND data_source_id = %s {clause} "
            "ORDER BY role, table_key",
            tuple(params),
        )
        return [_table_grant(row) for row in rows or []]

    async def list_column_grants(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: Optional[str] = None,
        table: Optional[str] = None,
    ) -> List[ColumnGrant]:
        params: List[Any] = [self._tenant(context), data_source_id]
        clause = ""
        if role:
            clause += " AND lower(role) = %s"
            params.append(role.lower())
        if table:
            clause += " AND table_key = %s"
            params.append(normalize_table(table))
        rows = await self.db.fetch_all(
            f"SELECT * FROM {SCHEMA}.column_grants "
            f"WHERE tenant_id = %s AND data_source_id = %s {clause} "
            "ORDER BY role, table_key, column_key",
            tuple(params),
        )
        return [_column_grant(row) for row in rows or []]

    # -- mutations -----------------------------------------------------

    async def set_table_grant(self, context: Any, grant: TableGrant) -> None:
        tenant = self._tenant(context)

        def run(cursor: Any) -> None:
            cursor.execute(
                # source='explicit' on both branches: this method is only ever
                # reached from an administrator's own action, and a row somebody
                # has deliberately set must survive a later preset `replace`.
                f"INSERT INTO {SCHEMA}.table_grants "
                "(tenant_id, data_source_id, role, table_key, table_name, "
                " can_select, can_insert, can_update, can_delete, source, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'explicit', now()) "
                "ON CONFLICT (tenant_id, data_source_id, role, table_key) DO UPDATE SET "
                "  table_name = EXCLUDED.table_name, "
                "  can_select = EXCLUDED.can_select, "
                "  can_insert = EXCLUDED.can_insert, "
                "  can_update = EXCLUDED.can_update, "
                "  can_delete = EXCLUDED.can_delete, "
                "  source     = 'explicit', "
                "  updated_at = now()",
                (
                    tenant, grant.data_source_id, grant.role, grant.key, grant.table,
                    grant.can_select, grant.can_insert, grant.can_update, grant.can_delete,
                ),
            )
            _bump(cursor, tenant, grant.data_source_id)

        await self._transact(run)

    async def set_column_grant(self, context: Any, grant: ColumnGrant) -> None:
        tenant = self._tenant(context)

        def run(cursor: Any) -> None:
            cursor.execute(
                f"INSERT INTO {SCHEMA}.column_grants "
                "(tenant_id, data_source_id, role, table_key, column_key, "
                " table_name, column_name, "
                " can_read, can_filter, can_aggregate, can_write, mask_strategy, "
                " granted_by, source, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'explicit', now()) "
                "ON CONFLICT (tenant_id, data_source_id, role, table_key, column_key) "
                "DO UPDATE SET "
                "  table_name    = EXCLUDED.table_name, "
                "  column_name   = EXCLUDED.column_name, "
                "  can_read      = EXCLUDED.can_read, "
                "  can_filter    = EXCLUDED.can_filter, "
                "  can_aggregate = EXCLUDED.can_aggregate, "
                "  can_write     = EXCLUDED.can_write, "
                "  mask_strategy = EXCLUDED.mask_strategy, "
                "  granted_by    = EXCLUDED.granted_by, "
                "  source        = 'explicit', "
                "  updated_at    = now()",
                (
                    tenant, grant.data_source_id, grant.role,
                    grant.table_key, grant.key, grant.table, grant.column,
                    grant.can_read, grant.can_filter, grant.can_aggregate,
                    # A mask on an unreadable column is a rule that never fires
                    # and reads in an admin screen as protection that is not
                    # there. The CHECK constraint refuses it; this keeps the API
                    # from ever presenting one.
                    grant.can_write,
                    grant.mask if grant.can_read else "none",
                    grant.granted_by,
                ),
            )
            _bump(cursor, tenant, grant.data_source_id)

        await self._transact(run)

    async def replace_table_grants(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: str,
        grants: Sequence[TableGrant],
    ) -> None:
        tenant = self._tenant(context)
        rows = [
            (
                tenant, data_source_id, role, g.key, g.table,
                g.can_select, g.can_insert, g.can_update, g.can_delete,
            )
            for g in grants
        ]

        def run(cursor: Any) -> None:
            # Delete and insert in one transaction: a concurrent resolver would
            # otherwise catch the role stripped of every grant it is about to
            # regain, and answer "nothing is permitted" for that instant.
            cursor.execute(
                f"DELETE FROM {SCHEMA}.table_grants "
                "WHERE tenant_id = %s AND data_source_id = %s AND lower(role) = %s",
                (tenant, data_source_id, role.lower()),
            )
            if rows:
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.table_grants "
                    "(tenant_id, data_source_id, role, table_key, table_name, "
                    " can_select, can_insert, can_update, can_delete) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    rows,
                )
            _bump(cursor, tenant, data_source_id)

        await self._transact(run)

    async def replace_column_grants(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: str,
        grants: Sequence[ColumnGrant],
    ) -> None:
        tenant = self._tenant(context)
        rows = [
            (
                tenant, data_source_id, role, g.table_key, g.key, g.table, g.column,
                g.can_read, g.can_filter, g.can_aggregate, g.can_write,
            )
            for g in grants
        ]

        def run(cursor: Any) -> None:
            cursor.execute(
                f"DELETE FROM {SCHEMA}.column_grants "
                "WHERE tenant_id = %s AND data_source_id = %s AND lower(role) = %s",
                (tenant, data_source_id, role.lower()),
            )
            if rows:
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.column_grants "
                    "(tenant_id, data_source_id, role, table_key, column_key, "
                    " table_name, column_name, "
                    " can_read, can_filter, can_aggregate, can_write) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    rows,
                )
            _bump(cursor, tenant, data_source_id)

        await self._transact(run)

    async def auto_grant_columns(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: str,
        table: str,
        columns: Sequence[str],
        can_write: bool = False,
        unassignable: Sequence[str] = (),
    ) -> None:
        """Fill the gaps, and only the gaps.

        ``DO NOTHING`` rather than an upsert: an administrator who has already
        withheld one column must not silently get it back the next time the
        table grant is touched.
        """
        tenant = self._tenant(context)
        table_key = normalize_table(table)
        blocked = {normalize_identifier(name) for name in (unassignable or ())}
        rows = [
            (
                tenant, data_source_id, role, table_key, normalize_identifier(name),
                table, name, True, True, True,
                bool(can_write) and normalize_identifier(name) not in blocked,
                AUTOFILL,
            )
            for name in columns
        ]
        if not rows:
            return

        def run(cursor: Any) -> None:
            # DO UPDATE, but only for a row no person has claimed. An
            # administrator's row carries their identity and is left exactly as
            # they left it; autofill's own rows, a preset's rows, and rows
            # predating provenance all follow their table's access level -- which
            # is what makes Read & write actually writable. The predicate must
            # match ColumnGrant.is_machine_managed, or the two backends disagree
            # about who owns a row.
            cursor.executemany(
                f"INSERT INTO {SCHEMA}.column_grants "
                "(tenant_id, data_source_id, role, table_key, column_key, "
                " table_name, column_name, can_read, can_filter, can_aggregate, "
                " can_write, granted_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, data_source_id, role, table_key, column_key) "
                "DO UPDATE SET can_write = EXCLUDED.can_write, updated_at = now() "
                f"WHERE coalesce({SCHEMA}.column_grants.granted_by, '') = '' "
                f"   OR {SCHEMA}.column_grants.granted_by = %s "
                f"   OR {SCHEMA}.column_grants.granted_by LIKE %s",
                [row + (AUTOFILL, MACHINE_PREFIX + "%") for row in rows],
            )
            _bump(cursor, tenant, data_source_id)

        await self._transact(run)

    # -- revocation ----------------------------------------------------

    async def delete_table_grant(
        self, context: Any, *, data_source_id: str, role: str, table: str
    ) -> bool:
        tenant = self._tenant(context)
        removed = {"n": 0}

        def run(cursor: Any) -> None:
            cursor.execute(
                f"DELETE FROM {SCHEMA}.table_grants WHERE tenant_id = %s "
                "AND data_source_id = %s AND lower(role) = %s AND table_key = %s",
                (tenant, data_source_id, role.lower(), normalize_table(table)),
            )
            removed["n"] = cursor.rowcount or 0
            if removed["n"]:
                _bump(cursor, tenant, data_source_id)

        await self._transact(run)
        return bool(removed["n"])

    async def delete_column_grant(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: str,
        table: str,
        column: str,
    ) -> bool:
        tenant = self._tenant(context)
        removed = {"n": 0}

        def run(cursor: Any) -> None:
            cursor.execute(
                f"DELETE FROM {SCHEMA}.column_grants WHERE tenant_id = %s "
                "AND data_source_id = %s AND lower(role) = %s "
                "AND table_key = %s AND column_key = %s",
                (
                    tenant,
                    data_source_id,
                    role.lower(),
                    normalize_table(table),
                    normalize_identifier(column),
                ),
            )
            removed["n"] = cursor.rowcount or 0
            if removed["n"]:
                _bump(cursor, tenant, data_source_id)

        await self._transact(run)
        return bool(removed["n"])

    # -- bulk application ----------------------------------------------

    async def apply_preset(
        self,
        context: Any,
        *,
        data_source_id: str,
        role: str,
        table_grants: Sequence[TableGrant],
        column_grants: Sequence[ColumnGrant],
        mode: str = "fill",
        granted_by: str = "",
    ) -> Dict[str, int]:
        """Materialize a preset in one transaction, with one version bump.

        One bump for the whole application, not one per row: a hundred tables
        must not produce a hundred versions, and the transaction is what stops a
        resolver seeing a half-applied preset.

        ``replace`` deletes only rows this store wrote (``source = 'preset'``),
        so an administrator's own grants survive it. ``fill`` inserts with
        ``ON CONFLICT DO NOTHING``, the same idiom `auto_grant_columns` uses, so
        re-applying never resurrects a column somebody deliberately withheld.
        """
        tenant = self._tenant(context)
        counts: Dict[str, int] = {"tables": 0, "columns": 0, "skipped": 0}

        table_rows = [
            (
                tenant, data_source_id, role, g.key, g.table,
                g.can_select, g.can_insert, g.can_update, g.can_delete, granted_by,
            )
            for g in table_grants
        ]
        column_rows = [
            (
                tenant, data_source_id, role, g.table_key, g.key, g.table, g.column,
                g.can_read, g.can_filter, g.can_aggregate, g.can_write, granted_by,
            )
            for g in column_grants
        ]

        def run(cursor: Any) -> None:
            if mode == "replace":
                cursor.execute(
                    f"DELETE FROM {SCHEMA}.table_grants WHERE tenant_id = %s "
                    "AND data_source_id = %s AND lower(role) = %s AND source = 'preset'",
                    (tenant, data_source_id, role.lower()),
                )
                cursor.execute(
                    f"DELETE FROM {SCHEMA}.column_grants WHERE tenant_id = %s "
                    "AND data_source_id = %s AND lower(role) = %s AND source = 'preset'",
                    (tenant, data_source_id, role.lower()),
                )

            if table_rows:
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.table_grants "
                    "(tenant_id, data_source_id, role, table_key, table_name, "
                    " can_select, can_insert, can_update, can_delete, granted_by, "
                    " source) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'preset') "
                    "ON CONFLICT (tenant_id, data_source_id, role, table_key) "
                    "DO NOTHING",
                    table_rows,
                )
                counts["tables"] = cursor.rowcount if cursor.rowcount > 0 else 0

            if column_rows:
                cursor.executemany(
                    f"INSERT INTO {SCHEMA}.column_grants "
                    "(tenant_id, data_source_id, role, table_key, column_key, "
                    " table_name, column_name, can_read, can_filter, can_aggregate, "
                    " can_write, granted_by, source) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'preset') "
                    "ON CONFLICT (tenant_id, data_source_id, role, table_key, column_key) "
                    "DO NOTHING",
                    column_rows,
                )
                counts["columns"] = cursor.rowcount if cursor.rowcount > 0 else 0

            _bump(cursor, tenant, data_source_id)

        await self._transact(run)

        counts["skipped"] = (
            len(table_rows) + len(column_rows) - counts["tables"] - counts["columns"]
        )
        return counts

    # -- plumbing ------------------------------------------------------

    async def _transact(self, body: Callable[[Any], None]) -> None:
        """Run ``body(cursor)`` inside one transaction, off the event loop."""

        await self.db.transact(body)


def _bump(cursor: Any, tenant: str, data_source_id: str) -> None:
    """Increment the grant version, in the caller's transaction.

    Every mutation must call this. The version is what an approved-but-not-yet
    executed write is re-checked against, so a grant change that does not move
    it is a grant change no in-flight write will notice.
    """
    cursor.execute(
        f"INSERT INTO {SCHEMA}.grant_versions (tenant_id, data_source_id, version) "
        "VALUES (%s, %s, 1) "
        "ON CONFLICT (tenant_id, data_source_id) DO UPDATE SET "
        f"  version = {SCHEMA}.grant_versions.version + 1, updated_at = now()",
        (tenant, data_source_id),
    )


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """json_agg comes back as parsed JSON or as text, depending on the driver."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError:
            return []
    return list(payload or [])


def _table_grant(row: Dict[str, Any]) -> TableGrant:
    return TableGrant(
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        role=row["role"],
        table=row.get("table_name") or row["table_key"],
        can_select=bool(row.get("can_select")),
        can_insert=bool(row.get("can_insert")),
        can_update=bool(row.get("can_update")),
        can_delete=bool(row.get("can_delete")),
    )


def _column_grant(row: Dict[str, Any]) -> ColumnGrant:
    return ColumnGrant(
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        role=row["role"],
        table=row.get("table_name") or row["table_key"],
        column=row.get("column_name") or row["column_key"],
        can_read=bool(row.get("can_read")),
        can_filter=bool(row.get("can_filter")),
        can_aggregate=bool(row.get("can_aggregate")),
        can_write=bool(row.get("can_write")),
        mask=row.get("mask_strategy") or "none",
        granted_by=row.get("granted_by"),
    )
