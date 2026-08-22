"""Executing a validated write: one transaction, one promise per statement.

``SqlRunner`` is one method returning a DataFrame, which is the whole contract a
read needs. A write needs three things that shape has nowhere to put: bound
parameters, several statements committing together, and a way to fail *after*
some of them have succeeded.

The load-bearing rule is the row-count assertion. Every step said how many rows
it would touch, a person approved that number, and the executor holds the
statement to it -- exact equality, not a ceiling. The check runs **inside** the
transaction, so a mismatch on the third statement rolls back the first two as
well. An UPDATE that promised one row and matched four hundred is not a partial
success to report; it is a misunderstanding to undo.

Everything here is a plain data contract. The runners implement it; nothing in
this module touches a database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:  # pragma: no cover
    from ...core.tool import ToolContext
    from ...core.write.validator import ValidatedWrite


class WriteStepResult(BaseModel):
    """What one statement actually did."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    operation: str
    rows_affected: int
    #: Values this step returned, for later steps to bind. Native driver
    #: objects, not strings -- see the note in the Postgres runner about why
    #: rendering a numeric key as text makes a foreign key match nothing.
    returned: Dict[str, Any] = Field(default_factory=dict)


class WriteResult(BaseModel):
    """What the whole transaction did, once committed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: List[WriteStepResult] = Field(default_factory=list)
    committed: bool = False
    duration_ms: Optional[float] = None

    @property
    def rows_affected(self) -> int:
        return sum(step.rows_affected for step in self.steps)


class UnexpectedRowCount(Exception):
    """A statement touched a different number of rows than it promised.

    Raised inside the transaction so the driver rolls everything back. Carries
    both numbers and the step's position, because "expected 1, matched 412, on
    step 2 of 3" is a message a person can act on and "the write failed" is not.
    """

    def __init__(self, *, expected: int, actual: int, step: int = 0, steps: int = 1) -> None:
        self.expected = expected
        self.actual = actual
        self.step = step
        self.steps = steps
        where = f" on step {step + 1} of {steps}" if steps > 1 else ""
        super().__init__(
            f"expected to change {expected} row(s) but matched {actual}{where}"
        )


class ConstraintViolated(Exception):
    """The database refused the write.

    ``kind`` is drawn from a closed vocabulary rather than the driver's message,
    which varies by engine and version and often names the schema.
    """

    KINDS = ("unique", "foreign_key", "check", "not_null", "constraint")

    def __init__(self, kind: str = "constraint") -> None:
        self.kind = kind if kind in self.KINDS else "constraint"
        super().__init__(f"the database refused the change ({self.kind})")


class WritesNotSupported(Exception):
    """This runner cannot execute writes.

    The default for every engine that has not implemented the contract. Raised
    rather than silently doing nothing, because the failure mode being avoided
    is a runner that accepts DML, never commits it, and reports success -- which
    is what MySQL did here before this contract existed.
    """


@runtime_checkable
class WriteRunner(Protocol):
    """A runner that can execute a validated write."""

    #: How this runner's driver spells a bind parameter. Must match the
    #: paramstyle the statements were rendered with.
    paramstyle: str

    async def execute_write(
        self, validated: "ValidatedWrite", context: "ToolContext"
    ) -> WriteResult:
        """Run every step in one transaction, or none of them."""
        ...


def bind_parameters(
    parameters: List[Any],
    bindings: List[Any],
    results: List[WriteStepResult],
) -> List[Any]:
    """Fill bound positions from earlier steps' returned values.

    Kept here rather than in each runner so every engine resolves a cross-step
    reference the same way, and so the one rule that matters is stated once:
    the value is copied as the driver returned it. Rendering a numeric key as a
    string here is how a foreign key ends up matching nothing.
    """
    filled = list(parameters)
    for binding in bindings:
        source = results[binding.from_step]
        if binding.column not in source.returned:
            raise ConstraintViolated("foreign_key")
        filled[binding.position - 1] = source.returned[binding.column]
    return filled
