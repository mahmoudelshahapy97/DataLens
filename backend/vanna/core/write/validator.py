"""Builds the statement. Never parses one.

This module receives no SQL at all. It receives a :class:`WritePlan` and a
:class:`WritePolicy`, checks the one against the other, and then **builds** the
statement itself from sqlglot expression nodes. Nothing here formats a string
with a value in it, so the class of bug where a validator and a database disagree
about what a string means cannot arise.

Values never appear in the SQL. Every one becomes a bound parameter, and the
placeholder marker is chosen by the *driver* that will bind them while the
identifier quoting is chosen by the *dialect* -- two separate concerns that a
single "render for postgres" would silently conflate. ``exp.Var`` carries the
marker through unchanged in every dialect, which is why there is no
search-and-replace pass over the finished SQL.

The output is a :class:`ValidatedWrite`: the exact statements, their parameters
in bind order, the cross-step bindings, and a hash over all of it. Everything
downstream -- the approval card, the re-authorization check, the executor --
reads that object and never re-derives anything from the plan.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from ..grants import normalize_identifier
from .errors import WriteCode, WriteRefusal
from .models import JsonScalar, WritePlan, WriteStep
from .policy import WritableTable, WritePolicy

#: How a driver spells a bind parameter. The dialect decides how identifiers are
#: quoted; this decides how values are marked. psycopg2 and pymysql both want
#: ``%s``; sqlite3 and pyodbc want ``?``; asyncpg wants ``$1``.
PARAMSTYLES = {
    "format": lambda position: "%s",
    "qmark": lambda position: "?",
    "numeric": lambda position: f":{position}",
    "dollar": lambda position: f"${position}",
}


class StepBinding(BaseModel):
    """A parameter whose value comes from an earlier step's RETURNING clause."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1, description="1-based index into this step's parameters.")
    from_step: int = Field(ge=0)
    column: str


class ValidatedWriteStep(BaseModel):
    """One built statement, ready to bind and run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    sql: str
    parameters: List[JsonScalar] = Field(default_factory=list)
    bindings: List[StepBinding] = Field(default_factory=list)
    returning_columns: List[str] = Field(default_factory=list)
    operation: str
    table: str
    schema_name: Optional[str] = None
    expected_row_count: int = Field(ge=0)
    description: Optional[str] = None

    @model_validator(mode="after")
    def bindings_address_empty_positions(self) -> "ValidatedWriteStep":
        for binding in self.bindings:
            if binding.position > len(self.parameters):
                raise ValueError(
                    "a binding names a parameter position that does not exist"
                )
            if self.parameters[binding.position - 1] is not None:
                # A bound position holds a placeholder for a value that does not
                # exist yet. Finding a literal there means two sources disagree
                # about what that parameter is, and the executor would silently
                # honour one.
                raise ValueError("a bound position must not also carry a literal")
        return self

    @property
    def is_destructive(self) -> bool:
        return self.operation == "delete" or (
            self.operation == "update" and self.expected_row_count > 1
        )


class ValidatedWrite(BaseModel):
    """Everything an approval card shows and an executor runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dialect: str
    paramstyle: str
    steps: List[ValidatedWriteStep] = Field(min_length=1)
    plan_hash: str
    grants_version: int = 0

    @model_validator(mode="after")
    def bindings_point_backwards(self) -> "ValidatedWrite":
        for index, step in enumerate(self.steps):
            for binding in step.bindings:
                if binding.from_step >= index:
                    raise ValueError(
                        "a statement may only bind from one that ran before it"
                    )
                parent = self.steps[binding.from_step]
                if binding.column not in parent.returning_columns:
                    raise ValueError(
                        "a binding names a column that statement does not return"
                    )
        return self

    @property
    def operation(self) -> str:
        return self.steps[0].operation if len(self.steps) == 1 else "transaction"

    @property
    def expected_row_count(self) -> int:
        return sum(step.expected_row_count for step in self.steps)

    @property
    def is_destructive(self) -> bool:
        return any(step.is_destructive for step in self.steps)

    @property
    def statement_preview(self) -> str:
        """The statements as they will run, still parameterized.

        Safe to store and to show: the markers stand where tenant data would be,
        so a preview can be kept in an audit trail that the values must not enter.
        """
        return ";\n".join(step.sql for step in self.steps)

    @property
    def parameter_summary(self) -> Dict[str, object]:
        """Shapes, never values -- for an audit record or an API response."""
        return {
            "count": sum(len(step.parameters) for step in self.steps),
            "steps": [
                {
                    "index": step.index,
                    "table": step.table,
                    "operation": step.operation,
                    "expected_row_count": step.expected_row_count,
                    "description": step.description,
                }
                for step in self.steps
            ],
        }

    @property
    def tables(self) -> List[str]:
        seen: List[str] = []
        for step in self.steps:
            if step.table not in seen:
                seen.append(step.table)
        return seen


