"""SQL safety policy: AST-based validation of generated queries.

An LLM writing SQL from a user's free text is untrusted input by construction,
and prompt-level mitigation ("only write SELECT queries") is guidance, not a
control. This package is the control.

Quick start -- wrap the tool registry and every SQL-bearing tool is covered::

    from vanna.core.sql_policy import SqlPolicy, SqlPolicyToolRegistry

    registry = SqlPolicyToolRegistry(
        policy=SqlPolicy.read_only(),
        dialect="postgres",
    )
    registry.register_local_tool(RunSqlTool(sql_runner=runner), [])

Standalone validation::

    from vanna.core.sql_policy import SqlPolicyValidator

    violations = SqlPolicyValidator().validate(sql, dialect="postgres")
"""

from .data_readers import (
    DATA_READER_FUNCTIONS,
    GENERATOR_FUNCTIONS,
    ROW_EXPANSION_FUNCTIONS,
)
from .models import (
    PolicyViolation,
    SqlPolicy,
    SqlPolicyError,
    ViolationCode,
)
from .registry import DEFAULT_SQL_FIELDS, SqlPolicyToolRegistry
from .validator import (
    SqlParseUnavailable,
    SqlPolicyValidator,
    apply_row_limit,
    has_row_limit,
    resolve_table_name,
)

__all__ = [
    "SqlPolicy",
    "SqlPolicyError",
    "PolicyViolation",
    "ViolationCode",
    "SqlPolicyValidator",
    "SqlParseUnavailable",
    "SqlPolicyToolRegistry",
    "DEFAULT_SQL_FIELDS",
    "apply_row_limit",
    "has_row_limit",
    "resolve_table_name",
    "DATA_READER_FUNCTIONS",
    "GENERATOR_FUNCTIONS",
    "ROW_EXPANSION_FUNCTIONS",
]
