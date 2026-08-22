"""Controlled writes: typed plans, built statements, and a human in the loop.

The model proposes a :class:`WritePlan` -- a table, some columns, some values,
and which rows it means. :func:`build_write_policy` says what this caller may
change. :func:`validate_write_plan` checks the one against the other and
**builds** the statement, so there is never model-authored SQL to parse.

See :mod:`vanna.core.write.models` for why that shape, rather than filtering,
is the security argument.
"""

from .errors import REPAIRABLE_CODES, WriteCode, WriteRefusal
from .models import (
    MAX_TRANSACTION_STEPS,
    ColumnAssignment,
    JsonScalar,
    KeyPredicate,
    StepReference,
    WritePlan,
    WriteStep,
)
from .policy import (
    RETURNING_DIALECTS,
    WritableColumn,
    WritableReference,
    WritableTable,
    WritePolicy,
    build_write_policy,
    describe_write_policy,
)
from .validator import (
    PARAMSTYLES,
    StepBinding,
    ValidatedWrite,
    ValidatedWriteStep,
    plan_hash,
    validate_write_plan,
)

__all__ = [
    "MAX_TRANSACTION_STEPS",
    "PARAMSTYLES",
    "REPAIRABLE_CODES",
    "RETURNING_DIALECTS",
    "ColumnAssignment",
    "JsonScalar",
    "KeyPredicate",
    "StepBinding",
    "StepReference",
    "ValidatedWrite",
    "ValidatedWriteStep",
    "WritableColumn",
    "WritableReference",
    "WritableTable",
    "WriteCode",
    "WritePlan",
    "WritePolicy",
    "WriteRefusal",
    "WriteStep",
    "build_write_policy",
    "describe_write_policy",
    "plan_hash",
    "validate_write_plan",
]
