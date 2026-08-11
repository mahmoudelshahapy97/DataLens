"""DuckDB implementation of SqlRunner interface."""

from typing import Optional

import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy


class DuckDBRunner(BaseSqlRunner):
    """DuckDB implementation of the SqlRunner interface.

    Extends :class:`BaseSqlRunner`, so it inherits the row cap, the timeout,
    thread offloading, and truncation reporting rather than reimplementing any
    of them.
    """

    dialect = "duckdb"

    def __init__(
        self,
        database_path: str = ":memory:",
        init_sql: Optional[str] = None,
        *,
        policy: Optional[ExecutionPolicy] = None,
        read_only: bool = False,
        **kwargs,
    ):
        """Initialize with DuckDB connection parameters.

        Args:
            database_path: Path to the DuckDB database file.
                          Use ":memory:" for in-memory database (default).
                          Use "md:" or "motherduck:" for MotherDuck database.
            init_sql: Optional SQL to run when connecting to the database
            policy: Row cap and timeout. Defaults are conservative.
            read_only: Open the database read-only. Enforced by DuckDB itself,
                which is a stronger guarantee than the SQL policy's AST check --
                use both.
            **kwargs: Additional duckdb connection parameters
        """
        super().__init__(policy)

        try:
            import duckdb

            self.duckdb = duckdb
        except ImportError as e:
            raise ImportError(
                "duckdb package is required. Install with: pip install 'vanna[duckdb]'"
            ) from e

        self.database_path = database_path
        self.init_sql = init_sql
        self.read_only = read_only
        self.kwargs = kwargs
        self._conn = None

    def _get_connection(self):
        """Get or create DuckDB connection."""
        if self._conn is None:
            kwargs = dict(self.kwargs)
            # An in-memory database has nothing to protect and cannot be opened
            # read-only, so the flag is only meaningful for a real file.
            if self.read_only and self.database_path not in (":memory:", ""):
                kwargs.setdefault("read_only", True)
            self._conn = self.duckdb.connect(self.database_path, **kwargs)
            if self.init_sql:
                self._conn.execute(self.init_sql)
        return self._conn

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Run the query on a worker thread.

        DuckDB has no statement timeout, so the wall-clock limit imposed by
        ``BaseSqlRunner`` is the only one. It abandons the client, not the
        query -- acceptable here because DuckDB is in-process and dies with it.
        """
        return self._get_connection().execute(sql).fetch_df()

    def dry_run_sql(self, sql: str) -> None:
        """Plan the query without running it."""
        self._get_connection().execute(f"EXPLAIN {sql}")
