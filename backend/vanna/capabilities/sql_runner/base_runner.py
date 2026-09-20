"""A SqlRunner base class that handles the cross-cutting execution concerns.

Every database integration needs the same four things, and getting any of them
wrong is a production incident:

1. **A row cap**, so one query cannot pull a fact table into memory.
2. **A timeout**, so one slow query cannot occupy a worker indefinitely.
3. **Thread offloading**, because the DB-API drivers are synchronous. Calling
   a blocking driver inside ``async def`` stalls the event loop and every other
   concurrent request with it -- the failure is invisible under light load and
   catastrophic under real traffic.
4. **Honest truncation reporting**, so the caller knows it got a partial answer.

Implementing those once here means a new integration supplies only
``_execute_sync`` and inherits all of it::

    class MyRunner(BaseSqlRunner):
        dialect = "postgres"

        def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
            with self._pool.connection() as conn:
                return pd.read_sql(sql, conn)

Truncation detection uses an **N+1 probe**: request one row more than the cap.
Getting ``limit + 1`` rows back proves more data exists, without a second
``COUNT(*)`` query. Cheaper than counting and exactly as informative for the
one question that matters -- "is there more?".
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from typing import TYPE_CHECKING, Any, Optional

import pandas as pd

from .base import SqlRunner
from .models import RunSqlToolArgs
from .policy import ExecutionPolicy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

logger = logging.getLogger(__name__)


class QueryTimeoutError(Exception):
    """Raised when a query exceeds its execution timeout."""


class ResultTooLargeError(Exception):
    """Raised when a result exceeds ``max_result_bytes``."""


class BaseSqlRunner(SqlRunner):
    """SqlRunner with limits, timeouts, and async offloading built in.

    Subclasses implement :meth:`_execute_sync` and set :attr:`dialect`.
    """

    #: sqlglot dialect name. Used for limit injection and by the SQL policy
    #: validator. Subclasses should override; None falls back to generic SQL,
    #: which parses most queries but mis-handles dialect-specific syntax.
    dialect: Optional[str] = None

    #: How this runner's driver spells a bind parameter, for the write path.
    #: Distinct from :attr:`dialect`, which decides identifier quoting: psycopg2
    #: and pymysql both want ``%s`` while their databases quote identifiers
    #: differently, and sqlite3 wants ``?`` from the same SQL sqlglot renders
    #: for postgres. Conflating the two produces statements that look right and
    #: fail at bind time.
    paramstyle: str = "format"

    def __init__(self, policy: Optional[ExecutionPolicy] = None) -> None:
        self.policy = policy or ExecutionPolicy()

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def _execute_sync(self, sql: str, timeout_seconds: int) -> pd.DataFrame:
        """Execute *sql* and return a DataFrame. Runs on a worker thread.

        Blocking calls are correct here -- this method is deliberately
        synchronous and is dispatched off the event loop by :meth:`run_sql`.

        Implementations should apply a server-side timeout where the engine
        supports one (``SET statement_timeout`` on PostgreSQL, ``MAX_EXECUTION_TIME``
        on MySQL) so an abandoned query stops consuming warehouse resources
        rather than merely losing its client.
        """

    #: The cheapest statement that proves the connection works.
    #:
    #: ``SELECT 1`` is valid in every engine here except Oracle, which requires
    #: a FROM clause and answers ORA-00923 without one. Hardcoding it meant the
    #: connection probe rejected every Oracle database -- including ones that
    #: were perfectly reachable -- so no Oracle workspace could be registered
    #: through the console at all.
    health_check_sql: str = "SELECT 1"

    def dry_run_sql(self, sql: str) -> None:
        """Check *sql* without returning rows. Raise on a problem.

        Subclasses should override with the engine's native mechanism --
        ``EXPLAIN`` on PostgreSQL/MySQL/SQLite, dry-run mode on BigQuery. The
        default raises :class:`NotImplementedError`, and callers treat that as
        "validation unavailable" rather than "query invalid": a runner without
        a cheap check should not cause every query to be reported as broken.

        Catching a bad column name here costs nothing. Catching it after a
        four-minute warehouse scan costs four minutes and real money.
        """
        raise NotImplementedError

    async def execute_write(self, validated: Any, context: "ToolContext") -> Any:
        """Run a validated write as one transaction. Raises unless overridden.

        Refusing is the correct default. The alternative -- inheriting a
        best-effort implementation -- is how a runner ends up accepting DML,
        never committing it, and reporting success, which is exactly what the
        MySQL runner did before this contract existed. An engine either
        implements the transaction and the row-count assertion or it does not
        do writes.
        """
        from .write import WritesNotSupported

        raise WritesNotSupported(
            f"{type(self).__name__} cannot execute writes. Writes need a "
            "transaction and a row-count assertion this runner does not implement."
        )

    def explain_sql(self, sql: str) -> Optional[str]:
        """The engine's query plan as text, or None if it cannot produce one.

        Distinct from :meth:`dry_run_sql`, which runs the same ``EXPLAIN`` and
        keeps only whether it raised. The plan itself answers a question the
        error cannot: whether the query the model just wrote is about to do
        something catastrophic -- a nested loop over two unfiltered tables, a
        sequential scan of a fact table -- which is not an error and will
        otherwise be discovered by waiting for it.

        Subclasses override with the engine's own syntax. The default raises
        :class:`NotImplementedError`, and callers treat that as "no plan
        available" rather than as a failure.
        """
        raise NotImplementedError

    async def explain(self, sql: str, context: "ToolContext") -> Optional[str]:
        """Plan text, or None when the runner cannot produce one.

        Never raises. A plan is an extra, and a query whose plan could not be
        fetched is not thereby a bad query -- the caller has already validated
        it by other means.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.explain_sql, sql),
                timeout=self.policy.timeout_seconds,
            )
        except NotImplementedError:
            return None
        except Exception as e:
            logger.debug("Could not explain query: %s", e)
            return None

    async def dry_run(self, sql: str, context: "ToolContext") -> Optional[str]:
        """Validate *sql*, returning an error message or None if it is fine.

        Returns None both when the query is valid and when the runner cannot
        check it -- the caller cannot act differently on those two outcomes, and
        conflating them keeps every call site from having to special-case
        unsupported runners.
        """
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.dry_run_sql, sql),
                timeout=self.policy.timeout_seconds,
            )
            return None
        except NotImplementedError:
            return None
        except asyncio.TimeoutError:
            return "Validation timed out; the query plan could not be produced."
        except Exception as e:
            return str(e)

    def policy_for(self, context: "ToolContext") -> ExecutionPolicy:
        """Resolve the policy for this execution.

        Override to vary limits per tenant, per user group, or per plan tier --
        e.g. a larger row cap for internal analysts than for external users.
        """
        return self.policy

    # ------------------------------------------------------------------
    # SqlRunner interface
    # ------------------------------------------------------------------

    async def run_sql(
        self, args: RunSqlToolArgs, context: "ToolContext"
    ) -> pd.DataFrame:
        """Execute SQL under the resolved execution policy.

        The returned frame carries execution metadata in ``df.attrs``:

        ``truncated``
            True if more rows existed than were returned. Callers should
            surface this -- see ``RunSqlTool``.
        ``row_count``, ``execution_ms``, ``limit_applied``
            Observability detail.
        """
        policy = self.policy_for(context)
        max_rows = policy.effective_max_rows()

        # N+1 probe: fetch one extra row so truncation is detectable.
        sql = self._apply_limit(args.sql, max_rows + 1)

        started = time.perf_counter()
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, sql, policy.timeout_seconds),
                timeout=policy.timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            elapsed = time.perf_counter() - started
            logger.warning(
                "Query exceeded %ss timeout (elapsed %.1fs) tenant=%s",
                policy.timeout_seconds,
                elapsed,
                getattr(context, "tenant_id", "default"),
            )
            raise QueryTimeoutError(
                f"The query exceeded the {policy.timeout_seconds}s time limit "
                "and was cancelled. Narrow the filters or aggregate to reduce "
                "the amount of data scanned."
            ) from e

        execution_ms = (time.perf_counter() - started) * 1000.0

        if df is None:
            df = pd.DataFrame()

        truncated = len(df) > max_rows
        if truncated:
            df = df.head(max_rows)

        self._check_result_size(df, policy)

        # attrs survives .head() and most pandas operations; it is the natural
        # place for out-of-band metadata that must not become a data column.
        df.attrs["truncated"] = truncated
        df.attrs["row_count"] = len(df)
        df.attrs["execution_ms"] = execution_ms
        df.attrs["limit_applied"] = max_rows

        return df

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_limit(self, sql: str, limit: int) -> str:
        """Inject a row limit, preserving any tighter limit already present.

        Uses AST rewriting rather than string concatenation so the clause lands
        correctly for the dialect and cannot corrupt a query that already ends
        in ``LIMIT``/``OFFSET``, a set operation, or a trailing comment.

        Falls back to the original SQL when sqlglot is unavailable or the query
        will not parse: the query has already passed policy validation by this
        point, and refusing to run it here would turn a missing optional
        dependency into an outage. The timeout still bounds the damage.
        """
        try:
            from vanna.core.sql_policy.validator import apply_row_limit

            return apply_row_limit(sql, limit, self.dialect)
        except Exception as e:
            logger.debug("Could not apply row limit, executing as written: %s", e)
            return sql

    @staticmethod
    def _check_result_size(df: pd.DataFrame, policy: ExecutionPolicy) -> None:
        """Reject results that are within the row cap but still enormous.

        A row cap alone does not bound memory: 1,000 rows each holding a 1 MB
        blob is a 1 GB frame. Only checked for non-empty frames, and failures
        of the size estimate itself are ignored -- an unmeasurable frame should
        not fail an otherwise successful query.
        """
        if df.empty:
            return
        try:
            size = int(df.memory_usage(deep=True).sum())
        except Exception:
            return
        if size > policy.max_result_bytes:
            raise ResultTooLargeError(
                f"The result is approximately {size // (1024 * 1024)} MB, "
                f"over the {policy.max_result_bytes // (1024 * 1024)} MB limit. "
                "Select fewer columns or add filters."
            )
