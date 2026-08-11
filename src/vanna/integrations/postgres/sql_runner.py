"""PostgreSQL implementation of SqlRunner interface."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy

logger = logging.getLogger(__name__)


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
                "psycopg2 package is required. Install with: pip install 'vanna[postgres]'"
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
                if self.connection_string:
                    self._pool = self.psycopg2.pool.ThreadedConnectionPool(
                        self._pool_min_size,
                        self._pool_max_size,
                        self.connection_string,
                    )
                else:
                    self._pool = self.psycopg2.pool.ThreadedConnectionPool(
                        self._pool_min_size,
                        self._pool_max_size,
                        **(self.connection_params or {}),
                    )
            return self._pool

    def close(self) -> None:
        """Close all pooled connections."""
        with self._pool_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

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
        conn = pool.getconn()
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
                pool.putconn(conn, close=broken)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed returning connection to pool: %s", e)

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Execute *sql* on a pooled connection. Runs on a worker thread.

        The connection is always returned to the pool, and is discarded rather
        than reused if the transaction could not be cleaned up -- returning a
        connection in an unknown state poisons the next caller.
        """
        pool = self._get_pool()
        conn = pool.getconn()
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
                pool.putconn(conn, close=broken)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed returning connection to pool: %s", e)
