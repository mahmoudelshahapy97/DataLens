"""MySQL implementation of SqlRunner interface."""

from typing import Optional
import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy


class MySQLRunner(BaseSqlRunner):

    """MySQL implementation of the SqlRunner interface.

    Extends :class:`BaseSqlRunner`, so the row cap, timeout, thread offloading
    and truncation reporting come for free rather than being reimplemented.
    """

    dialect = "mysql"
    #: pymysql binds with `%s` -- note sqlglot's mysql dialect writes `?`,
    #: which is the database's syntax rather than the driver's.
    paramstyle = "format"

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        *,
        policy: Optional[ExecutionPolicy] = None,
        read_only: bool = True,
        **kwargs,
    ):
        """Initialize with MySQL connection parameters.

        Args:
            host: Database host address
            database: Database name
            user: Database user
            password: Database password
            port: Database port (default: 3306)
            policy: Row cap and timeout.
            read_only: Refuse to commit anything. Default, and the only safe
                one for a natural-language query interface. Note this is
                enforced by this class rather than by the server: unlike
                PostgreSQL, MySQL has no per-session read-only transaction
                mode to fall back on, so the SQL policy above is doing more of
                the work here.
            **kwargs: Additional PyMySQL connection parameters
        """
        super().__init__(policy)
        self.read_only = read_only

        try:
            import pymysql.cursors

            self.pymysql = pymysql
        except ImportError as e:
            raise ImportError(
                "PyMySQL package is required. Install with: pip install PyMySQL"
            ) from e

        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.port = port
        self.kwargs = kwargs

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Run the query on a worker thread.

        Also sets MAX_EXECUTION_TIME so an abandoned query stops consuming
        server resources instead of merely losing its client -- the wall-clock
        timeout in BaseSqlRunner cancels our wait, not MySQL's work.
        """
        conn = self.pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            cursorclass=self.pymysql.cursors.DictCursor,
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            **self.kwargs,
        )

        try:
            conn.ping(reconnect=True)
            cursor = conn.cursor()
            try:
                # Milliseconds, and SELECT-only in MySQL -- a failure here is
                # not worth losing the query over.
                try:
                    cursor.execute(
                        "SET SESSION MAX_EXECUTION_TIME=%s", (timeout_seconds * 1000,)
                    )
                except Exception:
                    pass

                cursor.execute(sql)

                if cursor.description is None:
                    # No result set: a statement whose effect is a row count.
                    #
                    # This branch used to be absent, and its absence was a real
                    # bug rather than an omission: pymysql opens a connection
                    # with autocommit off, `conn.close()` rolls back, and so a
                    # DML statement that reached this runner was silently
                    # discarded while the tool above reported it as executed.
                    # A write that reports success and does nothing is worse
                    # than one that fails.
                    affected = max(cursor.rowcount, 0)
                    if self.read_only:
                        conn.rollback()
                    else:
                        conn.commit()
                    frame = pd.DataFrame()
                    frame.attrs["rows_affected"] = affected
                    return frame

                results = cursor.fetchall()
                if not self.read_only:
                    conn.commit()
                return pd.DataFrame(
                    results,
                    columns=[desc[0] for desc in cursor.description],
                )
            finally:
                cursor.close()
        finally:
            conn.close()

    def dry_run_sql(self, sql: str) -> None:
        """Plan the query without running it."""
        conn = self.pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            **self.kwargs,
        )
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(f"EXPLAIN {sql}")
            finally:
                cursor.close()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def execute_write(self, validated, context):
        """Run every step of a validated write in one transaction, or none.

        MySQL has no ``RETURNING``, so a plan whose child row needs its
        parent's generated key is refused one layer earlier by the validator.
        What arrives here is therefore always a sequence of independent
        statements -- still one transaction, still one row-count promise each.
        """
        import asyncio

        from vanna.capabilities.sql_runner.write import WritesNotSupported

        if self.read_only:
            raise WritesNotSupported(
                "This connection is read-only. Writes are enabled per workspace, "
                "and this one has not enabled them."
            )
        return await asyncio.wait_for(
            asyncio.to_thread(self._execute_write_sync, validated),
            timeout=self.policy.timeout_seconds,
        )

    def _execute_write_sync(self, validated):
        import time

        from vanna.capabilities.sql_runner.write import (
            ConstraintViolated,
            UnexpectedRowCount,
            WriteResult,
            WriteStepResult,
            bind_parameters,
        )

        started = time.perf_counter()
        conn = self.pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            cursorclass=self.pymysql.cursors.DictCursor,
            connect_timeout=self.policy.timeout_seconds,
            read_timeout=self.policy.timeout_seconds,
            autocommit=False,
            **self.kwargs,
        )
        results = []
        try:
            cursor = conn.cursor()
            try:
                try:
                    cursor.execute(
                        "SET SESSION MAX_EXECUTION_TIME=%s",
                        (self.policy.timeout_seconds * 1000,),
                    )
                except Exception:
                    pass  # SELECT-only in MySQL; not worth losing the write over

                for step in validated.steps:
                    parameters = bind_parameters(step.parameters, step.bindings, results)
                    cursor.execute(step.sql, tuple(parameters))
                    affected = max(cursor.rowcount, 0)

                    if affected != step.expected_row_count:
                        # Before the commit, so nothing survives.
                        raise UnexpectedRowCount(
                            expected=step.expected_row_count,
                            actual=affected,
                            step=step.index,
                            steps=len(validated.steps),
                        )
                    results.append(
                        WriteStepResult(
                            index=step.index,
                            operation=step.operation,
                            rows_affected=affected,
                        )
                    )
                conn.commit()
            finally:
                cursor.close()

            return WriteResult(
                steps=results,
                committed=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except UnexpectedRowCount:
            _rollback(conn)
            raise
        except self.pymysql.err.IntegrityError as exc:
            _rollback(conn)
            raise ConstraintViolated(_constraint_kind(exc)) from exc
        except Exception:
            _rollback(conn)
            raise
        finally:
            conn.close()


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - defensive
        pass


def _constraint_kind(exc) -> str:
    """Map a MySQL error number to the closed vocabulary, never its message."""
    number = (exc.args[0] if exc.args else 0) or 0
    return {
        1062: "unique",
        1451: "foreign_key",
        1452: "foreign_key",
        1048: "not_null",
        3819: "check",
    }.get(number, "constraint")
