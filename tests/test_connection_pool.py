"""The shared connection pool, and the line it draws around retrying.

Only ``PostgresRunner`` pooled; MySQL, Oracle and SQLite opened a connection per
query, and SQL Server pooled with SQLAlchemy's defaults -- five plus ten overflow,
which is fifteen per runner, multiplied by cached runtimes and again by workers.

The half of this worth testing hardest is not the reuse. It is what the retry
refuses to do:

    INSERT -> server executes it -> connection dies before the ack
           -> client sees a failure -> retry -> executed twice

So acquisition retries and nothing else. A dead pooled connection is discovered
before anything is sent, which makes replacing it free of consequence; a statement
that failed may have arrived, which makes re-running it a decision no library
should take on the caller's behalf.
"""

from __future__ import annotations

import threading
import time

import pytest

from vanna.capabilities.sql_runner import ConnectionPool, looks_dead


class FakeConnection:
    def __init__(self, number: int) -> None:
        self.number = number
        self.closed = False
        self.alive = True

    def close(self) -> None:
        self.closed = True


def a_pool(**kwargs) -> tuple:
    """A pool over counted fake connections, and the list it opened."""
    opened = []

    def connect():
        connection = FakeConnection(len(opened))
        opened.append(connection)
        return connection

    pool = ConnectionPool(connect, **kwargs)
    return pool, opened


class TestReuse:
    def test_a_returned_connection_is_handed_out_again(self):
        pool, opened = a_pool(max_size=2)

        first = pool.acquire()
        pool.release(first)
        second = pool.acquire()

        assert second is first
        assert len(opened) == 1, "opened a second connection with one idle"

    def test_it_opens_up_to_the_ceiling_and_no_further(self):
        pool, opened = a_pool(max_size=2)

        a = pool.acquire()
        b = pool.acquire()

        # A third caller waits rather than opening a third connection -- the
        # ceiling is the point, since it is multiplied by runtimes and workers.
        with pytest.raises(TimeoutError) as caught:
            pool.acquire(timeout=0.05)
        assert "connection became free" in str(caught.value)
        assert len(opened) == 2

        pool.release(a)
        pool.release(b)

    def test_a_waiter_is_woken_by_a_release(self):
        pool, _ = a_pool(max_size=1)
        held = pool.acquire()
        got = []

        def waiter():
            got.append(pool.acquire(timeout=5))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        pool.release(held)
        thread.join(timeout=5)

        assert got and got[0] is held

    def test_the_size_reports_what_exists(self):
        pool, _ = a_pool(max_size=3)
        a, b = pool.acquire(), pool.acquire()

        assert pool.size == 2 and pool.in_use == 2

        pool.release(a)
        assert pool.size == 2 and pool.in_use == 1
        pool.release(b, broken=True)
        assert pool.size == 1


class TestDeadConnections:
    def test_a_connection_that_fails_its_liveness_check_is_replaced(self):
        pool, opened = a_pool(max_size=2, alive=lambda c: c.alive)

        first = pool.acquire()
        pool.release(first)
        first.alive = False  # the database restarted while it sat idle

        second = pool.acquire()

        assert second is not first
        assert first.closed, "the dead connection was not closed"
        assert len(opened) == 2

    def test_a_broken_connection_is_not_pooled(self):
        pool, opened = a_pool(max_size=2)

        connection = pool.acquire()
        pool.release(connection, broken=True)

        assert connection.closed
        assert pool.acquire() is not connection

    def test_a_failure_to_connect_does_not_leak_a_lease(self):
        """Otherwise a database that is down permanently exhausts the pool with
        connections that were never made, and recovery needs a restart."""
        attempts = {"n": 0}

        def connect():
            attempts["n"] += 1
            raise OSError("connection refused")

        pool = ConnectionPool(connect, max_size=1)

        for _ in range(3):
            with pytest.raises(OSError):
                pool.acquire(timeout=0.05)

        assert attempts["n"] == 3, "the lease was never returned after a failure"


class TestClose:
    def test_closing_closes_idle_connections(self):
        pool, opened = a_pool(max_size=2)
        pool.release(pool.acquire())

        pool.close()

        assert all(c.closed for c in opened)

    def test_closing_does_not_close_one_in_use(self):
        """The use-after-close this codebase has already been bitten by: a pool
        closed under a live query leaves its borrower holding a dead handle."""
        pool, _ = a_pool(max_size=2)
        borrowed = pool.acquire()

        pool.close()

        assert not borrowed.closed
        # It is discarded when the borrower is done with it, not before.
        pool.release(borrowed)
        assert borrowed.closed

    def test_a_closed_pool_refuses_new_leases(self):
        pool, _ = a_pool(max_size=1)
        pool.close()

        with pytest.raises(RuntimeError, match="closed"):
            pool.acquire(timeout=0.05)


