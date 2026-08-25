"""The few SQL expressions that differ per engine and cannot be transpiled.

Deliberately small, and smaller than it first looks like it should be.

sqlglot is already a dependency and already knows how to rewrite most of what
varies -- ``LIMIT`` becomes ``TOP`` for T-SQL, ``::numeric`` becomes
``CAST(... AS NUMERIC)``, identifier quoting follows the target. Reimplementing
that here would be a worse copy of a library the project already trusts for the
SQL policy's parsing.

What sqlglot does *not* carry is date truncation into SQLite and Oracle. Asked to
rewrite ``DATE_TRUNC('month', c)`` it emits::

    sqlite  STRFTIME('%Y-%m', TIMESTAMP_TRUNC(c, MONTH))   <- not a SQLite function
    oracle  TO_CHAR(TIMESTAMP_TRUNC(c, MONTH), 'YYYY-MM')  <- not an Oracle function

Both produce SQL that parses and then fails at the database, which is the worst
kind of wrong: it looks like a working query until somebody runs it. So this
module owns exactly the expressions that have to be written per engine, and
everything else goes through ``sqlglot.transpile``.

The alternative -- ``if dialect == "postgres": ... elif ...`` wherever SQL is
built -- was rejected because the branches then live in whichever file needed one
first, and the fifth engine is added by grepping for the fourth.
"""

from __future__ import annotations

from typing import Dict, Type

#: Engine names as the runners report them via ``runner.dialect``.
POSTGRES = "postgres"
MYSQL = "mysql"
TSQL = "tsql"
SQLITE = "sqlite"
ORACLE = "oracle"


class Dialect:
    """How one engine spells the handful of things sqlglot cannot rewrite.

    Postgres is the base rather than an abstract class: it is what the rest of
    this codebase generates natively, so a new engine that forgets to override
    something produces Postgres SQL and fails loudly at the database, rather than
    raising ``NotImplementedError`` somewhere far from the cause.
    """

    name = POSTGRES

    def month(self, column: str) -> str:
        """A ``YYYY-MM`` label for the month *column* falls in.

        A label, not a date: it is the x axis of a trend chart, so it has to sort
        lexically and read plainly.
        """
        return f"TO_CHAR(DATE_TRUNC('month', {column}), 'YYYY-MM')"

    def money(self, expression: str) -> str:
        """*expression* summed and rounded to two decimals.

        Engines disagree about whether ``ROUND`` accepts a precision on a float,
        and about what type ``SUM`` of a money column even is.
        """
        return f"ROUND(SUM({expression})::numeric, 2)"

    def limit(self, sql: str, rows: int) -> str:
        """Cap the rows. Overridden only where ``LIMIT`` is not the spelling."""
        return f"{sql} LIMIT {rows}"


class PostgresDialect(Dialect):
    name = POSTGRES


class MySQLDialect(Dialect):
    name = MYSQL

    def month(self, column: str) -> str:
        return f"DATE_FORMAT({column}, '%Y-%m')"

    def money(self, expression: str) -> str:
        return f"ROUND(SUM({expression}), 2)"


class TSQLDialect(Dialect):
    name = TSQL

    def month(self, column: str) -> str:
        # FORMAT() is elegant and notoriously slow on SQL Server; CONVERT with
        # style 23 then a substring is the idiom people actually ship.
        return f"CONVERT(char(7), {column}, 126)"

    def money(self, expression: str) -> str:
        return f"ROUND(SUM(CAST({expression} AS decimal(18,2))), 2)"

    def limit(self, sql: str, rows: int) -> str:
        # T-SQL has no LIMIT. TOP goes after SELECT, so this is a rewrite rather
        # than a suffix -- and it must not fire twice on a query that already has
        # one.
        stripped = sql.lstrip()
        if stripped[:11].upper().startswith("SELECT TOP"):
            return sql
        if stripped[:6].upper() == "SELECT":
            return f"SELECT TOP {rows}" + stripped[6:]
        return sql


class SQLiteDialect(Dialect):
    name = SQLITE

    def month(self, column: str) -> str:
        return f"STRFTIME('%Y-%m', {column})"

    def money(self, expression: str) -> str:
        # No DECIMAL: SQLite's numeric affinity is a suggestion, and ROUND
        # returns a float whatever it is given.
        return f"ROUND(SUM({expression}), 2)"


class OracleDialect(Dialect):
    name = ORACLE

    def month(self, column: str) -> str:
        return f"TO_CHAR(TRUNC({column}, 'MM'), 'YYYY-MM')"

    def money(self, expression: str) -> str:
        return f"ROUND(SUM({expression}), 2)"

    def limit(self, sql: str, rows: int) -> str:
        # FETCH FIRST needs 12c or later; ROWNUM is the older spelling and works
        # everywhere, but changes the semantics under ORDER BY. 12c is a
        # reasonable floor for a deployment being set up today.
        return f"{sql} FETCH FIRST {rows} ROWS ONLY"


_BY_NAME: Dict[str, Type[Dialect]] = {
    POSTGRES: PostgresDialect,
    MYSQL: MySQLDialect,
    TSQL: TSQLDialect,
    "mssql": TSQLDialect,
    "sqlserver": TSQLDialect,
    SQLITE: SQLiteDialect,
    ORACLE: OracleDialect,
}


def dialect_for(name: str) -> Dialect:
    """The dialect for *name*, defaulting to Postgres.

    Defaulting rather than raising: an unknown engine is far more likely to be a
    new runner whose SQL is Postgres-shaped than a reason to refuse to build a
    dashboard. It fails at the database if wrong, with the database's own message,
    which says more than anything this function could.
    """
    return _BY_NAME.get((name or "").strip().lower(), PostgresDialect)()
