"""SQL-aware error recovery.

Most SQL failures are mechanically diagnosable, and several are mechanically
fixable given the right hint. Handing the model a raw driver message and hoping
the tool loop stumbles into a fix wastes turns and often fails, because driver
messages say *what* broke without saying *what exists instead*.

This strategy does three things the generic retry cannot:

**Classifies before retrying.** Retrying a permission denial is pure waste, and
repeated denials look like probing to anyone reading the audit log. Only the
error classes a rewrite can plausibly fix are retried.

**Builds a targeted hint.** On `unknown_column` it does not merely echo the
error -- it looks the table up in the catalog and attaches the real column list.
That converts "column x does not exist" into "column x does not exist; the
columns are a, b, c", which is usually enough on the first retry.

**Sanitises.** Driver errors routinely embed the full query text and sometimes
data values, and those messages end up in logs and in the model's context. They
are cleaned before either sees them.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional, Set

from ..errors import ErrorCode
from .base import ErrorRecoveryStrategy
from .models import RecoveryAction, RecoveryActionType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...capabilities.schema_catalog import SchemaCatalog
    from ..tool.models import ToolContext

logger = logging.getLogger(__name__)


class SqlErrorKind:
    """Coarse classification of a SQL failure.

    A class of string constants rather than an enum because these values are
    persisted on ``SqlGeneration.error_kind``; promoting them to an enum now
    would not change what is already written in stored rows.
    """

    SYNTAX = "syntax"
    UNKNOWN_TABLE = "unknown_table"
    UNKNOWN_COLUMN = "unknown_column"
    AMBIGUOUS_COLUMN = "ambiguous_column"
    TYPE_MISMATCH = "type_mismatch"
    AGGREGATE_MISUSE = "aggregate_misuse"
    DIVISION_BY_ZERO = "division_by_zero"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    POLICY = "policy"
    CONNECTION = "connection"
    UNKNOWN = "unknown"


#: Errors a rewrite cannot fix. Retrying these burns an LLM call and a database
#: round trip to arrive at the same place -- and in the permission case, looks
#: like an access probe.
NON_RETRYABLE: Set[str] = {
    SqlErrorKind.PERMISSION,
    SqlErrorKind.TIMEOUT,
    SqlErrorKind.CONNECTION,
}

#: Every ``SqlErrorKind`` mapped onto the framework-wide :class:`ErrorCode`, so
#: a driver failure and a policy rejection can be reported in one vocabulary.
#: Kept as data, and covered by an exhaustiveness test, so a new kind without a
#: mapping fails the suite rather than silently becoming INTERNAL_ERROR.
_KIND_TO_CODE = {
    SqlErrorKind.SYNTAX: ErrorCode.INVALID_SQL,
    SqlErrorKind.UNKNOWN_TABLE: ErrorCode.OBJECT_NOT_FOUND,
    SqlErrorKind.UNKNOWN_COLUMN: ErrorCode.OBJECT_NOT_FOUND,
    SqlErrorKind.AMBIGUOUS_COLUMN: ErrorCode.INVALID_SQL,
    SqlErrorKind.TYPE_MISMATCH: ErrorCode.INVALID_SQL,
    # An aggregate outside GROUP BY is a malformed statement, not a type
    # problem -- the database rejects it at plan time.
    SqlErrorKind.AGGREGATE_MISUSE: ErrorCode.INVALID_SQL,
    SqlErrorKind.DIVISION_BY_ZERO: ErrorCode.INVALID_SQL,
    SqlErrorKind.PERMISSION: ErrorCode.PERMISSION_DENIED,
    SqlErrorKind.TIMEOUT: ErrorCode.QUERY_TIMEOUT,
    SqlErrorKind.POLICY: ErrorCode.POLICY_VIOLATION,
    SqlErrorKind.CONNECTION: ErrorCode.DATABASE_UNAVAILABLE,
    SqlErrorKind.UNKNOWN: ErrorCode.INTERNAL_ERROR,
}


def error_code_for_sql_kind(kind: str) -> ErrorCode:
    """Map a :class:`SqlErrorKind` value onto an :class:`ErrorCode`."""
    return _KIND_TO_CODE.get(kind, ErrorCode.INTERNAL_ERROR)


#: Ordered patterns. First match wins, so the specific precede the generic:
#: "column ... does not exist" must be tested before a bare "does not exist".
_PATTERNS = [
    (SqlErrorKind.PERMISSION, r"permission denied|access denied|not authorized"
                              r"|insufficient privile|must be owner"),
    (SqlErrorKind.TIMEOUT, r"timeout|timed out|canceling statement due to"
                           r"|query exceeded|execution time"),
    (SqlErrorKind.CONNECTION, r"connection (refused|reset|closed|failed)"
                              r"|could not connect|server closed|no such host"),
    (SqlErrorKind.POLICY, r"safety policy|not permitted by the current policy"
                          r"|blocked by the sql"),
    (SqlErrorKind.AMBIGUOUS_COLUMN, r"ambiguous column|column reference .* is ambiguous"),
    (SqlErrorKind.UNKNOWN_COLUMN, r"column .* (does not exist|not found|unknown)"
                                  r"|unknown column|no such column|invalid column name"
                                  r"|has no column named"),
    (SqlErrorKind.UNKNOWN_TABLE, r"(table|relation) .* (does not exist|not found)"
                                 r"|no such table|invalid object name|unknown table"),
    (SqlErrorKind.AGGREGATE_MISUSE, r"must appear in the group by|not in group by"
                                    r"|aggregate function.*not allowed|nested aggregate"),
    (SqlErrorKind.TYPE_MISMATCH, r"cannot be (cast|coerced|converted)|type mismatch"
                                 r"|invalid input syntax for|operator does not exist"
                                 r"|incompatible types"),
    (SqlErrorKind.DIVISION_BY_ZERO, r"division by zero|divide by zero"),
    (SqlErrorKind.SYNTAX, r"syntax error|parse error|unexpected token|near \""),
]


def classify_sql_error(message: str) -> str:
    """Classify a database error message into a :class:`SqlErrorKind`."""
    text = (message or "").lower()
    for kind, pattern in _PATTERNS:
        if re.search(pattern, text):
            return kind
    return SqlErrorKind.UNKNOWN


def sanitize_error(message: str, *, max_length: int = 500) -> str:
    """Strip query text and data values out of a driver error message.

    Drivers commonly append the whole failing statement (often after ``LINE 1:``
    or inside brackets), and it may carry filter values that are themselves
    sensitive. The error is going into logs and into the model's context, so it
    is trimmed to the part that identifies the problem.
    """
    text = (message or "").strip()

    # PostgreSQL appends `LINE n: <the query>` plus a caret pointer.
    text = re.split(r"\nLINE \d+:", text)[0]
    # SQLAlchemy appends `[SQL: ...]` and `[parameters: ...]`.
    text = re.split(r"\[SQL:", text)[0]
    text = re.split(r"\[parameters:", text)[0]
    # Collapse whitespace so multi-line errors stay readable in a log line.
    text = " ".join(text.split())

    if len(text) > max_length:
        text = text[:max_length] + "..."
    return text


def extract_identifier(message: str) -> Optional[str]:
    """Pull the offending table or column name out of an error message.

    Used to look the real thing up in the catalog. Returns None when nothing
    quoted is present, in which case the hint falls back to generic guidance.
    """
    for pattern in (r'"([^"]+)"', r"'([^']+)'", r"`([^`]+)`"):
        match = re.search(pattern, message or "")
        if match:
            return match.group(1)
    return None


class SqlRepairStrategy(ErrorRecoveryStrategy):
    """Retries recoverable SQL failures with a targeted hint.

    Args:
        catalog: Schema catalog, used to build hints naming real columns and
            tables. Strongly recommended -- without it, hints degrade to
            generic advice and the retry is much less likely to succeed.
        max_attempts: Retries per tool call. Two is usually right: the first
            retry with a good hint fixes most of what is fixable, and a third
            attempt on the same error rarely differs from the second.
        retry_delay_ms: Delay before retrying. Zero for logic errors, since
            waiting changes nothing.
    """

    def __init__(
        self,
        *,
        catalog: Optional["SchemaCatalog"] = None,
        max_attempts: int = 2,
        retry_delay_ms: int = 0,
    ) -> None:
        self.catalog = catalog
        self.max_attempts = max_attempts
        self.retry_delay_ms = retry_delay_ms

    async def handle_tool_error(
        self, error: Exception, context: "ToolContext", attempt: int = 1
    ) -> RecoveryAction:
        raw = str(error)
        kind = classify_sql_error(raw)
        clean = sanitize_error(raw)

        logger.info(
            "SQL error kind=%s attempt=%d tenant=%s",
            kind,
            attempt,
            getattr(context, "tenant_id", "default"),
        )

        if kind in NON_RETRYABLE:
            return RecoveryAction(
                action=RecoveryActionType.FAIL,
                message=self._terminal_message(kind, clean),
            )

        if attempt > self.max_attempts:
            return RecoveryAction(
                action=RecoveryActionType.FAIL,
                message=(
                    f"The query still fails after {self.max_attempts} "
                    f"correction attempts: {clean}"
                ),
            )

        hint = await self._build_hint(kind, raw, context)
        return RecoveryAction(
            action=RecoveryActionType.RETRY,
            retry_delay_ms=self.retry_delay_ms or None,
            message=f"{clean}\n\n{hint}",
        )

    # ------------------------------------------------------------------
    # Hints
    # ------------------------------------------------------------------

    @staticmethod
    def _terminal_message(kind: str, clean: str) -> str:
        """A message the user can act on, for errors no rewrite will fix."""
        if kind == SqlErrorKind.PERMISSION:
            return (
                "You do not have permission to read that data. Ask the user to "
                "request access rather than trying a different query."
            )
        if kind == SqlErrorKind.TIMEOUT:
            return (
                "The query took too long and was cancelled. Narrow the time "
                "range, add filters, or aggregate in SQL to scan less data."
            )
        if kind == SqlErrorKind.CONNECTION:
            return (
                "The database is unreachable right now. This is not a problem "
                "with the query -- tell the user to try again shortly."
            )
        return clean

    async def _build_hint(
        self, kind: str, raw: str, context: "ToolContext"
    ) -> str:
        """Turn an error into actionable instruction, using the catalog."""
        identifier = extract_identifier(raw)

        if kind == SqlErrorKind.UNKNOWN_COLUMN:
            columns = await self._columns_near(identifier, context)
            if columns:
                return (
                    f"The column {identifier!r} does not exist. The available "
                    f"columns are: {', '.join(columns)}. Use one of these "
                    "exactly as written."
                )
            return (
                f"The column {identifier!r} does not exist. Call "
                "get_table_schema for the table to see its real columns "
                "before retrying."
            )

        if kind == SqlErrorKind.UNKNOWN_TABLE:
            tables = await self._tables_named(identifier, context)
            if tables:
                return (
                    f"The table {identifier!r} does not exist. Similar tables: "
                    f"{', '.join(tables)}."
                )
            return (
                f"The table {identifier!r} does not exist. Call search_tables "
                "to find the correct name. Do not guess."
            )

        if kind == SqlErrorKind.AMBIGUOUS_COLUMN:
            return (
                f"The column {identifier!r} exists in more than one joined "
                "table. Qualify it with its table alias."
            )

        if kind == SqlErrorKind.AGGREGATE_MISUSE:
            return (
                "Every non-aggregated column in the SELECT must appear in "
                "GROUP BY. Either add the missing columns to GROUP BY or wrap "
                "them in an aggregate."
            )

        if kind == SqlErrorKind.TYPE_MISMATCH:
            return (
                "The types on either side of a comparison or operator do not "
                "match. Cast explicitly (e.g. CAST(col AS VARCHAR)), and check "
                "the column types with get_table_schema if unsure."
            )

        if kind == SqlErrorKind.DIVISION_BY_ZERO:
            return (
                "A denominator was zero. Guard it with NULLIF(denominator, 0), "
                "which yields NULL instead of failing."
            )

        if kind == SqlErrorKind.SYNTAX:
            return (
                "Fix the syntax error at the position named above. Change only "
                "that part -- rewriting the whole query tends to introduce new "
                "problems."
            )

        if kind == SqlErrorKind.POLICY:
            return (
                "The query was blocked by the safety policy. Write a plain "
                "read-only SELECT over the available tables."
            )

        return "Correct the specific problem named above and try once more."

    async def _columns_near(
        self, table_hint: Optional[str], context: "ToolContext"
    ) -> list:
        """Real column names, when the catalog can supply them.

        The identifier in the error may be either the column or the table
        depending on the driver, so both are tried before giving up.
        """
        if self.catalog is None or not table_hint:
            return []
        try:
            table = await self.catalog.get_table(context, table_hint)
            if table:
                return [c.name for c in table.columns]
            # The quoted name was probably the column; find a table owning a
            # similarly-named one.
            for candidate in await self.catalog.get_tables(context):
                if candidate.get_column(table_hint):
                    return [c.name for c in candidate.columns]
        except Exception as e:
            logger.debug("Could not build a column hint: %s", e)
        return []

    async def _tables_named(
        self, name: Optional[str], context: "ToolContext"
    ) -> list:
        if self.catalog is None or not name:
            return []
        try:
            matches = await self.catalog.search_tables(context, name, limit=5)
            return [t.qualified_name for t in matches]
        except Exception as e:
            logger.debug("Could not build a table hint: %s", e)
        return []
