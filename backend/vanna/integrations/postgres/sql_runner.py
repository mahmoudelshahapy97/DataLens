"""PostgreSQL implementation of SqlRunner interface."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import pandas as pd

from vanna.capabilities.sql_runner import (
    BaseSqlRunner,
    ConnectionPool,
    ExecutionPolicy,
)

logger = logging.getLogger(__name__)


def _still_open(connection: Any) -> bool:
    """Whether psycopg2 still considers this connection usable.

    `connection.closed` is 0 for open and non-zero for closed, and it notices a
    server that went away without the round trip a `SELECT 1` would cost.
    """
    return getattr(connection, "closed", 1) == 0


class PostgresRunner(BaseSqlRunner):
    """PostgreSQL implementation of the SqlRunner interface.

    Inherits row limiting, timeouts, thread offloading, and truncation
    reporting from :class:`BaseSqlRunner`, and adds three PostgreSQL-specific
    protections:

    * **A connection pool.** Opening a fresh connection per query costs a TCP
      handshake plus authentication on every request and exhausts the server's
      ``max_connections`` under concurrency.
    * **A server-side ``statement_timeout``.** A client-side timeout abandons
      the client but leaves the backend running; this makes PostgreSQL itself
      cancel the query.
    * **A read-only transaction.** Belt and braces alongside the SQL policy
      layer -- even if a write somehow reaches the driver, the server refuses
      it. Disable via ``read_only=False`` for genuine ETL use.
    """

    dialect = "postgres"
    #: psycopg2 binds with `%s`, whatever the SQL dialect writes.
    paramstyle = "format"

    def __init__(
        self,
        connection_string: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = 5432,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        *,
        policy: Optional[ExecutionPolicy] = None,
        read_only: bool = True,
        pool_min_size: int = 1,
        pool_max_size: int = 5,
        **kwargs: Any,
    ):
        """Initialize with PostgreSQL connection parameters.

        You can either provide a connection_string OR individual parameters
        (host, database, etc.). If connection_string is provided, it takes
        precedence.

        Args:
            connection_string: PostgreSQL connection string
                (e.g. "postgresql://user:password@host:port/database")
            host: Database host address
            port: Database port (default: 5432)
            database: Database name
            user: Database user
            password: Database password
            policy: Execution limits (rows, timeout, result size)
            read_only: Wrap every statement in a READ ONLY transaction.
                Leave enabled for natural-language query interfaces.
            pool_min_size: Connections opened eagerly.
            pool_max_size: Ceiling on pooled connections. Multiply by the number
                of processes and tenants when sizing against the server's
                ``max_connections``.
            **kwargs: Additional psycopg2 connection parameters
                (sslmode, connect_timeout, etc.)
        """
        super().__init__(policy=policy)

        try:
            import psycopg2
            import psycopg2.extras
            import psycopg2.pool

            self.psycopg2 = psycopg2
        except Exception as e:
            raise ImportError(
                "psycopg2 package is required. Install with: pip install psycopg2-binary"
            ) from e

        if connection_string:
            self.connection_string: Optional[str] = connection_string
            self.connection_params: Optional[Dict[str, Any]] = None
        elif host and database and user:
            self.connection_string = None
            self.connection_params = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
                **kwargs,
            }
        else:
            raise ValueError(
                "Either provide connection_string OR (host, database, and user) parameters"
            )

        self.read_only = read_only
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._pool: Any = None
        self._pool_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection pool
    # ------------------------------------------------------------------

    def _get_pool(self) -> Any:
        """Lazily build the pool, guarded so concurrent threads share one.

        Built on first use rather than in ``__init__`` so constructing a runner
        never performs I/O -- important because runners are often created at
        import time, before the database is reachable.
        """
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    self._open_connection,
                    alive=_still_open,
                    max_size=self._pool_max_size,
                    name=f"postgres {self._describe()}",
                )
            return self._pool

    def _open_connection(self):
        """One new psycopg2 connection, from whichever form was configured."""
        if self.connection_string:
            return self.psycopg2.connect(self.connection_string)
        return self.psycopg2.connect(**(self.connection_params or {}))

    def _describe(self) -> str:
        """A name for log lines that is not a connection string."""
        params = self.connection_params or {}
        return str(params.get("dbname") or params.get("database") or "database")

    def close(self) -> None:
        """Close all pooled connections.

        Only safe when nothing is using this runner. A pool closed underneath an
        in-flight query cannot take its connection back -- the borrower gets
        "connection pool is closed" and the request fails. Callers that are merely
        *finished caching* a runner should drop their reference instead and let
        :meth:`__del__` do this once the last user has gone.
        """
        with self._pool_lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    def __del__(self) -> None:
        """Close the pool when the last reference goes.

        This is what makes dropping a reference a safe way to retire a runner: the
        connections are released exactly when nobody can still be holding one, with
        no timer to tune and no window to lose a request in.

        Guarded to the point of paranoia because finalisers run during interpreter
        shutdown, when the modules this needs may already be torn down, and an
        exception in `__del__` is unraisable noise on stderr.
        """
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter teardown
            pass

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def dry_run_sql(self, sql: str) -> None:
        """Validate via ``EXPLAIN`` inside a rolled-back transaction.

        Plain ``EXPLAIN`` (without ``ANALYZE``) plans the query but does not
        execute it, so this resolves every table, column, function, and type
        without reading data. The rollback is belt-and-braces on top of the
        read-only session.
        """
        pool = self._get_pool()
        conn = pool.acquire()
        broken = False
        try:
            conn.set_session(readonly=True, autocommit=False)
            with conn.cursor() as cursor:
                cursor.execute(f"EXPLAIN {sql}")
            conn.rollback()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            try:
                pool.release(conn, broken=broken)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed returning connection to pool: %s", e)

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Execute *sql* on a pooled connection. Runs on a worker thread.

        The connection is always returned to the pool, and is discarded rather
        than reused if the transaction could not be cleaned up -- returning a
        connection in an unknown state poisons the next caller.
        """
        pool = self._get_pool()
        conn = pool.acquire()
        broken = False
        try:
            conn.set_session(readonly=self.read_only, autocommit=False)
            with conn.cursor(
                cursor_factory=self.psycopg2.extras.RealDictCursor
            ) as cursor:
                # Server-side cancellation: without this, a client timeout
                # leaves the backend executing.
                if self.policy.apply_server_side_timeout:
                    cursor.execute(
                        "SET LOCAL statement_timeout = %s", (timeout_seconds * 1000,)
                    )

                cursor.execute(sql)

                if cursor.description is None:
                    # No result set: either a statement like SET, or -- when
                    # this runner was built writable -- a DML statement whose
                    # effect is a row count.
                    #
                    # The commit is conditional on `read_only` and nothing
                    # else. Committing unconditionally would make a read-only
                    # runner writable the moment a policy elsewhere let a DML
                    # statement through; rolling back unconditionally, which is
                    # what this did before, silently discards every write a
                    # deployment has deliberately enabled.
                    affected = cursor.rowcount
                    if self.read_only:
                        conn.rollback()
                    else:
                        conn.commit()

                    frame = pd.DataFrame()
                    frame.attrs["rows_affected"] = max(affected, 0)
                    return frame

                rows = cursor.fetchall()
                if self.read_only:
                    conn.rollback()  # nothing to commit
                else:
                    # A write with RETURNING has both a result set and an
                    # effect; discarding it here would return the rows and undo
                    # the change that produced them.
                    conn.commit()

                if not rows:
                    columns = [d[0] for d in cursor.description]
                    return pd.DataFrame(columns=columns)

                return pd.DataFrame([dict(row) for row in rows])
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            try:
                pool.release(conn, broken=broken)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed returning connection to pool: %s", e)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def execute_write(self, validated: Any, context: Any) -> Any:
        """Run every step of a validated write in one transaction, or none.

        The row-count assertion is the control everything else rests on, so it
        is raised *inside* the transaction block: psycopg2 then rolls back on
        the way out, including steps that already succeeded. An UPDATE that
        promised one row and matched four hundred is not a partial success to
        report, it is a misunderstanding to undo.
        """
        import asyncio

        from ...capabilities.sql_runner.write import WritesNotSupported

        if self.read_only:
            # The connection-level guard is the last of the three layers between
            # a question and a write, and it is the one that cannot be reasoned
            # around. If it is on, the deployment did not intend writes, whatever
            # the policy above it says.
            raise WritesNotSupported(
                "This connection is read-only. Writes are enabled per workspace, "
                "and this one has not enabled them."
            )

        return await asyncio.wait_for(
            asyncio.to_thread(self._execute_write_sync, validated),
            timeout=self.policy.timeout_seconds,
        )

    def _execute_write_sync(self, validated: Any) -> Any:
        import time

        from ...capabilities.sql_runner.write import (
            ConstraintViolated,
            UnexpectedRowCount,
            WriteResult,
            WriteStepResult,
            bind_parameters,
        )

        pool = self._get_pool()
        conn = pool.acquire()
        broken = False
        started = time.perf_counter()
        results: list = []
        try:
            conn.set_session(readonly=False, autocommit=False)
            with conn.cursor(
                cursor_factory=self.psycopg2.extras.RealDictCursor
            ) as cursor:
                if self.policy.apply_server_side_timeout:
                    cursor.execute(
                        "SET LOCAL statement_timeout = %s",
                        (self.policy.timeout_seconds * 1000,),
                    )
                    # A write waits on locks a read never takes. Without this a
                    # statement blocked behind someone else's uncommitted
                    # transaction holds a worker until the statement timeout,
                    # having done nothing at all.
                    cursor.execute("SET LOCAL lock_timeout = %s", (5000,))

                for step in validated.steps:
                    parameters = bind_parameters(step.parameters, step.bindings, results)
                    cursor.execute(step.sql, tuple(parameters))

                    returned = {}
                    if step.returning_columns:
                        # RETURNING makes the row count the length of the result
                        # set: psycopg2's rowcount is right here, but fetching is
                        # what gives later steps the generated key.
                        rows = cursor.fetchall()
                        affected = len(rows)
                        if rows:
                            returned = {
                                name: dict(rows[0])[name]
                                for name in step.returning_columns
                                if name in dict(rows[0])
                            }
                    else:
                        affected = max(cursor.rowcount, 0)

                    if affected != step.expected_row_count:
                        # Inside the transaction on purpose. Raising here rolls
                        # back every earlier step too.
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
                            returned=returned,
                        )
                    )

                conn.commit()

            return WriteResult(
                steps=results,
                committed=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except UnexpectedRowCount:
            self._rollback(conn)
            raise
        except self.psycopg2.IntegrityError as exc:
            self._rollback(conn)
            raise ConstraintViolated(_constraint_kind(exc)) from exc
        except Exception:
            try:
                conn.rollback()
            except Exception:
                broken = True
            raise
        finally:
            try:
                pool.release(conn, broken=broken)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed returning connection to pool: %s", e)

    @staticmethod
    def _rollback(conn: Any) -> None:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - defensive
            logger.warning("Rollback failed after a refused write")


def _constraint_kind(exc: Any) -> str:
    """Map a driver error to the closed vocabulary, never its message.

    PostgreSQL's SQLSTATE classes are stable across versions; the message text
    is not, and often names the schema and the offending value -- neither of
    which belongs in something shown to a user.
    """
    code = getattr(getattr(exc, "pgcode", None), "strip", lambda: "")() or ""
    return {
        "23505": "unique",
        "23503": "foreign_key",
        "23514": "check",
        "23502": "not_null",
    }.get(code, "constraint")
