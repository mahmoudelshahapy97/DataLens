"""Propose, decide, execute -- the three moves, in one place.

A write is decided in a conversation and sometimes in an HTTP request, and both
paths must apply exactly the same checks. Putting the sequence here rather than
in the tool means the approve button and the ``/confirm`` message cannot drift
apart, and that re-authorization cannot be forgotten by whichever caller is
written second.

The sequence, and why it is in this order:

1. **Propose.** Build the policy from current grants, validate the plan, store
   it. Nothing is executed. If the plan cannot be authorized the turn still
   *succeeds* -- a refused write is a conversation, not a fault.
2. **Decide.** One atomic claim. Single-use, and only by someone entitled to.
3. **Execute.** Rebuild the policy from scratch, re-parse the stored plan
   through its model, re-validate, and only then compare against what was
   approved. The stored statements are never run as stored; they are rebuilt
   and checked to be identical. A row edited in the database between approval
   and execution must not be able to skip a validator.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from .approval import (
    DEFAULT_TTL_SECONDS,
    PendingWrite,
    WriteApprovalMode,
    WriteApprovalStore,
    WriteStatus,
    assert_still_valid,
    requires_second_person,
)
from .errors import WriteCode, WriteRefusal
from .models import WritePlan
from .policy import WritePolicy, build_write_policy
from .validator import ValidatedWrite, validate_write_plan

if TYPE_CHECKING:  # pragma: no cover
    from ...capabilities.sql_runner.write import WriteResult
    from ..grants import GrantStore
    from ..tool import ToolContext

logger = logging.getLogger("vanna.write")


class WriteService:
    """Everything a write needs, wired once.

    ``runner`` must implement ``execute_write``; ``catalog`` must answer
    ``get_tables``. Both are injected rather than constructed so a deployment
    can point writes at a different credential from reads simply by passing a
    different runner.
    """

    def __init__(
        self,
        *,
        grants: "GrantStore",
        catalog: Any,
        approvals: WriteApprovalStore,
        runner: Any,
        data_source_id: str,
        dialect: str,
        max_rows: int = 50,
        approval_mode: WriteApprovalMode = WriteApprovalMode.SELF,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        audit_logger: Any = None,
        roles_for: Optional[Any] = None,
    ) -> None:
        self.grants = grants
        self.catalog = catalog
        self.approvals = approvals
        self.runner = runner
        self.data_source_id = data_source_id
        self.dialect = dialect
        self.max_rows = max_rows
        self.approval_mode = approval_mode
        self.ttl_seconds = ttl_seconds
        self.audit_logger = audit_logger
        # How to read a caller's roles. Defaults to the group memberships on
        # the resolved user, which is the only source that cannot be supplied
        # by the caller themselves.
        self._roles_for = roles_for or _default_roles

    # -- authorization -------------------------------------------------

    async def build_policy(self, context: "ToolContext") -> WritePolicy:
        """What this caller may change, right now.

        Rebuilt on every call. Caching it is the one optimisation that must not
        be made here: the value's whole purpose is to be current at the moment
        it is used.
        """
        grants = await self.grants.resolve(
            context,
            data_source_id=self.data_source_id,
            roles=self._roles_for(context),
        )
        tables = await self.catalog.get_tables(
            context, data_source_id=self.data_source_id
        )
        return build_write_policy(
            grants,
            tables or [],
            dialect=self.dialect,
            max_rows=self.max_rows,
        )

    async def catalog_fingerprint(self, context: "ToolContext") -> Optional[str]:
        """A hash of the catalog, so a schema change can invalidate an approval."""
        try:
            return await self.catalog.catalog_hash(
                context, data_source_id=self.data_source_id
            )
        except Exception:  # pragma: no cover - optional capability
            return None

    @property
    def paramstyle(self) -> str:
        return getattr(self.runner, "paramstyle", "format")

    # -- 1. propose ----------------------------------------------------

    async def propose(
        self, context: "ToolContext", plan: WritePlan
    ) -> Tuple[PendingWrite, ValidatedWrite]:
        """Authorize a plan and park it for a decision. Executes nothing.

        Raises :class:`WriteRefusal` if the plan cannot be authorized. The
        caller should render that as an answer, not as an error.
        """
        policy = await self.build_policy(context)
        validated = validate_write_plan(plan, policy, paramstyle=self.paramstyle)

        if requires_second_person(self.approval_mode, validated.is_destructive):
            if not await self._has_eligible_approver(context):
                # Better to refuse now than to park a request nobody can ever
                # decide, which would look identical to one merely waiting.
                raise WriteRefusal(
                    WriteCode.APPROVER_UNAVAILABLE,
                    "This change needs a second administrator to approve it, "
                    "and there is no one else who can.",
                )

        pending = PendingWrite.propose(
            validated,
            tenant_id=_tenant(context),
            data_source_id=self.data_source_id,
            requested_by=_user_id(context),
            requested_by_email=_user_email(context),
            plan=plan.model_dump(mode="json"),
            mode=self.approval_mode,
            catalog_fingerprint=await self.catalog_fingerprint(context),
            conversation_id=getattr(context, "conversation_id", None),
            request_id=getattr(context, "request_id", None),
            ttl_seconds=self.ttl_seconds,
        )
        stored = await self.approvals.create(context, pending)
        await self._audit(context, "proposed", stored)
        return stored, validated

    # -- 2. decide -----------------------------------------------------

    async def decide(
        self,
        context: "ToolContext",
        pending_id: str,
        *,
        approve: bool,
        decided_by: Optional[str] = None,
        decider_is_admin: bool = False,
    ) -> PendingWrite:
        """Approve or decline. Atomic, single-use, and audited either way."""
        decided = await self.approvals.claim(
            context,
            pending_id,
            decided_by=decided_by or _user_id(context),
            decision=WriteStatus.APPROVED if approve else WriteStatus.REJECTED,
            decider_is_admin=decider_is_admin,
        )
        await self._audit(context, "approved" if approve else "rejected", decided)
        return decided

    # -- 3. execute ----------------------------------------------------

    async def execute(
        self, context: "ToolContext", pending: PendingWrite
    ) -> "WriteResult":
        """Re-authorize from scratch, then run.

        Every input is re-derived. The stored plan is re-parsed through
        :class:`WritePlan` so a row edited in the database cannot skip a
        validator, the policy is rebuilt from current grants, and the freshly
        built statements are compared against the approved hash before anything
        touches the database.
        """
        policy = await self.build_policy(context)
        plan = WritePlan.model_validate(pending.plan)
        validated = validate_write_plan(plan, policy, paramstyle=self.paramstyle)

        assert_still_valid(
            pending,
            plan_hash=validated.plan_hash,
            grants_version=policy.grants_version,
            catalog_fingerprint=await self.catalog_fingerprint(context),
        )

        try:
            result = await self.runner.execute_write(validated, context)
        except Exception as exc:
            refusal = _as_refusal(exc)
            await self.approvals.mark(
                context, pending.id, status=WriteStatus.FAILED, error=str(refusal)
            )
            await self._audit(context, "failed", pending, error=str(refusal))
            raise refusal from exc

        await self.approvals.mark(context, pending.id, status=WriteStatus.EXECUTED)
        await self._audit(context, "executed", pending, rows=result.rows_affected)
        return result

    async def decide_and_execute(
        self,
        context: "ToolContext",
        pending_id: str,
        *,
        approve: bool,
        decided_by: Optional[str] = None,
        decider_is_admin: bool = False,
    ) -> Tuple[PendingWrite, Optional["WriteResult"]]:
        """The whole second half, for a caller that has one button.

        A refusal during re-authorization settles the row as ``REFUSED`` before
        propagating, so a write that was approved but could not run does not
        sit in the queue looking approvable.
        """
        decided = await self.decide(
            context,
            pending_id,
            approve=approve,
            decided_by=decided_by,
            decider_is_admin=decider_is_admin,
        )
        if not approve:
            return decided, None

        try:
            return decided, await self.execute(context, decided)
        except WriteRefusal as refusal:
            if refusal.code in (
                WriteCode.CONFIRMATION_MISMATCH,
                WriteCode.CONFIRMATION_EXPIRED,
                WriteCode.CONFIRMATION_NOT_PENDING,
            ):
                await self.approvals.mark(
                    context, pending_id, status=WriteStatus.REFUSED, error=str(refusal)
                )
                await self._audit(context, "refused", decided, error=str(refusal))
            raise

    # -- helpers -------------------------------------------------------

    async def pending_for_review(
        self, context: "ToolContext", *, limit: int = 50
    ) -> List[PendingWrite]:
        return await self.approvals.list_open(
            context, status=WriteStatus.AWAITING_REVIEW, limit=limit
        )

    async def _has_eligible_approver(self, context: "ToolContext") -> bool:
        """Whether anyone other than the requester could approve.

        Overridable: the default assumes a deployment that can answer this
        cannot be asked from here, and so says yes. A deployment with a user
        directory should subclass and answer honestly, because the alternative
        is a request that waits forever.
        """
        return True

    async def _audit(
        self,
        context: "ToolContext",
        event: str,
        pending: PendingWrite,
        *,
        rows: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record what happened. Never breaks the write.

        Records the parameterized preview and never the bound values: those are
        tenant data, and an audit log is exactly the place they must not
        accumulate.
        """
        if self.audit_logger is None:
            return
        details: Dict[str, Any] = {
            "pending_write_id": pending.id,
            "operation": pending.operation,
            "tables": pending.tables,
            "expected_row_count": pending.expected_row_count,
            "is_destructive": pending.is_destructive,
            "plan_hash": pending.plan_hash,
            "statement_preview": pending.statement_preview,
            "requested_by": pending.requested_by,
            "decided_by": pending.decided_by,
        }
        if rows is not None:
            details["rows_affected"] = rows
        if error:
            details["error"] = error
        try:
            await self.audit_logger.log_write_event(
                user=getattr(context, "user", None),
                event=event,
                details=details,
                context=context,
            )
        except Exception as exc:  # pragma: no cover - bookkeeping is never fatal
            logger.debug("Write audit not recorded: %s", exc)


