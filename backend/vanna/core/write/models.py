"""The typed write plan: a proposal to change data, never free-form SQL.

This is the whole security argument for writes, and it is a structural one
rather than a filtering one.

A language model asked to change a row could emit a SQL string, and the platform
could parse it and decide whether to allow it. That design has a permanent
weakness: the parser and the database must agree about what the string means, and
every disagreement between them is a bypass. The list of shapes that must be
rejected is also open-ended and grows with every dialect feature.

So the model does not write SQL. It names a table, some columns, some values, and
which rows it means -- and the platform *builds* the statement from expression
nodes. There is no text to interpolate anywhere, so shapes that would be
dangerous are not so much rejected as **unrepresentable**:

* An unfiltered ``UPDATE`` or ``DELETE`` -- ``predicate`` is required by a
  validator, and cannot be an empty list.
* A subquery, a computed assignment, ``quantity = quantity + 1`` -- an assignment
  holds a value or a reference to an earlier step, and neither is an expression.
* ``DROP``, ``TRUNCATE``, ``GRANT`` -- ``operation`` is a three-member Literal.
* A range or ``LIKE`` predicate -- :class:`KeyPredicate` is equality only.
* A cyclic dependency between steps -- a reference may only point at a lower
  index, so the graph cannot contain one.

What remains representable but wrong is caught by the validator, which checks the
plan against what the caller may actually touch. What remains after *that* is
caught by the row-count assertion at execution time.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Ceiling on statements in one transaction. A business action is rarely one row
#: -- placing an order writes an order and its lines -- but an action needing
#: more than ten statements is one nobody can meaningfully approve from a card.
MAX_TRANSACTION_STEPS: int = 10

#: What a value may be. Deliberately not `Any`: a dict or a list here would be a
#: structure the renderer has no way to bind, and accepting one would mean
#: discovering that at execution time.
JsonScalar = Union[str, int, float, bool, None]


class StepReference(BaseModel):
    """A value taken from a row an earlier step in this transaction created.

    The reason multi-step plans exist. A child row's foreign key must equal a key
    the database generates for its parent, and that key does not exist when the
    plan is written -- so it cannot be a literal, and it must not be a string
    template. This is a typed edge in a graph: there is no text to interpolate,
    so the guarantee above survives intact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_step: int = Field(
        ge=0,
        lt=MAX_TRANSACTION_STEPS,
        description="Index of an earlier step in this plan. Must be strictly "
        "less than the index of the step carrying it.",
    )
    column: str = Field(
        min_length=1,
        max_length=256,
        description="The column on that step's table whose value is wanted -- "
        "in practice its generated primary key.",
    )


class ColumnAssignment(BaseModel):
    """One ``column = value`` pair in an INSERT or UPDATE.

    The value is data, never SQL. It reaches the database as a bound parameter,
    so there is no expression here to inject into.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str = Field(min_length=1, max_length=256)
    value: JsonScalar = Field(
        default=None, description="A literal value. Bound, never interpolated."
    )
    reference: Optional[StepReference] = Field(
        default=None,
        description="Take the value from a row an earlier step created, instead "
        "of stating it. Use for a foreign key whose parent this plan inserts.",
    )

    @model_validator(mode="after")
    def one_source_of_value(self) -> "ColumnAssignment":
        if self.reference is not None and self.value is not None:
            raise ValueError(
                "an assignment carries a value or a reference, never both"
            )
        return self


class KeyPredicate(BaseModel):
    """One ``column = value`` equality restricting an UPDATE or DELETE.

    Equality only, and the validator further requires the column to be a key. A
    range or a ``LIKE`` would let one approved statement touch an unbounded set
    of rows, which is precisely what the promised row count exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str = Field(min_length=1, max_length=256)
    value: JsonScalar = None


