"""Business rules in the control plane.

The store behind a workspace's own instructions, and behind its choices about
the platform baseline. Replaces ``MarkdownInstructionStore`` for any deployment
with a control plane; the markdown store stays as the demo-mode fallback and as
the library's default, and is still what the one-shot importer reads from.

Three responsibilities, kept in one class because they share a tenant and a
transaction: the :class:`InstructionStore` contract, the
:class:`BaselineOverrideStore` contract (which baseline rules this workspace
switched off), and starter-pack bookkeeping.

Failures propagate rather than degrade. Most stores here log and return empty on
a database error, which is right when the cost is a missing convenience. These
rules are what the model is told it must obey, and silently dropping them
produces a confidently wrong answer with nothing in the response to suggest
anything went missing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from vanna.capabilities.agent_memory import tenant_scope
from vanna.capabilities.knowledge import (
    Instruction,
    InstructionOrigin,
    InstructionScope,
    InstructionStore,
)

logger = logging.getLogger("vanna.instructions")

SCHEMA = "vanna_app"

#: Columns every read selects, in one place so the row mapper cannot drift.
_COLUMNS = (
    "id, tenant_id, text, scope, scope_ref, priority, enabled, origin, "
    "source_pack, metadata, created_at, created_by, updated_at"
)


def _to_model(row: Dict[str, Any]) -> Instruction:
    try:
        scope = InstructionScope(str(row.get("scope") or "global"))
    except ValueError:  # pragma: no cover - the CHECK constraint prevents this
        scope = InstructionScope.GLOBAL
    try:
        origin = InstructionOrigin(str(row.get("origin") or "tenant"))
    except ValueError:  # pragma: no cover - ditto
        origin = InstructionOrigin.TENANT

    return Instruction(
        id=str(row["id"]),
        text=row["text"],
        scope=scope,
        scope_ref=row.get("scope_ref"),
        tenant_id=row["tenant_id"],
        priority=int(row.get("priority") or 0),
        enabled=bool(row.get("enabled", True)),
        origin=origin,
        source_pack=row.get("source_pack"),
        metadata=dict(row.get("metadata") or {}),
        created_at=row.get("created_at") or datetime.now(timezone.utc),
        created_by=row.get("created_by"),
        updated_at=row.get("updated_at"),
    )


class PostgresInstructionStore(InstructionStore):
    """Instructions, overrides and pack membership for every workspace."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _tenant(context: Any) -> str:
        return tenant_scope(context)

    @staticmethod
    def _actor(context: Any) -> str:
        user = getattr(context, "user", None)
        return str(getattr(user, "email", "") or getattr(user, "id", "") or "")

    # -- InstructionStore ----------------------------------------------

    async def add(self, context: Any, instruction: Instruction) -> Instruction:
        if instruction.scope != InstructionScope.GLOBAL and not instruction.scope_ref:
            raise ValueError(
                f"Instructions scoped to '{instruction.scope.value}' require a "
                "scope_ref naming the data source, table, or group."
            )
        if instruction.origin == InstructionOrigin.PLATFORM:
            # The baseline is not rows. Storing one here would create a second,
            # per-tenant copy that a later edit to the YAML would not reach.
            raise ValueError("a platform rule cannot be stored against a tenant")

        tenant = self._tenant(context)
        actor = instruction.created_by or self._actor(context)

        row = await self.db.fetch_one(
            f"""
            INSERT INTO {SCHEMA}.instructions
                (tenant_id, text, scope, scope_ref, priority, enabled,
                 origin, source_pack, metadata, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                tenant,
                instruction.text.strip(),
                instruction.scope.value,
                instruction.scope_ref,
                instruction.priority,
                instruction.enabled,
                instruction.origin.value,
                instruction.source_pack,
                json.dumps(instruction.metadata or {}),
                actor,
                actor,
            ),
        )
        return _to_model(row)

    async def resolve(
        self,
        context: Any,
        *,
        data_source_id: Optional[str] = None,
        tables: Optional[List[str]] = None,
    ) -> List[Instruction]:
        # Scope matching stays in `Instruction.applies_to` rather than becoming
        # SQL. The table-scope rule matches bare and schema-qualified names, and
        # duplicating that in a WHERE clause would give two answers to maintain.
        rules = await self.list_all(context)
        groups = list(
            getattr(getattr(context, "user", None), "group_memberships", None) or []
        )
        applicable = [
            rule
            for rule in rules
            if rule.applies_to(
                data_source_id=data_source_id, tables=tables, user_groups=groups
            )
        ]
        applicable.sort(key=lambda i: (-i.priority, i.id))
        return applicable

    async def list_all(self, context: Any) -> List[Instruction]:
        rows = await self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM {SCHEMA}.instructions "
            "WHERE tenant_id = %s ORDER BY priority DESC, created_at",
            (self._tenant(context),),
        )
        return [_to_model(r) for r in rows or []]

    async def update(
        self,
        context: Any,
        instruction_id: str,
        *,
        text: Optional[str] = None,
        scope: Optional[InstructionScope] = None,
        scope_ref: Optional[str] = None,
        priority: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Instruction]:
        tenant = self._tenant(context)

        current = await self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM {SCHEMA}.instructions "
            "WHERE tenant_id = %s AND id = %s",
            (tenant, instruction_id),
        )
        if not current:
            return None

        existing = _to_model(current)
        new_scope = scope if scope is not None else existing.scope
        new_ref = scope_ref if scope_ref is not None else existing.scope_ref
        if new_scope != InstructionScope.GLOBAL and not new_ref:
            raise ValueError(
                f"Instructions scoped to '{new_scope.value}' require a "
                "scope_ref naming the data source, table, or group."
            )

        row = await self.db.fetch_one(
            f"""
            UPDATE {SCHEMA}.instructions
               SET text = %s, scope = %s, scope_ref = %s, priority = %s,
                   enabled = %s, updated_at = now(), updated_by = %s
             WHERE tenant_id = %s AND id = %s
            RETURNING {_COLUMNS}
            """,
            (
                (text if text is not None else existing.text).strip(),
                new_scope.value,
                new_ref,
                existing.priority if priority is None else priority,
                existing.enabled if enabled is None else enabled,
                self._actor(context),
                tenant,
                instruction_id,
            ),
        )
        return _to_model(row) if row else None

    async def set_enabled(
        self, context: Any, instruction_id: str, enabled: bool
    ) -> bool:
        changed = await self.db.execute(
            f"UPDATE {SCHEMA}.instructions SET enabled = %s, updated_at = now(), "
            "updated_by = %s WHERE tenant_id = %s AND id = %s",
            (enabled, self._actor(context), self._tenant(context), instruction_id),
        )
        return bool(changed)

    async def delete(self, context: Any, instruction_id: str) -> bool:
        removed = await self.db.execute(
            f"DELETE FROM {SCHEMA}.instructions WHERE tenant_id = %s AND id = %s",
            (self._tenant(context), instruction_id),
        )
        return bool(removed)

    # -- BaselineOverrideStore -----------------------------------------

    async def disabled_ids(self, context: Any) -> Set[str]:
        rows = await self.db.fetch_all(
            f"SELECT baseline_id FROM {SCHEMA}.instruction_overrides "
            "WHERE tenant_id = %s AND disabled",
            (self._tenant(context),),
        )
        return {str(r["baseline_id"]) for r in rows or []}

    async def set_disabled(
        self, context: Any, baseline_id: str, disabled: bool
    ) -> None:
        tenant = self._tenant(context)
        if not disabled:
            # Re-enabling removes the exception rather than storing `false`, so
            # the table stays a list of deviations from the shipped default.
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.instruction_overrides "
                "WHERE tenant_id = %s AND baseline_id = %s",
                (tenant, baseline_id),
            )
            return

        await self.db.execute(
            f"""
            INSERT INTO {SCHEMA}.instruction_overrides
                (tenant_id, baseline_id, disabled, updated_by)
            VALUES (%s, %s, true, %s)
            ON CONFLICT (tenant_id, baseline_id)
            DO UPDATE SET disabled = true, updated_at = now(),
                          updated_by = EXCLUDED.updated_by
            """,
            (tenant, baseline_id, self._actor(context)),
        )

    # -- starter packs -------------------------------------------------

    async def enabled_packs(self, context: Any) -> Set[str]:
        rows = await self.db.fetch_all(
            f"SELECT pack_id FROM {SCHEMA}.instruction_packs_enabled "
            "WHERE tenant_id = %s",
            (self._tenant(context),),
        )
        return {str(r["pack_id"]) for r in rows or []}

    async def copy_pack(self, context: Any, pack: Any) -> Tuple[int, int]:
        """Copy a pack's rules in. Returns ``(added, skipped)``.

        One transaction, so a pack is never half-taken. Idempotent by rule text
        as well as by pack membership: an admin who typed the same rule by hand
        before enabling the pack ends up with one copy, not two.
        """
        tenant = self._tenant(context)
        actor = self._actor(context)
        existing = {
            r.text.strip().casefold() for r in await self.list_all(context)
        }

        wanted = [
            rule
            for rule in pack.instructions
            if rule.text.strip().casefold() not in existing
        ]
        skipped = len(pack.instructions) - len(wanted)

        def run(cursor: Any) -> None:
            for rule in wanted:
                cursor.execute(
                    f"""
                    INSERT INTO {SCHEMA}.instructions
                        (tenant_id, text, scope, scope_ref, priority,
                         origin, source_pack, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, 'library', %s, %s, %s)
                    """,
                    (
                        tenant,
                        rule.text.strip(),
                        rule.scope.value,
                        rule.scope_ref,
                        rule.priority,
                        pack.id,
                        actor,
                        actor,
                    ),
                )
            cursor.execute(
                f"""
                INSERT INTO {SCHEMA}.instruction_packs_enabled
                    (tenant_id, pack_id, enabled_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, pack_id) DO NOTHING
                """,
                (tenant, pack.id, actor),
            )

        await self._transact(run)
        return len(wanted), skipped

    async def remove_pack(self, context: Any, pack_id: str) -> Tuple[int, int]:
        """Remove a pack. Returns ``(removed, kept)``.

        An **edited** copy is kept. Once somebody has changed the wording, the
        rule is theirs and removing the pack it arrived in should not delete
        their work -- so only rows still untouched since they were copied go.
        """
        tenant = self._tenant(context)

        kept = await self.db.fetch_value(
            f"SELECT count(*) FROM {SCHEMA}.instructions "
            "WHERE tenant_id = %s AND source_pack = %s AND updated_at > created_at",
            (tenant, pack_id),
            default=0,
        )

        def run(cursor: Any) -> None:
            cursor.execute(
                f"DELETE FROM {SCHEMA}.instructions "
                "WHERE tenant_id = %s AND source_pack = %s "
                "AND updated_at <= created_at",
                (tenant, pack_id),
            )
            cursor.execute(
                f"DELETE FROM {SCHEMA}.instruction_packs_enabled "
                "WHERE tenant_id = %s AND pack_id = %s",
                (tenant, pack_id),
            )

        before = await self.db.fetch_value(
            f"SELECT count(*) FROM {SCHEMA}.instructions "
            "WHERE tenant_id = %s AND source_pack = %s",
            (tenant, pack_id),
            default=0,
        )
        await self._transact(run)
        return int(before) - int(kept), int(kept)

    # -- one-shot import -----------------------------------------------

    async def import_from(self, context: Any, source: InstructionStore) -> int:
        """Copy a markdown store's rules for this tenant in. Returns the count.

        Deduplicates on normalised text, so running it twice is safe. The caller
        is responsible for only running it once per workspace -- see
        ``tenants.instructions_imported_at``, without which this would resurrect
        every rule an administrator deliberately deleted.
        """
        try:
            incoming = await source.list_all(context)
        except Exception as exc:
            logger.warning("Could not read the markdown rules to import: %s", exc)
            return 0

        existing = {r.text.strip().casefold() for r in await self.list_all(context)}
        added = 0
        for rule in incoming:
            key = rule.text.strip().casefold()
            if not key or key in existing:
                continue
            existing.add(key)
            # The id is not carried over: the column is a uuid generated by the
            # database, while a markdown rule's id may be any filename stem.
            await self.add(context, rule)
            added += 1
        return added

    # -- plumbing ------------------------------------------------------

    async def _transact(self, body: Callable[[Any], None]) -> None:
        """Run ``body(cursor)`` inside one transaction, off the event loop."""

        await self.db.transact(body)


async def claim_import(db: Any, tenant_id: str) -> bool:
    """Take responsibility for importing this workspace's markdown rules.

    Returns True to exactly one caller. The marker is set *before* the import
    rather than after, and that ordering is the whole point: the API runs four
    workers, and with a read-then-import-then-mark sequence two of them both saw
    "not imported", both read an empty table, and both inserted the same rules.
    That is not hypothetical -- it happened on the first workspace this ran
    against, and produced two of every rule.

    A worker that claims and then fails calls :func:`release_import` so the work
    is retried rather than silently skipped.
    """
    row = await db.fetch_one(
        f"UPDATE {SCHEMA}.tenants SET instructions_imported_at = now() "
        "WHERE id = %s AND instructions_imported_at IS NULL RETURNING id",
        (tenant_id,),
    )
    return bool(row)


async def release_import(db: Any, tenant_id: str) -> None:
    """Give the claim back after a failed import, so the next boot retries."""
    await db.execute(
        f"UPDATE {SCHEMA}.tenants SET instructions_imported_at = NULL WHERE id = %s",
        (tenant_id,),
    )


async def needs_import(db: Any, tenant_id: str) -> bool:
    """Whether the import is still outstanding. Advisory -- see `claim_import`."""
    value = await db.fetch_value(
        f"SELECT instructions_imported_at FROM {SCHEMA}.tenants WHERE id = %s",
        (tenant_id,),
    )
    return value is None
