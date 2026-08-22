"""``WriteApprovalStore`` over the control plane.

Like the grant store and unlike the analytics stores, a failure here propagates.
These rows decide whether a change happens; a lookup that degrades to a no-op
degrades to "not found", and a mutation that degrades to a no-op loses the record
of a decision somebody made.

The one property that has to survive is that :meth:`claim` is atomic. Two people
clicking approve in the same instant must produce one execution and one refusal.
``SELECT ... FOR UPDATE`` inside an explicit transaction is what provides it: the
second caller blocks until the first commits, then finds a settled row.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from vanna.core.write.approval import (
    OPEN,
    PendingWrite,
    WriteApprovalStore,
    WriteStatus,
    assert_may_decide,
)
from vanna.core.write.errors import WriteCode, WriteRefusal

from .db import SCHEMA

logger = logging.getLogger("vanna.writes")

_COLUMNS = """
    id, tenant_id, data_source_id, conversation_id, request_id,
    requested_by, requested_by_email, status, plan, statement_preview,
    parameter_summary, operation, tables, expected_row_count, is_destructive,
    plan_hash, grants_version, catalog_fingerprint, expires_at, created_at,
    decided_by, decided_at, error
"""


class PostgresWriteApprovalStore(WriteApprovalStore):
    """Pending writes shared across replicas."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _tenant(context: Any) -> str:
        return getattr(context, "tenant_id", None) or "default"

    # -- reads ---------------------------------------------------------

    async def get(self, context: Any, pending_id: str) -> Optional[PendingWrite]:
        row = await self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM {SCHEMA}.pending_writes "
            "WHERE id = %s AND tenant_id = %s",
            (pending_id, self._tenant(context)),
        )
        # A tenant mismatch reads as absence rather than as a permission error:
        # an id that answers differently for two tenants is an id that can be
        # used to probe another tenant's activity.
        return _to_model(row) if row else None

    async def list_open(
        self,
        context: Any,
        *,
        status: Optional[WriteStatus] = None,
        limit: int = 50,
    ) -> List[PendingWrite]:
        statuses = [status.value] if status else [s.value for s in OPEN]
        rows = await self.db.fetch_all(
            f"SELECT {_COLUMNS} FROM {SCHEMA}.pending_writes "
            "WHERE tenant_id = %s AND status = ANY(%s) AND expires_at > now() "
            "ORDER BY created_at DESC LIMIT %s",
            (self._tenant(context), statuses, limit),
        )
        return [_to_model(row) for row in rows or []]

    # -- writes --------------------------------------------------------

    async def create(self, context: Any, pending: PendingWrite) -> PendingWrite:
        stamped = pending.model_copy(update={"tenant_id": self._tenant(context)})
        await self.db.execute(
            f"INSERT INTO {SCHEMA}.pending_writes ({_COLUMNS}) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                stamped.id, stamped.tenant_id, stamped.data_source_id,
                stamped.conversation_id, stamped.request_id,
                stamped.requested_by, stamped.requested_by_email,
                stamped.status.value, json.dumps(stamped.plan),
                stamped.statement_preview, json.dumps(stamped.parameter_summary),
                stamped.operation, json.dumps(stamped.tables),
                stamped.expected_row_count, stamped.is_destructive,
                stamped.plan_hash, stamped.grants_version,
                stamped.catalog_fingerprint, stamped.expires_at,
                stamped.created_at, stamped.decided_by, stamped.decided_at,
                stamped.error,
            ),
        )
        return stamped

    async def claim(
        self,
        context: Any,
        pending_id: str,
        *,
        decided_by: str,
        decision: WriteStatus,
        decider_is_admin: bool = False,
    ) -> PendingWrite:
        if decision not in (WriteStatus.APPROVED, WriteStatus.REJECTED):
            raise ValueError("a claim decides approved or rejected, nothing else")

        tenant = self._tenant(context)
        outcome: dict = {}

        def run(cursor: Any) -> None:
            # FOR UPDATE inside the transaction is the single-use guarantee.
            # Without it two concurrent approvals both read `pending`, both
            # proceed, and the change happens twice.
            cursor.execute(
                f"SELECT {_COLUMNS} FROM {SCHEMA}.pending_writes "
                "WHERE id = %s AND tenant_id = %s FOR UPDATE",
                (pending_id, tenant),
            )
            row = cursor.fetchone()
            if row is None:
                outcome["error"] = (
                    WriteCode.CONFIRMATION_NOT_PENDING,
                    "That change is no longer available to decide.",
                )
                return

            pending = _to_model(_named(cursor, row))
            try:
                assert_may_decide(
                    pending, decider_id=decided_by, decider_is_admin=decider_is_admin
                )
            except WriteRefusal as refusal:
                outcome["error"] = (refusal.code, refusal.message)
                return

            if pending.status not in OPEN:
                outcome["error"] = (
                    WriteCode.CONFIRMATION_NOT_PENDING,
                    "That change has already been dealt with.",
                )
                return

            if pending.is_expired:
                # Settle it on the way past. Safety does not depend on the
                # sweeper having run, but leaving it open lets the console keep
                # offering a button that cannot work.
                cursor.execute(
                    f"UPDATE {SCHEMA}.pending_writes SET status = 'expired' "
                    "WHERE id = %s AND tenant_id = %s",
                    (pending_id, tenant),
                )
                outcome["error"] = (
                    WriteCode.CONFIRMATION_EXPIRED,
                    "That change sat unapproved for too long. Ask for it again "
                    "and it will be re-checked.",
                )
                return

            decided_at = datetime.now(timezone.utc)
            cursor.execute(
                f"UPDATE {SCHEMA}.pending_writes "
                "SET status = %s, decided_by = %s, decided_at = %s "
                "WHERE id = %s AND tenant_id = %s",
                (decision.value, decided_by, decided_at, pending_id, tenant),
            )
            outcome["pending"] = pending.model_copy(update={
                "status": decision,
                "decided_by": decided_by,
                "decided_at": decided_at,
            })

        await self._transact(run)

        if "error" in outcome:
            raise WriteRefusal(*outcome["error"])
        return outcome["pending"]

    async def mark(
        self,
        context: Any,
        pending_id: str,
        *,
        status: WriteStatus,
        error: Optional[str] = None,
    ) -> None:
        await self.db.execute(
            f"UPDATE {SCHEMA}.pending_writes SET status = %s, error = %s "
            "WHERE id = %s AND tenant_id = %s",
            (status.value, error, pending_id, self._tenant(context)),
        )

    async def expire_stale(self, context: Any) -> int:
        return int(
            await self.db.execute(
                f"UPDATE {SCHEMA}.pending_writes SET status = 'expired' "
                "WHERE tenant_id = %s AND status = ANY(%s) AND expires_at <= now()",
                (self._tenant(context), [s.value for s in OPEN]),
            )
            or 0
        )

    # -- plumbing ------------------------------------------------------

    async def _transact(self, body: Callable[[Any], None]) -> None:
        def run() -> None:
            with self.db.transaction() as connection:
                with connection.cursor() as cursor:
                    body(cursor)

        await asyncio.to_thread(run)


def _named(cursor: Any, row: Any) -> dict:
    """A plain cursor returns tuples; pair them with the column names."""
    if isinstance(row, dict):
        return row
    return dict(zip([d[0] for d in cursor.description], row))


def _to_model(row: dict) -> PendingWrite:
    return PendingWrite(
        id=str(row["id"]),
        tenant_id=row["tenant_id"],
        data_source_id=row["data_source_id"],
        conversation_id=row.get("conversation_id"),
        request_id=row.get("request_id"),
        requested_by=row["requested_by"],
        requested_by_email=row.get("requested_by_email"),
        status=WriteStatus(row["status"]),
        plan=_json(row["plan"], {}),
        statement_preview=row["statement_preview"],
        parameter_summary=_json(row.get("parameter_summary"), {}),
        operation=row["operation"],
        tables=_json(row.get("tables"), []),
        expected_row_count=int(row.get("expected_row_count") or 0),
        is_destructive=bool(row.get("is_destructive")),
        plan_hash=row["plan_hash"],
        grants_version=int(row.get("grants_version") or 0),
        catalog_fingerprint=row.get("catalog_fingerprint"),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        decided_by=row.get("decided_by"),
        decided_at=row.get("decided_at"),
        error=row.get("error"),
    )


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value