class WriteStep(BaseModel):
    """One statement in a write transaction."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["insert", "update", "delete"]
    table: str = Field(min_length=1, max_length=512)
    assignments: List[ColumnAssignment] = Field(default_factory=list, max_length=200)
    predicate: List[KeyPredicate] = Field(default_factory=list, max_length=20)
    expected_row_count: int = Field(
        ge=0,
        le=10_000,
        description="What THIS step promises to touch, asserted inside the "
        "transaction before commit. Per step and never a total: 'one order plus "
        "two lines' is three separate promises, and a single sum would let one "
        "step's overshoot hide behind another's undershoot.",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=200,
        description="One short line shown beside the statement on the approval "
        "card, so a two-step transaction reads as 'create the order' / 'add the "
        "line item' rather than as two blocks of SQL.",
    )

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "WriteStep":
        columns = [item.column.casefold().strip() for item in self.assignments]
        if len(set(columns)) != len(columns):
            raise ValueError("a column is assigned twice in one step")

        constrained = [item.column.casefold().strip() for item in self.predicate]
        if len(set(constrained)) != len(constrained):
            raise ValueError("a column is constrained twice in one step")

        if self.operation == "insert":
            if not self.assignments:
                raise ValueError("an insert must assign at least one column")
            if self.predicate:
                # There is no existing row to restrict. A predicate here would
                # mean the model has confused inserting with updating, and
                # honouring it would produce a statement that cannot be built.
                raise ValueError("an insert cannot carry a predicate")
        elif self.operation == "update":
            if not self.assignments:
                raise ValueError("an update must assign at least one column")
            if not self.predicate:
                raise ValueError(
                    "an update must carry a predicate: an unrestricted update "
                    "changes every row in the table"
                )
        else:  # delete
            if self.assignments:
                raise ValueError("a delete cannot assign columns")
            if not self.predicate:
                raise ValueError(
                    "a delete must carry a predicate: an unrestricted delete "
                    "empties the table"
                )

        if self.operation != "insert":
            for item in self.assignments:
                if item.reference is not None:
                    # A reference names a row created by this transaction, so
                    # only a step creating rows can consume one.
                    raise ValueError(
                        "only an insert step may take a value from an earlier step"
                    )
        return self

    @property
    def is_destructive(self) -> bool:
        """Whether this step is one a user should be warned about twice.

        A delete always. An update only when it touches more than one row --
        a single-row correction is the ordinary case and warning about it
        teaches people to click through warnings.
        """
        return self.operation == "delete" or (
            self.operation == "update" and self.expected_row_count > 1
        )


class WritePlan(BaseModel):
    """A typed proposal to modify data, as one transaction.

    A business action is rarely one row: placing an order writes an order and its
    lines; taking a booking writes a booking and its payment. Those must commit
    together or not at all, which is why this is a list of steps -- and why a step
    may take an assignment's value from an earlier step rather than stating it.
    """

    model_config = ConfigDict(extra="forbid")

    steps: List[WriteStep] = Field(min_length=1, max_length=MAX_TRANSACTION_STEPS)

    @model_validator(mode="after")
    def references_point_back_at_single_row_inserts(self) -> "WritePlan":
        for index, step in enumerate(self.steps):
            for assignment in step.assignments:
                reference = assignment.reference
                if reference is None:
                    continue
                if reference.from_step >= index:
                    # Forward and self references are both refused here, so the
                    # dependency graph can only ever point at a lower index --
                    # which makes a cycle unrepresentable rather than merely
                    # detected.
                    raise ValueError(
                        f"step {index} references step {reference.from_step}, "
                        "which does not run before it"
                    )
                parent = self.steps[reference.from_step]
                if parent.operation != "insert":
                    raise ValueError(
                        f"step {index} takes a value from step "
                        f"{reference.from_step}, which is not an insert and so "
                        "creates no row to take it from"
                    )
                if parent.expected_row_count != 1:
                    # With two parent rows there are two candidate keys and no
                    # rule for picking one. Refusing is the only honest answer.
                    raise ValueError(
                        f"step {index} takes a value from step "
                        f"{reference.from_step}, which promises "
                        f"{parent.expected_row_count} rows rather than exactly one"
                    )
        return self

    @property
    def total_expected_rows(self) -> int:
        return sum(step.expected_row_count for step in self.steps)

    @property
    def is_destructive(self) -> bool:
        return any(step.is_destructive for step in self.steps)

    @property
    def operation(self) -> str:
        """The verb to label this plan with -- or ``transaction`` for several."""
        return self.steps[0].operation if len(self.steps) == 1 else "transaction"

    @property
    def tables(self) -> List[str]:
        """Every table this plan touches, in step order, without duplicates."""
        seen: List[str] = []
        for step in self.steps:
            if step.table not in seen:
                seen.append(step.table)
        return seen
