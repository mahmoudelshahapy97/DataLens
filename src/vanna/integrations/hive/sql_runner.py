"""Hive implementation of SqlRunner interface."""

from typing import Optional
import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy


class HiveRunner(BaseSqlRunner):
    """Hive implementation of the SqlRunner interface."""

    dialect = "hive"

    def __init__(
        self,
        host: str,
        database: str = "default",
        user: Optional[str] = None,
        password: Optional[str] = None,
        port: int = 10000,
        auth: str = "CUSTOM",
        *,
        policy: Optional[ExecutionPolicy] = None,
        **kwargs,
    ):
        """Initialize with Hive connection parameters.

        Args:
            host: The host of the Hive database
            database: The name of the database to connect to (default: 'default')
            user: The username to use for authentication
            password: The password to use for authentication
            port: The port to use for the connection (default: 10000)
            auth: The authentication method to use (default: 'CUSTOM')
            **kwargs: Additional pyhive connection parameters
        """
        super().__init__(policy)

        try:
            from pyhive import hive

            self.hive = hive
        except ImportError as e:
            raise ImportError(
                "pyhive package is required. Install with: pip install pyhive"
            ) from e

        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.port = port
        self.auth = auth
        self.kwargs = kwargs

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Execute SQL query against Hive database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            hive.Error: If query execution fails
        """
        # Connect to the database
        conn = self.hive.Connection(
            host=self.host,
            username=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            auth=self.auth,
            **self.kwargs,
        )

        try:
            cursor = conn.cursor()
            cursor.execute(sql)
            results = cursor.fetchall()

            # Create a pandas dataframe from the results
            df = pd.DataFrame(results, columns=[desc[0] for desc in cursor.description])

            cursor.close()
            return df

        finally:
            conn.close()
