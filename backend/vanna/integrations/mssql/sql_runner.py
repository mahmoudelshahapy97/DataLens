"""Microsoft SQL Server implementation of SqlRunner interface."""

from typing import Optional
import pandas as pd

from vanna.capabilities.sql_runner import BaseSqlRunner, ExecutionPolicy


class MSSQLRunner(BaseSqlRunner):
    """Microsoft SQL Server implementation of the SqlRunner interface."""

    dialect = "tsql"
    #: pyodbc binds positionally with `?`.
    paramstyle = "qmark"

    def __init__(self, odbc_conn_str: str, *,
        policy: Optional[ExecutionPolicy] = None,
        read_only: bool = True,
        **kwargs):
        """Initialize with MSSQL connection parameters.

        Args:
            odbc_conn_str: The ODBC connection string for SQL Server
            policy: Row cap and timeout.
            read_only: Refuse to commit anything. Default, and the only safe
                one for a natural-language query interface.
            **kwargs: Additional SQLAlchemy engine parameters
        """
        super().__init__(policy)
        self.read_only = read_only

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
        # `engine.begin()` commits when the block exits, which for a read-only
        # runner is the wrong default: it would commit anything that reached
        # the driver. `engine.connect()` does not, so a statement that slips
        # past the policy layer is rolled back on close rather than persisted.
        if self.read_only:
            with self.engine.connect() as conn:
                return pd.read_sql_query(self.sa.text(sql), conn)

        with self.engine.begin() as conn:
            return pd.read_sql_query(self.sa.text(sql), conn)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def execute_write(self, validated, context):
        """Run every step of a validated write in one transaction, or none.

        SQL Server has no ``RETURNING`` (its ``OUTPUT`` clause is spelled
        differently and placed differently), so a plan whose child row needs
        its parent's generated key is refused one layer earlier by the
        validator.
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
        results = []
        try:
            # One `begin()` around every step: the commit happens when the
            # block exits normally, and any exception -- including the
            # row-count assertion below -- rolls the whole thing back.
            with self.engine.begin() as conn:
                raw = conn.connection
                cursor = raw.cursor()
                try:
                    for step in validated.steps:
                        parameters = bind_parameters(
                            step.parameters, step.bindings, results
                        )
                        cursor.execute(step.sql, tuple(parameters))
                        affected = max(cursor.rowcount, 0)
                        if affected != step.expected_row_count:
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
                finally:
                    cursor.close()
        except UnexpectedRowCount:
            raise
        except self.pyodbc.IntegrityError as exc:
            raise ConstraintViolated(_constraint_kind(exc)) from exc

        return WriteResult(
            steps=results,
            committed=True,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _constraint_kind(exc) -> str:
    """Map a SQLSTATE to the closed vocabulary, never the driver's message."""
    state = str(exc.args[0]) if exc.args else ""
    return {
        "23000": "constraint",
        "23505": "unique",
        "23503": "foreign_key",
    }.get(state, "constraint")
