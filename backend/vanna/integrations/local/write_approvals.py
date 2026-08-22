"""In-memory write approval store.

For tests, examples and single-process deployments. The one property that must
survive the move to a real database is that :meth:`claim` is atomic, and here
that is one lock held across read-check-write. A test that proves two concurrent
approvals produce one execution proves it against the same semantics production
gets from ``SELECT ... FOR UPDATE``.

Not durable: a restart forgets every pending write. For a store whose empty
state means "nothing is approved to run", that is a safe way to fail.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional

from ...core.write.approval import (
    OPEN,
    PendingWrite,
    WriteApprovalStore,
    WriteStatus,
    assert_may_decide,
)
from ...core.write.errors import WriteCode, WriteRefusal

if TYPE_CHECKING:  # pragma: no cover
    from ...core.tool import ToolContext


class MemoryWriteApprovalStore(WriteApprovalStore):
    """Pending writes held in process memory, scoped by tenant."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._rows: Dict[str, PendingWrite] = {}

    async def create(
        self, context: "ToolContext", pending: PendingWrite
    ) -> PendingWrite:
        stamped = pending.model_copy(update={"tenant_id": _tenant(context)})
        async with self._lock:
            self._rows[stamped.id] = stamped
        return stamped

    async def get(
        self, context: "ToolContext", pending_id: str
    ) -> Optional[PendingWrite]:
        async with self._lock:
            row = self._rows.get(pending_id)
            # Tenant mismatch reads as absence, not as a permission error: an
            # id that answers differently for two tenants is an id that can be
            # used to probe another tenant's activity.
            if row is None or row.tenant_id != _tenant(context):
                return None
            return row.model_copy()

    async def claim(
        self,
        context: "ToolContext",
        pending_id: str,
        *,
        decided_by: str,
        decision: WriteStatus,
        decider_is_admin: bool = False,
    ) -> PendingWrite:
        if decision not in (WriteStatus.APPROVED, WriteStatus.REJECTED):
            raise ValueError("a claim decides approved or rejected, nothing else")

        # One lock across read, check and write. Splitting them is exactly the
        # race this method exists to close: two approvals both reading `pending`
        # and both proceeding.
        async with self._lock:
            row = self._rows.get(pending_id)
            if row is None or row.tenant_id != _tenant(context):
                raise WriteRefusal(
                    WriteCode.CONFIRMATION_NOT_PENDING,
                    "That change is no longer available to decide.",
                )

            assert_may_decide(
                row, decider_id=decided_by, decider_is_admin=decider_is_admin
            )

            if row.status not in OPEN:
                raise WriteRefusal(
                    WriteCode.CONFIRMATION_NOT_PENDING,
                    "That change has already been dealt with.",
                )

            if row.is_expired:
                # Settle it on the way past. Safety does not depend on the
                # sweeper having run, but leaving the row open would let the
                # console keep offering a button that cannot work.
                self._rows[pending_id] = row.model_copy(
                    update={"status": WriteStatus.EXPIRED}
                )
                raise WriteRefusal(
                    WriteCode.CONFIRMATION_EXPIRED,
                    "That change sat unapproved for too long. Ask for it again "
                    "and it will be re-checked.",
                )

            from datetime import datetime, timezone

            decided = row.model_copy(
                update={
                    "status": decision,
                    "decided_by": decided_by,
                    "decided_at": datetime.now(timezone.utc),
                }
            )
            self._rows[pending_id] = decided
            return decided

    async def mark(
        self,
        context: "ToolContext",
        pending_id: str,
        *,
        status: WriteStatus,
        error: Optional[str] = None,
    ) -> None:
        async with self._lock:
            row = self._rows.get(pending_id)
            if row is None or row.tenant_id != _tenant(context):
                return
            self._rows[pending_id] = row.model_copy(
                update={"status": status, "error": error}
            )

    async def list_open(
        self,
        context: "ToolContext",
        *,
        status: Optional[WriteStatus] = None,
        limit: int = 50,
    ) -> List[PendingWrite]:
        tenant = _tenant(context)
        async with self._lock:
            rows = [
                row.model_copy()
                for row in self._rows.values()
                if row.tenant_id == tenant
                and (row.status is status if status else row.status in OPEN)
                and not row.is_expired
            ]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def expire_stale(self, context: "ToolContext") -> int:
        tenant = _tenant(context)
        expired = 0
        async with self._lock:
            for key, row in list(self._rows.items()):
                if row.tenant_id == tenant and row.status in OPEN and row.is_expired:
                    self._rows[key] = row.model_copy(
                        update={"status": WriteStatus.EXPIRED}
                    )
                    expired += 1
        return expired


def _tenant(context: "ToolContext") -> str:
    return getattr(context, "tenant_id", "default") or "default"