class TestWhatCountsAsDead:
    """The classifier decides whether a connection is replaced. Widen it
    carelessly and "reconnect" becomes "run the statement again"."""

    @pytest.mark.parametrize("message", [
        "server closed the connection unexpectedly",
        "MySQL server has gone away",
        "Lost connection to MySQL server during query",
        "connection already closed",
        "[Microsoft][ODBC Driver 18] Communication link failure",
        "Broken pipe",
        "connection reset by peer",
    ])
    def test_transport_failures_are_dead(self, message):
        assert looks_dead(Exception(message))

    @pytest.mark.parametrize("message", [
        "syntax error at or near SELCT",
        "permission denied for table salaries",
        "canceling statement due to statement timeout",
        "duplicate key value violates unique constraint",
        "relation orders does not exist",
        "deadlock detected",
        "division by zero",
    ])
    def test_everything_else_is_not(self, message):
        """Especially the timeout and the constraint violation: a timed-out
        statement may still be running, and a duplicate key means the first
        attempt landed."""
        assert not looks_dead(Exception(message))


class TestTheRunnersUseIt:
    def test_mysql_takes_the_configured_ceiling(self):
        """`build_runner` documented "multiply by the worker count" and passed
        nothing, so every runner took its driver's default."""
        pytest.importorskip("pymysql")
        from vanna.core.datasource.runners import build_runner

        runner = build_runner(
            "mysql://u:p@nowhere:3306/db", pool_max=3, pool_min=0
        )
        assert runner._pool._max_size == 3

    def test_sqlite_shares_one_connection(self, tmp_path):
        """A file, not a server: the fix is to stop reopening it, not to pool."""
        import sqlite3

        from vanna.integrations.sqlite import SqliteRunner

        path = tmp_path / "demo.db"
        sqlite3.connect(path).close()

        runner = SqliteRunner(str(path), read_only=True)
        assert runner._connect() is runner._connect()

        runner.close()
        assert runner._connection is None


class TestDialects:
    """The expressions sqlglot cannot carry, and why they are hand-written.

    Asked to rewrite `DATE_TRUNC('month', c)` for SQLite or Oracle, sqlglot emits
    `TIMESTAMP_TRUNC(c, MONTH)` -- a function neither engine has. That parses and
    then fails at the database, which is the worst kind of wrong: it looks like a
    working query right up until somebody runs it.
    """

    def test_each_engine_gets_its_own_month_expression(self):
        from vanna.core.datasource.dialects import dialect_for

        spellings = {
            name: dialect_for(name).month("c")
            for name in ("postgres", "mysql", "tsql", "sqlite", "oracle")
        }
        # All different, and none of them the invention sqlglot produces.
        assert len(set(spellings.values())) == 5
        assert not any("TIMESTAMP_TRUNC" in s for s in spellings.values())

        assert "DATE_TRUNC" in spellings["postgres"]
        assert "DATE_FORMAT" in spellings["mysql"]
        assert "STRFTIME" in spellings["sqlite"]
        assert "TRUNC" in spellings["oracle"]

    def test_row_capping_follows_the_engine(self):
        """T-SQL puts it after SELECT, Oracle after ORDER BY, the rest at the end."""
        from vanna.core.datasource.dialects import dialect_for

        query = "SELECT a FROM t ORDER BY a"
        assert dialect_for("postgres").limit(query, 10).endswith("LIMIT 10")
        assert dialect_for("tsql").limit(query, 10).startswith("SELECT TOP 10")
        assert "FETCH FIRST 10 ROWS ONLY" in dialect_for("oracle").limit(query, 10)

    def test_capping_twice_does_not_double_up(self):
        """The generator caps once, but T-SQL's rewrite is not idempotent by
        construction the way a suffix is, so it says so itself."""
        from vanna.core.datasource.dialects import dialect_for

        once = dialect_for("tsql").limit("SELECT a FROM t", 10)
        assert dialect_for("tsql").limit(once, 10) == once

    def test_an_unknown_engine_falls_back_rather_than_raising(self):
        """A new runner whose SQL is Postgres-shaped is far likelier than a reason
        to refuse to build a dashboard. If it is wrong, the database says so."""
        from vanna.core.datasource.dialects import PostgresDialect, dialect_for

        assert isinstance(dialect_for("clickhouse"), PostgresDialect)
        assert isinstance(dialect_for(""), PostgresDialect)

    def test_mssql_and_sqlserver_are_the_same_dialect(self):
        from vanna.core.datasource.dialects import TSQLDialect, dialect_for

        for alias in ("tsql", "mssql", "sqlserver", "TSQL"):
            assert isinstance(dialect_for(alias), TSQLDialect), alias


class TestTheScannerReadsAnyEnginesRows:
    """`information_schema` is standard; the case of its column names is not.

    PostgreSQL returns `table_name`, MySQL returns `TABLE_NAME`. Reading only the
    lowercase spelling made every MySQL scan produce a table called `None.None`,
    which failed pydantic validation with a message about a string -- an error a
    long way from its cause.
    """

    def test_it_reads_either_case(self):
        from vanna.capabilities.schema_catalog.scanner import _cell

        assert _cell({"table_name": "film"}, "table_name") == "film"
        assert _cell({"TABLE_NAME": "film"}, "table_name") == "film"
        assert _cell({"Table_Name": "film"}, "table_name") == "film"

    def test_a_missing_column_is_none_not_an_exception(self):
        """The scanner asks for columns that only some engines return."""
        from vanna.capabilities.schema_catalog.scanner import _cell

        assert _cell({"table_name": "film"}, "is_identity") is None
        assert _cell(None, "anything") is None

    def test_an_exact_match_wins(self):
        from vanna.capabilities.schema_catalog.scanner import _cell

        assert _cell({"n": 1, "N": 2}, "n") == 1
