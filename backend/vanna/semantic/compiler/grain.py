"""Bucketing a timestamp by day, week, month, quarter, year.

An explicit table per dialect rather than trusting a transpiler, because the
differences here are semantic and not syntactic:

* SQLite has no ``DATE_TRUNC`` at all -- it needs ``strftime``, and the result
  is text, not a date.
* MySQL has no ``DATE_TRUNC`` either, and ``DATE_FORMAT`` cannot express
  quarters, so quarters are computed arithmetically.
* Weeks disagree about which day starts them. PostgreSQL and DuckDB use ISO
  (Monday); BigQuery and Snowflake default to Sunday.

That last one is the dangerous case: both answers are "correct", they are
simply different, and a report that silently changes its week boundary when the
warehouse changes is a bug nobody attributes to the semantic layer. Every
dialect below is pinned to **Monday**, and where the native default disagrees
the expression says so explicitly.
"""

from __future__ import annotations

from typing import Callable, Dict

from ...core.errors import ErrorCode, ErrorPhase, VannaError

#: Supported buckets, coarsest last.
GRANULARITIES = ("second", "minute", "hour", "day", "week", "month", "quarter", "year")


def _date_trunc(unit: str) -> Callable[[str], str]:
    return lambda column: f"DATE_TRUNC('{unit}', {column})"


#: Dialects whose DATE_TRUNC already starts weeks on Monday (ISO).
_ISO_WEEK_DATE_TRUNC = {
    unit: _date_trunc(unit) for unit in GRANULARITIES
}

#: BigQuery and Snowflake start the week on Sunday by default. Naming the
#: start day explicitly is what stops the boundary moving under a report.
_SUNDAY_DEFAULT_DATE_TRUNC = {
    **{unit: _date_trunc(unit) for unit in GRANULARITIES},
    "week": lambda column: f"DATE_TRUNC('week(monday)', {column})",
}

_BIGQUERY = {
    **{unit: (lambda u: lambda c: f"TIMESTAMP_TRUNC({c}, {u.upper()})")(unit)
       for unit in GRANULARITIES},
    "week": lambda column: f"TIMESTAMP_TRUNC({column}, WEEK(MONDAY))",
}

_SQLITE = {
    "second":  lambda c: f"strftime('%Y-%m-%d %H:%M:%S', {c})",
    "minute":  lambda c: f"strftime('%Y-%m-%d %H:%M:00', {c})",
    "hour":    lambda c: f"strftime('%Y-%m-%d %H:00:00', {c})",
    "day":     lambda c: f"date({c})",
    # `weekday 1` steps back to the most recent Monday; the -6 day adjustment
    # handles Sunday, which SQLite counts as day 0 of the *following* week.
    "week":    lambda c: f"date({c}, '-6 days', 'weekday 1')",
    "month":   lambda c: f"date({c}, 'start of month')",
    "quarter": lambda c: (
        f"date({c}, 'start of month', "
        f"'-' || ((CAST(strftime('%m', {c}) AS INTEGER) - 1) % 3) || ' months')"
    ),
    "year":    lambda c: f"date({c}, 'start of year')",
}

_MYSQL = {
    "second":  lambda c: f"DATE_FORMAT({c}, '%Y-%m-%d %H:%i:%s')",
    "minute":  lambda c: f"DATE_FORMAT({c}, '%Y-%m-%d %H:%i:00')",
    "hour":    lambda c: f"DATE_FORMAT({c}, '%Y-%m-%d %H:00:00')",
    "day":     lambda c: f"DATE({c})",
    # WEEKDAY() is 0 on Monday, so subtracting it always lands on Monday.
    "week":    lambda c: f"DATE_SUB(DATE({c}), INTERVAL WEEKDAY({c}) DAY)",
    "month":   lambda c: f"DATE_FORMAT({c}, '%Y-%m-01')",
    "quarter": lambda c: (
        f"MAKEDATE(YEAR({c}), 1) + INTERVAL (QUARTER({c}) - 1) QUARTER"
    ),
    "year":    lambda c: f"DATE_FORMAT({c}, '%Y-01-01')",
}

_MSSQL = {
    **{unit: (lambda u: lambda c: f"DATETRUNC({u}, {c})")(unit) for unit in GRANULARITIES},
    # DATETRUNC's week honours DATEFIRST, which is a session setting; deriving
    # the Monday arithmetically is immune to it.
    "week": lambda c: f"DATEADD(DAY, -((DATEPART(WEEKDAY, {c}) + 5) % 7), CAST({c} AS DATE))",
}

_BY_DIALECT: Dict[str, Dict[str, Callable[[str], str]]] = {
    "postgres": _ISO_WEEK_DATE_TRUNC,
    "postgresql": _ISO_WEEK_DATE_TRUNC,
    "duckdb": _ISO_WEEK_DATE_TRUNC,
    "redshift": _ISO_WEEK_DATE_TRUNC,
    "trino": _ISO_WEEK_DATE_TRUNC,
    "presto": _ISO_WEEK_DATE_TRUNC,
    "clickhouse": _ISO_WEEK_DATE_TRUNC,
    "snowflake": _SUNDAY_DEFAULT_DATE_TRUNC,
    "databricks": _ISO_WEEK_DATE_TRUNC,
    "spark": _ISO_WEEK_DATE_TRUNC,
    "bigquery": _BIGQUERY,
    "sqlite": _SQLITE,
    "mysql": _MYSQL,
    "tsql": _MSSQL,
    "mssql": _MSSQL,
}


def truncate(column_sql: str, granularity: str, dialect: str) -> str:
    """Bucket ``column_sql`` to ``granularity`` in ``dialect``.

    Raises rather than falling back to ``DATE_TRUNC`` on an unknown dialect:
    a guess that happens to parse produces silently wrong bucket boundaries,
    which is exactly the failure this module exists to prevent.
    """
    unit = (granularity or "").lower().strip()
    if unit not in GRANULARITIES:
        raise VannaError(
            ErrorCode.INVALID_REQUEST,
            f"unknown granularity {granularity!r}.",
            phase=ErrorPhase.SEMANTIC_COMPILE,
            hint="One of: " + ", ".join(GRANULARITIES),
        )

    table = _BY_DIALECT.get((dialect or "").lower())
    if table is None:
        raise VannaError(
            ErrorCode.NOT_IMPLEMENTED,
            f"time granularity is not implemented for dialect {dialect!r}.",
            phase=ErrorPhase.SEMANTIC_COMPILE,
            hint=(
                "Add it to vanna/semantic/compiler/grain.py. Falling back to a "
                "generic DATE_TRUNC would produce wrong week boundaries silently."
            ),
        )

    return table[unit](column_sql)


def supported_dialects() -> list:
    return sorted(_BY_DIALECT)
