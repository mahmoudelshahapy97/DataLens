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

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        *,
        policy: Optional[ExecutionPolicy] = None,
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
            **kwargs: Additional PyMySQL connection parameters
        """
        super().__init__(policy)

        try:
            import pymysql.cursors

            self.pymysql = pymysql
        except ImportError as e:
            raise ImportError(
                "PyMySQL package is required. Install with: pip install 'vanna[mysql]'"
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
                results = cursor.fetchall()
                return pd.DataFrame(
                    results,
                    columns=[desc[0] for desc in cursor.description]
                    if cursor.description
                    else [],
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
