"""A write, parked between being proposed and being run.

Approval spans requests. The model proposes a change in one turn, a person reads
it and decides in another, and the two may be minutes and several HTTP requests
apart. Anything held in request-scoped state -- ``ToolContext.metadata``, a
closure, a local -- is gone by then, which is why this is a persisted entity
rather than a flag.

**What is stored, and why it is stored separately.** The plan, and a hash of the
statements built from it. Then, in their own fields, the facts about the world
that made those statements legal: the grant version, the catalog fingerprint, an
expiry. Folding those into the hash would be simpler and worse -- the hash proves
the *statement* is the one that was shown, and these prove the *world* it was
shown in still holds. Keeping them apart is what lets a refusal say which one
moved, rather than only that something did.

**What is re-derived rather than trusted.** Everything. At execution the stored
plan is re-parsed through :class:`WritePlan`, a fresh policy is built from
current grants, and the plan is re-validated from scratch. A row edited in the
database between approval and execution must not be able to skip a validator,
and the only way to guarantee that is never to trust the stored statements.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .errors import WriteCode, WriteRefusal

if TYPE_CHECKING:  # pragma: no cover
    from ..tool import ToolContext
    from .validator import ValidatedWrite

#: How long an unapproved write stays live. Long enough to read a card and think;
#: short enough that the world it was authorized against has probably not moved.
DEFAULT_TTL_SECONDS = 900


class WriteApprovalMode(str, Enum):
    """Who has to say yes, configured per tenant."""

    SELF = "self"
    """The person who asked for the change confirms it. The default, and the
    right one for most deployments: an administrator has already decided which
    tables are writable at all, and this step confirms the specific statement."""

    SECOND_PERSON_DESTRUCTIVE = "second_person_destructive"
    """Self-approval for ordinary changes; a different administrator for
    deletes and multi-row updates."""

    SECOND_PERSON_ALWAYS = "second_person_always"
    """A different administrator for every change."""


class WriteStatus(str, Enum):
    """Where a proposed write has got to."""

    PENDING = "pending"
    """Waiting for the requester."""

    AWAITING_REVIEW = "awaiting_review"
    """Waiting for a second person."""

    APPROVED = "approved"
    """Decided yes; not yet run. Re-authorization happens after this."""

    REJECTED = "rejected"
    EXPIRED = "expired"
    REFUSED = "refused"
    """Re-authorization failed: something moved after approval."""

    EXECUTED = "executed"
    FAILED = "failed"


#: Statuses from which there is no transition. A claim against one of these is a
#: double-decision, and the store must refuse it rather than re-run anything.
SETTLED = frozenset({
    WriteStatus.APPROVED,
    WriteStatus.REJECTED,
    WriteStatus.EXPIRED,
    WriteStatus.REFUSED,
    WriteStatus.EXECUTED,
    WriteStatus.FAILED,
})

#: Statuses a person may still act on.
OPEN = frozenset({WriteStatus.PENDING, WriteStatus.AWAITING_REVIEW})


class PendingWrite(BaseModel):
    """One proposed change, awaiting a decision."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = "default"
    data_source_id: str

    conversation_id: Optional[str] = None
    request_id: Optional[str] = None
    requested_by: str
    requested_by_email: Optional[str] = None

    status: WriteStatus = WriteStatus.PENDING

    #: The serialized WritePlan. Re-parsed through the model before use, never
    #: trusted as stored.
    plan: Dict[str, Any]

    #: The statements as they will run, still parameterized. Safe to store and
    #: to show: the markers stand where tenant data would be.
    statement_preview: str
    #: Shapes only -- counts, tables, verbs. Never the bound values.
    parameter_summary: Dict[str, Any] = Field(default_factory=dict)

    operation: str
    tables: List[str] = Field(default_factory=list)
    expected_row_count: int = 0
    is_destructive: bool = False

    # -- the facts that made this legal, each checkable on its own -----
    plan_hash: str
    grants_version: int = 0
    catalog_fingerprint: Optional[str] = None

    expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    error: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        return _now() >= _aware(self.expires_at)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN and not self.is_expired

    @classmethod
    def propose(
        cls,
        validated: "ValidatedWrite",
        *,
        tenant_id: str,
        data_source_id: str,
        requested_by: str,
        plan: Dict[str, Any],
        mode: WriteApprovalMode = WriteApprovalMode.SELF,
        catalog_fingerprint: Optional[str] = None,
        conversation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        requested_by_email: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> "PendingWrite":
        """Build a pending write from a validated one.

        Everything shown on the approval card comes from ``validated`` rather
        than from the plan, so the card cannot describe something other than
        what would run.
        """
        return cls(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            conversation_id=conversation_id,
            request_id=request_id,
            requested_by=requested_by,
            requested_by_email=requested_by_email,
            status=(
                WriteStatus.AWAITING_REVIEW
                if requires_second_person(mode, validated.is_destructive)
                else WriteStatus.PENDING
            ),
            plan=plan,
            statement_preview=validated.statement_preview,
            parameter_summary=validated.parameter_summary,
            operation=validated.operation,
            tables=validated.tables,
            expected_row_count=validated.expected_row_count,
            is_destructive=validated.is_destructive,
            plan_hash=validated.plan_hash,
            grants_version=validated.grants_version,
            catalog_fingerprint=catalog_fingerprint,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )

    def describe(self) -> str:
        """The approval card's text: effect first, statement second."""
        rows = self.expected_row_count
        noun = "row" if rows == 1 else "rows"
        lines = [
            f"This would {self.operation} {rows} {noun} "
            f"in {', '.join(self.tables)}. Nothing has been changed yet.",
            "",
            self.statement_preview,
        ]
        if self.is_destructive:
            lines += ["", "This cannot be undone."]
        if self.status is WriteStatus.AWAITING_REVIEW:
            lines += ["", "A second administrator must approve this change."]
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Who may decide
# ----------------------------------------------------------------------


def requires_second_person(mode: WriteApprovalMode, is_destructive: bool) -> bool:
    if mode is WriteApprovalMode.SECOND_PERSON_ALWAYS:
        return True
    if mode is WriteApprovalMode.SECOND_PERSON_DESTRUCTIVE:
        return is_destructive
    return False


def may_decide(
    pending: PendingWrite, *, decider_id: str, decider_is_admin: bool = False
) -> bool:
    """Whether this person may approve or decline this write.

    The rule differs by which queue the write is in, and both directions matter:
    a second-person write must **not** be self-approvable, and a self-approval
    write must not be decidable by a passing administrator who never saw the
    question that produced it.
    """
    if pending.status is WriteStatus.AWAITING_REVIEW:
        return decider_is_admin and decider_id != pending.requested_by
    return decider_id == pending.requested_by


def assert_may_decide(
    pending: PendingWrite, *, decider_id: str, decider_is_admin: bool = False
) -> None:
    """Raise unless this person may decide.

    Callers should surface the refusal as **not found** rather than forbidden.
    Distinguishing "this id is not yours" from "this id does not exist" turns
    the identifier into an oracle for what other people are doing.
    """
    if not may_decide(
        pending, decider_id=decider_id, decider_is_admin=decider_is_admin
    ):
        raise WriteRefusal(
            WriteCode.CONFIRMATION_FORBIDDEN,
            "That change is not yours to decide.",
        )


def assert_still_valid(
    pending: PendingWrite,
    *,
    plan_hash: str,
    grants_version: int,
    catalog_fingerprint: Optional[str] = None,
) -> None:
    """Re-authorization: prove nothing that mattered has moved since approval.

    Called after approval and immediately before execution, against values
    freshly derived from current grants and the current catalog -- never against
    the stored copies, which is the entire point.

    Each check names its own fact. "Something changed" is not an answer anybody
    can act on; "the permissions changed" is.
    """
    if pending.status is not WriteStatus.APPROVED:
        raise WriteRefusal(
            WriteCode.CONFIRMATION_NOT_PENDING,
            "That change has already been dealt with.",
        )

    if pending.is_expired:
        raise WriteRefusal(
            WriteCode.CONFIRMATION_EXPIRED,
            "That change was approved too long ago to run now. "
            "Ask for it again and it will be re-checked.",
        )

    if pending.plan_hash != plan_hash:
        raise WriteRefusal(
            WriteCode.CONFIRMATION_MISMATCH,
            "The statement changed after it was approved, so it was not run.",
        )

    if pending.grants_version != grants_version:
        raise WriteRefusal(
            WriteCode.CONFIRMATION_MISMATCH,
            "Permissions changed after this was approved, so it was not run.",
        )

    if (
        pending.catalog_fingerprint is not None
        and catalog_fingerprint is not None
        and pending.catalog_fingerprint != catalog_fingerprint
    ):
        raise WriteRefusal(
            WriteCode.CONFIRMATION_MISMATCH,
            "The database schema changed after this was approved, so it was not run.",
        )


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


class WriteApprovalStore(ABC):
    """Persistence for writes awaiting a decision.

    Implementations must:

    * filter every read by ``context.tenant_id`` and stamp every write with it;
    * make :meth:`claim` **atomic**. Two people clicking approve at the same
      instant must produce one execution and one refusal, not two executions.
      A SQL backend does this with ``SELECT ... FOR UPDATE``; an in-memory one
      with a lock. It is part of the contract, not an implementation detail;
    * treat an expired row as settled even if no sweeper has run, so safety
      never depends on a scheduled job having fired.
    """

    @abstractmethod
    async def create(
        self, context: "ToolContext", pending: PendingWrite
    ) -> PendingWrite:
        """Store a newly proposed write."""

    @abstractmethod
    async def get(
        self, context: "ToolContext", pending_id: str
    ) -> Optional[PendingWrite]:
        """One pending write, or None. Scoped to the caller's tenant."""

    @abstractmethod
    async def claim(
        self,
        context: "ToolContext",
        pending_id: str,
        *,
        decided_by: str,
        decision: WriteStatus,
        decider_is_admin: bool = False,
    ) -> PendingWrite:
        """Atomically move an open write to ``APPROVED`` or ``REJECTED``.

        Raises :class:`WriteRefusal` if the write is missing, already settled,
        expired, or not this person's to decide. Single-use by construction:
        the second caller finds a settled row.
        """

    @abstractmethod
    async def mark(
        self,
        context: "ToolContext",
        pending_id: str,
        *,
        status: WriteStatus,
        error: Optional[str] = None,
    ) -> None:
        """Record the outcome of an approved write."""

    @abstractmethod
    async def list_open(
        self,
        context: "ToolContext",
        *,
        status: Optional[WriteStatus] = None,
        limit: int = 50,
    ) -> List[PendingWrite]:
        """Open writes for this tenant, newest first -- the review queue."""

    @abstractmethod
    async def expire_stale(self, context: "ToolContext") -> int:
        """Settle writes whose TTL has passed. Returns how many.

        Worth running on a schedule even though :meth:`claim` and
        :func:`assert_still_valid` both refuse an expired write on their own.
        Without a sweep the console keeps showing a live-looking approve button
        on a card that is already dead, which teaches people that the buttons
        do not mean anything.
        """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Storage round-trips lose the tzinfo on some drivers, and comparing a naive
    to an aware datetime raises -- which would turn a stale row into a crash
    instead of an expiry.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