def validate_write_plan(
    plan: WritePlan,
    policy: WritePolicy,
    *,
    paramstyle: str = "format",
) -> ValidatedWrite:
    """Authorize a plan and build its statements, or refuse with a coded reason.

    The order of checks is deliberate: the cheap whole-plan limits come first so
    an oversized plan is refused without touching the catalog, and the per-step
    authorization runs before anything is rendered so no SQL is ever built for a
    statement the caller may not run.
    """
    if paramstyle not in PARAMSTYLES:
        raise ValueError(
            f"unknown paramstyle {paramstyle!r}; expected one of "
            f"{', '.join(sorted(PARAMSTYLES))}"
        )

    if policy.is_empty:
        raise WriteRefusal(
            WriteCode.NOT_ENABLED,
            "Nothing is writable here. Ask an administrator to grant write "
            "access to the tables you need to change.",
        )

    if len(plan.steps) > policy.max_steps:
        raise WriteRefusal(
            WriteCode.STEP_LIMIT_EXCEEDED,
            f"This change needs {len(plan.steps)} statements; at most "
            f"{policy.max_steps} may run together.",
        )

    total = plan.total_expected_rows
    if total > policy.max_rows:
        raise WriteRefusal(
            WriteCode.ROW_LIMIT_EXCEEDED,
            f"This change would affect {total} rows; at most {policy.max_rows} "
            "may be changed at once.",
        )

    steps = [
        _validate_step(index, step, plan, policy, paramstyle)
        for index, step in enumerate(plan.steps)
    ]
    steps = _apply_returning(steps, policy)

    return ValidatedWrite(
        dialect=policy.dialect,
        paramstyle=paramstyle,
        steps=steps,
        plan_hash=plan_hash(steps),
        grants_version=policy.grants_version,
    )


