"""Why a write was refused.

A closed vocabulary, for the same reason :class:`vanna.core.sql_policy.ViolationCode`
is one: these codes are logged, alerted on, counted, and shown to a user as the
reason a change did not happen. Branching on message text is how a refusal turns
into a bug the first time someone improves the wording.

``WriteRefusal.__init__`` rejects a code it does not know. That looks like
pedantry until you notice the shape of the bug it prevents: the reference
implementation this was ported from raises ``write_predicate_incomplete_key`` in
its validator and forgot to add it to its own vocabulary, so a partial
composite-key predicate -- exactly the case the check exists for -- raises a
``ValueError`` about an unknown code and surfaces to the user as an internal
error rather than as the refusal it is. Making the constructor strict turns that
into a failure the test suite catches instead of one production discovers.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class WriteCode(str, Enum):
    """Every reason a write can be refused."""

    # -- authorization ------------------------------------------------
    NOT_ENABLED = "write_not_enabled"
    """Nothing is writable for this caller. The normal state, not a fault."""

    TABLE_NOT_ALLOWED = "write_table_not_allowed"
    """The table is unknown to the caller, or ambiguous and so not guessed at."""

    OPERATION_NOT_ALLOWED = "write_operation_not_allowed"
    """The table is writable, but not with this verb."""

    COLUMN_NOT_ALLOWED = "write_column_not_allowed"
    """The column exists and is readable, but may not be assigned."""

    COLUMN_UNKNOWN = "write_column_unknown"
    """No such column on that table, as far as this caller can see."""

    # -- statement shape ----------------------------------------------
    PREDICATE_REQUIRED = "write_predicate_required"
    """An UPDATE or DELETE with nothing restricting it -- i.e. all rows."""

    PREDICATE_NOT_KEY = "write_predicate_not_key"
    """A predicate column that is not a key. The bound on blast radius."""

    PREDICATE_INCOMPLETE_KEY = "write_predicate_incomplete_key"
    """Part of a composite key. Matches every row sharing the partial key,
    which is not the one row the approval card implies."""

    MISSING_REQUIRED_COLUMN = "write_missing_required_column"
    """An INSERT omitting a column the database will demand."""

    # -- limits --------------------------------------------------------
    ROW_LIMIT_EXCEEDED = "write_row_limit_exceeded"
    STEP_LIMIT_EXCEEDED = "write_step_limit_exceeded"

    # -- cross-step references -----------------------------------------
    REFERENCE_NOT_KEY = "write_reference_not_key"
    """A step took a value from a column that is not its table's key."""

    REFERENCE_NOT_RELATED = "write_reference_not_related"
    """No declared foreign key links those two columns in that direction."""

    RETURNING_UNSUPPORTED = "write_returning_unsupported"
    """The dialect cannot return a generated key, so a multi-step plan that
    needs one cannot be built for it."""

    # -- execution -----------------------------------------------------
    CONSTRAINT_VIOLATED = "write_constraint_violated"
    """The database refused: unique, foreign key, check or not-null."""

    ROW_COUNT_MISMATCH = "write_row_count_mismatch"
    """The statement touched a different number of rows than it promised.
    Raised inside the transaction, so nothing was committed."""

    NOT_SUPPORTED = "write_not_supported"
    """This engine's runner cannot execute writes at all."""

    # -- approval ------------------------------------------------------
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    """Something moved between approval and execution: the statement, the
    grants, or the schema. Which one is named in the message."""
    CONFIRMATION_NOT_PENDING = "confirmation_not_pending"
    CONFIRMATION_FORBIDDEN = "confirmation_forbidden"
    APPROVER_UNAVAILABLE = "approver_unavailable"
    """Second-person approval is required and nobody eligible exists. Refused
    at proposal time rather than parked as an unapprovable request."""

    CREDENTIAL_MISSING = "write_credential_missing"
    """Writes are configured to use a separate credential and it is absent.
    Absence is a refusal, never a fall back to the reading credential."""


#: Codes that describe a *proposal* that can never work, as opposed to one that
#: failed against the world. A caller may usefully re-plan after these; there is
#: no point re-planning after a row-count mismatch.
REPAIRABLE_CODES = frozenset({
    WriteCode.TABLE_NOT_ALLOWED,
    WriteCode.OPERATION_NOT_ALLOWED,
    WriteCode.COLUMN_NOT_ALLOWED,
    WriteCode.COLUMN_UNKNOWN,
    WriteCode.PREDICATE_REQUIRED,
    WriteCode.PREDICATE_NOT_KEY,
    WriteCode.PREDICATE_INCOMPLETE_KEY,
    WriteCode.MISSING_REQUIRED_COLUMN,
    WriteCode.ROW_LIMIT_EXCEEDED,
    WriteCode.STEP_LIMIT_EXCEEDED,
    WriteCode.REFERENCE_NOT_KEY,
    WriteCode.REFERENCE_NOT_RELATED,
})


class WriteRefusal(Exception):
    """A write was refused, with a code from the closed vocabulary above.

    Carries no SQL and no bound values. The message names a table or column at
    most -- the same rule :class:`vanna.core.sql_policy.PolicyViolation` follows,
    and for the same reason: this text reaches logs and users, and the values in
    a write plan are tenant data.
    """

    def __init__(
        self,
        code: "WriteCode | str",
        message: str,
        *,
        detail: Optional[str] = None,
        step: Optional[int] = None,
    ) -> None:
        try:
            self.code = WriteCode(code)
        except ValueError as exc:
            raise ValueError(
                f"unknown write refusal code: {code!r}. Add it to WriteCode "
                "rather than passing a string -- an unlisted code becomes an "
                "internal error at exactly the moment a user needed a reason."
            ) from exc
        self.message = message
        self.detail = detail
        self.step = step
        super().__init__(message)

    @property
    def repairable(self) -> bool:
        """Whether re-planning could plausibly produce an acceptable proposal."""
        return self.code in REPAIRABLE_CODES

    def to_vanna_error(self):
        """Convert to the framework-wide error envelope."""
        from ..errors import ErrorCode, ErrorPhase, VannaError

        execution = {
            WriteCode.CONSTRAINT_VIOLATED,
            WriteCode.ROW_COUNT_MISMATCH,
            WriteCode.NOT_SUPPORTED,
        }
        phase = (
            ErrorPhase.SQL_EXECUTION
            if self.code in execution
            else ErrorPhase.SQL_POLICY_CHECK
        )
        metadata = {"code": self.code.value}
        if self.detail:
            metadata["detail"] = self.detail
        if self.step is not None:
            metadata["step"] = self.step
        return VannaError(
            ErrorCode.POLICY_VIOLATION,
            self.message,
            phase=phase,
            metadata=metadata,
            cause=self,
        )

    def __str__(self) -> str:
        return self.message