def _as_refusal(exc: Exception) -> WriteRefusal:
    """Map an execution failure onto the closed vocabulary.

    The reference implementation this was ported from left the constraint and
    unsupported-engine cases unmapped, so a duplicate key reached the user as
    an internal error. Mapping them here is the fix.
    """
    from ...capabilities.sql_runner.write import (
        ConstraintViolated,
        UnexpectedRowCount,
        WritesNotSupported,
    )

    if isinstance(exc, WriteRefusal):
        return exc
    if isinstance(exc, UnexpectedRowCount):
        return WriteRefusal(
            WriteCode.ROW_COUNT_MISMATCH,
            f"This was approved to change {exc.expected} row(s) but would have "
            f"changed {exc.actual}, so nothing was changed.",
            step=exc.step,
        )
    if isinstance(exc, ConstraintViolated):
        return WriteRefusal(
            WriteCode.CONSTRAINT_VIOLATED,
            {
                "unique": "That would duplicate a value the table requires to be unique.",
                "foreign_key": "That would leave a reference pointing at a row that does not exist.",
                "not_null": "That would leave a required field empty.",
                "check": "That value is not one the table accepts.",
            }.get(exc.kind, "The database refused the change."),
            detail=exc.kind,
        )
    if isinstance(exc, WritesNotSupported):
        return WriteRefusal(WriteCode.NOT_SUPPORTED, str(exc))
    raise exc


def _default_roles(context: "ToolContext") -> Sequence[str]:
    user = getattr(context, "user", None)
    return list(getattr(user, "group_memberships", None) or [])


def _tenant(context: "ToolContext") -> str:
    return getattr(context, "tenant_id", "default") or "default"


def _user_id(context: "ToolContext") -> str:
    return str(getattr(getattr(context, "user", None), "id", "") or "")


def _user_email(context: "ToolContext") -> Optional[str]:
    return getattr(getattr(context, "user", None), "email", None)