def plan_hash(steps: Sequence[ValidatedWriteStep]) -> str:
    """A stable fingerprint of exactly what will run, in order.

    Covers the parameters as well as the statements: ``SET city = %s`` approved
    for 'Berlin' must not execute with 'Bochum'. Covers each step's promised row
    count, because those are what the transaction is held to. And covers the
    bindings, because "put step 0's order_id here" is part of what was approved
    -- swapping it for a literal must not hash the same.
    """
    material = json.dumps(
        [
            {
                "sql": step.sql,
                "parameters": step.parameters,
                "bindings": [b.model_dump(mode="json") for b in step.bindings],
                "returning": step.returning_columns,
                "rows": step.expected_row_count,
            }
            for step in steps
        ],
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


# ----------------------------------------------------------------------
# Per-step authorization
# ----------------------------------------------------------------------


def _validate_step(
    index: int,
    step: WriteStep,
    plan: WritePlan,
    policy: WritePolicy,
    paramstyle: str,
) -> ValidatedWriteStep:
    table = policy.resolve_table(step.table)
    if table is None:
        # Covers both "no such writable table" and "the bare name matched two".
        # The message does not distinguish them: telling a caller that a name
        # they may not use is ambiguous still tells them it exists.
        raise WriteRefusal(
            WriteCode.TABLE_NOT_ALLOWED,
            f"You do not have write access to {step.table}.",
            detail=step.table,
            step=index,
        )

    if not table.permits(step.operation):
        raise WriteRefusal(
            WriteCode.OPERATION_NOT_ALLOWED,
            f"You may not {step.operation} rows in {table.name}.",
            detail=table.name,
            step=index,
        )

    assignments, bindings = _resolve_assignments(index, step, table, plan, policy)
    predicate = _resolve_predicate(index, step, table)

    if step.operation == "insert":
        _require_insert_completeness(index, step, table, assignments)

    sql, parameters = _render(
        step.operation, table, assignments, predicate, policy.dialect, paramstyle, bindings
    )

    return ValidatedWriteStep(
        index=index,
        sql=sql,
        parameters=parameters,
        bindings=bindings,
        operation=step.operation,
        table=table.name,
        schema_name=table.schema_name,
        expected_row_count=step.expected_row_count,
        description=step.description,
    )


def _resolve_assignments(
    index: int,
    step: WriteStep,
    table: WritableTable,
    plan: WritePlan,
    policy: WritePolicy,
) -> Tuple[List[Tuple[str, JsonScalar]], List[StepBinding]]:
    resolved: List[Tuple[str, JsonScalar]] = []
    bindings: List[StepBinding] = []

    for assignment in step.assignments:
        key = normalize_identifier(assignment.column)
        column = table.columns.get(key)
        if column is None:
            raise WriteRefusal(
                WriteCode.COLUMN_UNKNOWN,
                f"{table.name} has no column named {assignment.column}.",
                detail=f"{table.name}.{assignment.column}",
                step=index,
            )
        if not column.can_write:
            raise WriteRefusal(
                WriteCode.COLUMN_NOT_ALLOWED,
                f"You may not change {table.name}.{column.name}.",
                detail=f"{table.name}.{column.name}",
                step=index,
            )

        if assignment.reference is not None:
            _check_reference(index, assignment.reference, table, column.name, plan, policy)
            # The value does not exist yet. Reserve the slot with None and
            # record where the executor must fetch it from; the structural
            # validator on ValidatedWriteStep proves the two agree.
            resolved.append((key, None))
            bindings.append(
                StepBinding(
                    position=len(resolved),
                    from_step=assignment.reference.from_step,
                    column=normalize_identifier(assignment.reference.column),
                )
            )
        else:
            resolved.append((key, assignment.value))

    return resolved, bindings


def _check_reference(
    index: int,
    reference,
    table: WritableTable,
    child_column: str,
    plan: WritePlan,
    policy: WritePolicy,
) -> None:
    if not policy.supports_returning:
        raise WriteRefusal(
            WriteCode.RETURNING_UNSUPPORTED,
            f"This database cannot hand back a generated key, so a change that "
            "creates a row and then refers to it cannot be made in one step here.",
            step=index,
        )

    parent_step = plan.steps[reference.from_step]
    parent = policy.resolve_table(parent_step.table)
    if parent is None:
        raise WriteRefusal(
            WriteCode.TABLE_NOT_ALLOWED,
            f"You do not have write access to {parent_step.table}.",
            detail=parent_step.table,
            step=index,
        )

    parent_column = parent.column(reference.column)
    if parent_column is None:
        raise WriteRefusal(
            WriteCode.COLUMN_UNKNOWN,
            f"{parent.name} has no column named {reference.column}.",
            detail=f"{parent.name}.{reference.column}",
            step=index,
        )
    if not parent_column.is_primary_key:
        # Note this deliberately does not require `can_write` on the parent
        # column: a generated key is never assignable, and it is exactly the
        # column worth referencing.
        raise WriteRefusal(
            WriteCode.REFERENCE_NOT_KEY,
            f"{parent.name}.{parent_column.name} is not a key column, so it "
            "cannot identify the row just created.",
            detail=f"{parent.name}.{parent_column.name}",
            step=index,
        )

    if not policy.permits_reference(
        child_table=table.name,
        child_column=child_column,
        parent_table=parent.name,
        parent_column=parent_column.name,
    ):
        raise WriteRefusal(
            WriteCode.REFERENCE_NOT_RELATED,
            f"{table.name}.{child_column} is not declared as referring to "
            f"{parent.name}.{parent_column.name}.",
            detail=f"{table.name}.{child_column}",
            step=index,
        )


def _resolve_predicate(
    index: int, step: WriteStep, table: WritableTable
) -> List[Tuple[str, JsonScalar]]:
    if step.operation == "insert":
        return []

    if not step.predicate:
        # Unreachable through the model, which requires a predicate for update
        # and delete. Kept because this function is also the last line if that
        # validator is ever relaxed, and "every row" is not a failure mode worth
        # leaving to one guard.
        raise WriteRefusal(
            WriteCode.PREDICATE_REQUIRED,
            f"A {step.operation} must say which rows it means.",
            step=index,
        )

    resolved: List[Tuple[str, JsonScalar]] = []
    addressed = set()
    for item in step.predicate:
        key = normalize_identifier(item.column)
        column = table.columns.get(key)
        if column is None:
            raise WriteRefusal(
                WriteCode.COLUMN_UNKNOWN,
                f"{table.name} has no column named {item.column}.",
                detail=f"{table.name}.{item.column}",
                step=index,
            )
        if not column.is_primary_key:
            # The bound on blast radius. A non-key predicate can match any
            # number of rows, and the promised row count would then be a guess
            # rather than a fact the transaction can be held to.
            raise WriteRefusal(
                WriteCode.PREDICATE_NOT_KEY,
                f"{table.name}.{column.name} is not a key column; changes must "
                "be addressed by primary key.",
                detail=f"{table.name}.{column.name}",
                step=index,
            )
        resolved.append((key, item.value))
        addressed.add(key)

    missing = table.key_columns - addressed
    if missing:
        # A strict subset of a composite key matches every row sharing the
        # partial key, not the one row the approval card implies.
        names = ", ".join(sorted(table.columns[key].name for key in missing))
        raise WriteRefusal(
            WriteCode.PREDICATE_INCOMPLETE_KEY,
            f"{table.name} is keyed by more than one column; this change does "
            f"not say which {names} it means.",
            detail=table.name,
            step=index,
        )

    return resolved


def _require_insert_completeness(
    index: int,
    step: WriteStep,
    table: WritableTable,
    assignments: Sequence[Tuple[str, JsonScalar]],
) -> None:
    supplied = {key for key, _ in assignments}
    missing = table.required_insert_columns - supplied
    if missing:
        names = ", ".join(sorted(table.columns[key].name for key in missing))
        raise WriteRefusal(
            WriteCode.MISSING_REQUIRED_COLUMN,
            f"A new row in {table.name} must supply {names}.",
            detail=table.name,
            step=index,
        )


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _render(
    operation: str,
    table: WritableTable,
    assignments: Sequence[Tuple[str, JsonScalar]],
    predicate: Sequence[Tuple[str, JsonScalar]],
    dialect: str,
    paramstyle: str,
    bindings: Sequence[StepBinding],
) -> Tuple[str, List[JsonScalar]]:
    """Build the statement from expression nodes, never from string formatting.

    Parameters are numbered in the order the driver will bind them: assignments
    first, then predicate, which is the order they appear in the rendered SQL
    for every operation here -- and the order ``_resolve_assignments`` already
    assigned binding positions in.
    """
    parameters: List[JsonScalar] = []
    marker = PARAMSTYLES[paramstyle]

    def placeholder(value: JsonScalar) -> exp.Expression:
        parameters.append(value)
        # exp.Var rather than exp.Placeholder: Placeholder renders per dialect
        # (`%s` for postgres, `?` for mysql) which describes the *database's*
        # syntax, while what has to match here is the *driver's* paramstyle --
        # and pymysql wants `%s` where sqlglot's mysql dialect writes `?`. Var
        # carries the exact marker through unchanged in every dialect, so the
        # two concerns stay separate and no pass ever rewrites finished SQL.
        return exp.Var(this=marker(len(parameters)))

    target = _table_expression(table, dialect)

    if operation == "insert":
        columns = [
            exp.to_identifier(table.columns[key].name, quoted=True)
            for key, _ in assignments
        ]
        statement: exp.Expression = exp.Insert(
            this=exp.Schema(this=target, expressions=columns),
            expression=exp.Values(
                expressions=[
                    exp.Tuple(expressions=[placeholder(value) for _, value in assignments])
                ]
            ),
        )
    elif operation == "update":
        statement = exp.Update(
            this=target,
            expressions=[
                exp.EQ(
                    this=exp.column(table.columns[key].name, quoted=True),
                    expression=placeholder(value),
                )
                for key, value in assignments
            ],
            where=exp.Where(this=_conjunction(table, predicate, placeholder)),
        )
    else:
        statement = exp.Delete(
            this=target,
            where=exp.Where(this=_conjunction(table, predicate, placeholder)),
        )

    return statement.sql(dialect=dialect, comments=False), parameters


def _table_expression(table: WritableTable, dialect: str) -> exp.Table:
    """A quoted table reference, schema-qualified when the catalog knows one."""
    name = exp.to_identifier(_bare_name(table), quoted=True)
    if table.schema_name:
        return exp.Table(
            this=name, db=exp.to_identifier(table.schema_name, quoted=True)
        )
    return exp.Table(this=name)


def _bare_name(table: WritableTable) -> str:
    if table.schema_name and table.name.startswith(f"{table.schema_name}."):
        return table.name[len(table.schema_name) + 1 :]
    return table.name.rsplit(".", 1)[-1]


def _conjunction(
    table: WritableTable,
    predicate: Sequence[Tuple[str, JsonScalar]],
    placeholder,
) -> exp.Expression:
    """``a = ? AND b = ?`` over the predicate, in order."""
    condition: Optional[exp.Expression] = None
    for key, value in predicate:
        comparison = exp.EQ(
            this=exp.column(table.columns[key].name, quoted=True),
            expression=placeholder(value),
        )
        condition = comparison if condition is None else exp.And(
            this=condition, expression=comparison
        )
    if condition is None:  # pragma: no cover - guarded by _resolve_predicate
        raise WriteRefusal(
            WriteCode.PREDICATE_REQUIRED, "A change must say which rows it means."
        )
    return condition


def _apply_returning(
    steps: List[ValidatedWriteStep], policy: WritePolicy
) -> List[ValidatedWriteStep]:
    """Add a RETURNING clause to every step a later step takes a value from.

    Done after rendering rather than during it, because a step does not know it
    is referenced until the steps after it have been read.
    """
    wanted: Dict[int, List[str]] = {}
    for step in steps:
        for binding in step.bindings:
            names = wanted.setdefault(binding.from_step, [])
            if binding.column not in names:
                names.append(binding.column)
    if not wanted:
        return steps

    rendered: List[ValidatedWriteStep] = []
    for step in steps:
        columns = wanted.get(step.index)
        if not columns:
            rendered.append(step)
            continue
        # Appended as text rather than rebuilt as an expression: sqlglot's
        # Insert constructor has spelled RETURNING differently across versions,
        # and pinning a rendering detail to a library version is the more
        # fragile of the two options. The names come from the policy, not from
        # the model, and are quoted by the dialect's own identifier rules.
        returning = ", ".join(
            exp.to_identifier(name, quoted=True).sql(dialect=policy.dialect)
            for name in columns
        )
        rendered.append(
            step.model_copy(
                update={
                    "sql": f"{step.sql} RETURNING {returning}",
                    "returning_columns": list(columns),
                }
            )
        )
    return rendered
