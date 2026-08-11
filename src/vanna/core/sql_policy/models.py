"""Policy and violation models for SQL safety validation."""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, List, Optional, Set

from pydantic import BaseModel, Field

from .data_readers import DATA_READER_FUNCTIONS


class ViolationCode(str, Enum):
    """Why a query was rejected.

    Codes are stable identifiers -- log them, alert on them, and branch on them.
    Do not branch on message text.
    """

    UNPARSEABLE = "unparseable"
    """The dialect parser could not read the statement. Rejected fail-closed:
    a query we cannot analyse is a query we cannot vouch for."""

    STATEMENT_NOT_ALLOWED = "statement_not_allowed"
    """Root node is a statement type the policy forbids (e.g. DELETE under a
    read-only policy). This is what makes read-only actually read-only."""

    MULTIPLE_STATEMENTS = "multiple_statements"
    """More than one statement in a single string -- the classic stacked-query
    injection shape (``SELECT 1; DROP TABLE users``)."""

    DATA_READER_BLOCKED = "data_reader_blocked"
    """A file/URL/remote-database reader appeared anywhere in the query."""

    FUNCTION_DENIED = "function_denied"
    """A function on the operator's denylist was called."""

    TABLE_NOT_IN_CATALOG = "table_not_in_catalog"
    """Strict mode: the query referenced a table absent from the catalog."""

    SOURCE_FUNCTION_NOT_ALLOWED = "source_function_not_allowed"
    """A table-valued function was used as a query source without an opt-in."""

    TOO_MANY_JOINS = "too_many_joins"
    """Join count exceeded the configured ceiling -- a cheap guard against
    runaway cartesian products."""


class PolicyViolation(BaseModel):
    """A single reason a query was rejected.

    ``message`` is safe to show a user and to write to logs: it names the
    offending function or table but **never** echoes the offending expression.
    Echoing it would leak the very thing the check exists to block -- file
    paths, URLs, and connection strings all appear as function arguments.
    """

    code: ViolationCode
    message: str
    detail: Optional[str] = Field(
        default=None,
        description=(
            "Name of the offending table or function. Never a full expression, "
            "never an argument value."
        ),
    )

    def __str__(self) -> str:
        return self.message


class SqlPolicyError(Exception):
    """Raised when SQL fails policy validation."""

    def __init__(self, violations: List[PolicyViolation]) -> None:
        self.violations = violations
        super().__init__("; ".join(v.message for v in violations))

    def to_vanna_error(self):
        """Convert to the framework-wide error envelope.

        Every violation collapses to ``POLICY_VIOLATION`` except the two that
        are not really about policy at all: an unparseable statement is
        malformed, and a table missing from the catalog is a missing object.
        The distinction matters to the agent, because only those two are worth
        rewriting -- a blocked function will be blocked again.
        """
        from ..errors import ErrorCode, ErrorPhase, VannaError

        codes = {v.code for v in self.violations}
        if ViolationCode.UNPARSEABLE in codes:
            code = ErrorCode.INVALID_SQL
        elif codes == {ViolationCode.TABLE_NOT_IN_CATALOG}:
            code = ErrorCode.OBJECT_NOT_FOUND
        else:
            code = ErrorCode.POLICY_VIOLATION

        return VannaError(
            code,
            str(self),
            phase=ErrorPhase.SQL_POLICY_CHECK,
            # Codes, not messages: an alert should group on these.
            metadata={"violations": sorted(c.value for c in codes)},
            cause=self,
        )


class SqlPolicy(BaseModel):
    """Rules a generated query must satisfy before it is allowed to execute.

    The default is deliberately strict -- read-only, no data readers, no
    generators, bounded joins -- because the caller of last resort is an LLM
    writing SQL from a user's free text, and the safe default should require no
    configuration. Loosening is an explicit act.

    Example::

        # Analytics: read-only, plus a row cap injected by the runner
        SqlPolicy()

        # Same, restricted to tables the catalog actually knows about
        SqlPolicy(require_catalog_tables=True)

        # An ETL tool that genuinely needs to write
        SqlPolicy(mode="read_write",
                  allowed_statements={"SELECT", "WITH", "INSERT", "UPDATE"})
    """

    mode: str = Field(
        default="read_only",
        description="'read_only' or 'read_write'. read_only permits only "
        "statements in allowed_statements and is what you want for a "
        "natural-language query interface.",
    )

    allowed_statements: Set[str] = Field(
        default_factory=lambda: {"SELECT", "WITH", "UNION", "EXCEPT", "INTERSECT"},
        description="Uppercase statement kinds permitted at the AST root.",
    )

    denied_functions: FrozenSet[str] = Field(
        default_factory=frozenset,
        description="Additional function names to reject, case-insensitive. "
        "Use for deployment-specific concerns (e.g. 'version', "
        "'current_user') without editing the built-in reader list.",
    )

    block_data_readers: bool = Field(
        default=True,
        description="Reject file/URL/remote-database readers in every AST "
        "position. Leave enabled unless the database credential is already "
        "sandboxed from the filesystem and network.",
    )

    allowed_source_functions: FrozenSet[str] = Field(
        default_factory=frozenset,
        description="Row generators (generate_series, sequence, range) opted "
        "back in as query sources. Data readers can never be enabled here.",
    )

    require_catalog_tables: bool = Field(
        default=False,
        description="Strict mode: every table reference must resolve to the "
        "schema catalog. Fail-closed and highly effective, but only turn it on "
        "once catalog coverage is complete -- otherwise valid queries break.",
    )

    max_joins: Optional[int] = Field(
        default=10,
        description="Reject queries with more joins than this. Guards against "
        "runaway cartesian products. None disables the check.",
    )

    require_limit: bool = Field(
        default=True,
        description="Whether the executing runner should inject a row limit "
        "when the query has none. Enforced at execution time, not parse time.",
    )

    default_limit: int = Field(
        default=1000,
        description="Row limit injected when require_limit is set.",
    )

    @property
    def effective_denied_functions(self) -> FrozenSet[str]:
        """Operator denylist plus the built-in reader list when enabled."""
        if self.block_data_readers:
            return frozenset(self.denied_functions) | DATA_READER_FUNCTIONS
        return frozenset(self.denied_functions)

    @classmethod
    def read_only(cls) -> "SqlPolicy":
        """The recommended default for natural-language query interfaces."""
        return cls()

    @classmethod
    def permissive(cls) -> "SqlPolicy":
        """Parse-and-warn only. Every check disabled.

        For trusted internal tooling where the caller is not an LLM. Do not use
        this on a path where query text originates from a model or an end user.
        """
        return cls(
            mode="read_write",
            allowed_statements=set(),  # empty == no statement restriction
            block_data_readers=False,
            require_catalog_tables=False,
            max_joins=None,
            require_limit=False,
        )
