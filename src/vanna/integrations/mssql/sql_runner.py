"""Microsoft SQL Server implementation of SqlRunner interface."""

from typing import Optional
import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy


class MSSQLRunner(BaseSqlRunner):
    """Microsoft SQL Server implementation of the SqlRunner interface."""

    dialect = "tsql"

    def __init__(self, odbc_conn_str: str, *,
        policy: Optional[ExecutionPolicy] = None,
        **kwargs):
        """Initialize with MSSQL connection parameters.

        Args:
            odbc_conn_str: The ODBC connection string for SQL Server
            **kwargs: Additional SQLAlchemy engine parameters
        """
        super().__init__(policy)

        try:
            import pyodbc

            self.pyodbc = pyodbc
        except ImportError as e:
            raise ImportError(
                "pyodbc package is required. Install with: pip install pyodbc"
            ) from e

        try:
            import sqlalchemy as sa
            from sqlalchemy.engine import URL
            from sqlalchemy import create_engine

            self.sa = sa
            self.URL = URL
            self.create_engine = create_engine
        except ImportError as e:
            raise ImportError(
                "sqlalchemy package is required. Install with: pip install sqlalchemy"
            ) from e

        # Create the connection URL
        connection_url = self.URL.create(
            "mssql+pyodbc", query={"odbc_connect": odbc_conn_str}
        )

        # Create the engine
        self.engine = self.create_engine(connection_url, **kwargs)

    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Execute SQL query against MSSQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If query execution fails
        """
        # Execute the SQL statement and return the result as a pandas DataFrame
        with self.engine.begin() as conn:
            df = pd.read_sql_query(self.sa.text(sql), conn)
            return df
