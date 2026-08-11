"""SQLite implementation of SqlRunner interface."""

from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy


class SqliteRunner(BaseSqlRunner):
    """SQLite implementation of the SqlRunner interface.

    Inherits row limiting, timeouts, thread offloading, and truncation
    reporting from :class:`BaseSqlRunner`.

    SQLite has no ``statement_timeout``, so the time limit is enforced with a
    progress handler that aborts the query from inside the engine. That is
    stronger than the inherited client-side timeout alone: ``asyncio.wait_for``
    stops *waiting* for a runaway query but cannot stop it *running*, and a
    thread stuck in C-level SQLite code is not interruptible from Python.
    """

    dialect = "sqlite"

    def __init__(
        self,
        database_path: str,
        *,
        policy: Optional[ExecutionPolicy] = None,
        read_only: bool = True,
    ):
        """Initialize with a SQLite database path.

        Args:
            database_path: Path to the SQLite database file.
            policy: Execution limits (rows, timeout, result size).
            read_only: Open the database in read-only mode. Uses a URI
                connection so SQLite itself rejects writes.
        """
        super().__init__(policy=policy)
        self.database_path = database_path
        self.read_only = read_only

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # file: URI with mode=ro makes the engine reject writes outright,
            # independent of anything the policy layer did or did not catch.
            uri = f"file:{self.database_path}?mode=ro"
            return sqlite3.connect(uri, uri=True)
        return sqlite3.connect(self.database_path)

    def dry_run_sql(self, sql: str) -> None:
        """Validate via ``EXPLAIN``, which prepares the statement without running it.

        SQLite resolves table and column names at prepare time, so this catches
        the two errors that matter most -- unknown table, unknown column --
        without touching a single row.
        """
        conn = self._connect()
        try:
            conn.execute(f"EXPLAIN {sql}")
        finally:
            conn.close()

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Execute *sql*. Runs on a worker thread."""
        conn = self._connect()
        conn.row_factory = sqlite3.Row

        # Abort from inside the engine once the deadline passes. The handler
        # fires every N virtual-machine instructions; returning non-zero raises
        # OperationalError in the executing statement.
        import time

        deadline = time.monotonic() + timeout_seconds

        def _abort_if_expired() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_abort_if_expired, 10_000)

        try:
            cursor = conn.cursor()
            cursor.execute(sql)

            if cursor.description is None:
                return pd.DataFrame()

            rows = cursor.fetchall()
            columns = [d[0] for d in cursor.description]
            if not rows:
                return pd.DataFrame(columns=columns)
            return pd.DataFrame([dict(row) for row in rows])
        finally:
            conn.set_progress_handler(None, 0)
            conn.close()
