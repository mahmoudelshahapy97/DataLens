"""
SQL runner capability.

This module provides abstractions for SQL execution used by tools.
"""

from .base import SqlRunner
from .base_runner import (
    BaseSqlRunner,
    QueryTimeoutError,
    ResultTooLargeError,
)
from .models import RunSqlToolArgs
from .pool import ConnectionPool, looks_dead
from .policy import ExecutionPolicy, ExecutionResult
from .write import (
    ConstraintViolated,
    UnexpectedRowCount,
    WriteResult,
    WriteRunner,
    WriteStepResult,
    WritesNotSupported,
    bind_parameters,
)

__all__ = [
    "SqlRunner",
    "ConnectionPool",
    "looks_dead",
    "BaseSqlRunner",
    "RunSqlToolArgs",
    "ExecutionPolicy",
    "ExecutionResult",
    "QueryTimeoutError",
    "ResultTooLargeError",
    "ConstraintViolated",
    "UnexpectedRowCount",
    "WriteResult",
    "WriteRunner",
    "WriteStepResult",
    "WritesNotSupported",
    "bind_parameters",
]
