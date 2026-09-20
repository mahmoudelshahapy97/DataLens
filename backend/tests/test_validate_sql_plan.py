"""`validate_sql` now returns the query plan, not just a verdict.

`dry_run_sql` already ran `EXPLAIN` and kept only whether it raised. The plan
answers the question the error cannot: whether a query that is perfectly valid
is about to read far more than the model thinks. An accidental cross join is
the case -- it is not a syntax error, it runs, and on a real warehouse it is
discovered by waiting several minutes for it.

Driven against real SQLite so the plan is a genuine one. `EXPLAIN QUERY PLAN`
is SQLite's own wording; the assertions here deliberately check the tool's
*interpretation* of a plan rather than a particular engine's phrasing, except
where the phrasing is what is being tested.
"""

from __future__ import annotations

import sqlite3

import pytest

from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.sqlite.sql_runner import SqliteRunner
from vanna.tools.validate_sql import (
    ValidateSqlArgs,
    ValidateSqlTool,
    _looks_expensive,
)


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id="acme"),
        conversation_id="c1",
        request_id="r1",
        tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "shop.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL);
        INSERT INTO customers (name) VALUES ('acme'), ('globex');
        INSERT INTO orders (customer_id, total) VALUES (1, 10.0), (2, 20.0);
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def tool(database):
    return ValidateSqlTool(SqliteRunner(database, read_only=True))


class TestQueryPlanReporting:
    async def test_valid_query_now_carries_a_plan(self, tool):
        result = await tool.execute(
            _context(), ValidateSqlArgs(sql="SELECT * FROM orders")
        )

        assert result.metadata["valid"] is True
        assert "Query plan:" in result.result_for_llm

    async def test_cross_join_is_called_out(self, tool):
        """Valid, runs, and reads every pair of rows. Not an error."""
        result = await tool.execute(
            _context(),
            ValidateSqlArgs(sql="SELECT * FROM orders, customers"),
        )

        assert result.metadata["valid"] is True
        assert "ON clause" in result.result_for_llm

    async def test_ordinary_join_is_not_called_out(self, tool):
        result = await tool.execute(
            _context(),
            ValidateSqlArgs(
                sql=(
                    "SELECT o.total FROM orders o "
                    "JOIN customers c ON c.id = o.customer_id "
                    "WHERE c.id = 1"
                )
            ),
        )

        assert result.metadata["valid"] is True
        assert "ON clause" not in result.result_for_llm

    async def test_an_invalid_query_still_reports_the_error_not_a_plan(self, tool):
        result = await tool.execute(
            _context(), ValidateSqlArgs(sql="SELECT nope FROM orders")
        )

        assert result.metadata["valid"] is False
        assert "Query plan:" not in result.result_for_llm

    async def test_a_runner_that_cannot_explain_still_validates(self, database):
        """The plan is an extra; losing it must not change the verdict."""

        class _NoExplain(SqliteRunner):
            def explain_sql(self, sql):
                raise NotImplementedError

        result = await ValidateSqlTool(_NoExplain(database, read_only=True)).execute(
            _context(), ValidateSqlArgs(sql="SELECT * FROM orders")
        )

        assert result.metadata["valid"] is True
        assert "Query plan:" not in result.result_for_llm

    async def test_an_explain_that_raises_does_not_fail_validation(self, database):
        class _BrokenExplain(SqliteRunner):
            def explain_sql(self, sql):
                raise RuntimeError("planner exploded")

        result = await ValidateSqlTool(
            _BrokenExplain(database, read_only=True)
        ).execute(_context(), ValidateSqlArgs(sql="SELECT * FROM orders"))

        assert result.metadata["valid"] is True


class TestExpensivePlanHeuristic:
    @pytest.mark.parametrize(
        "plan",
        [
            "Nested Loop  (cost=0.00..1000.00 rows=1000000)",
            "SCAN orders\nSCAN customers",
            "Seq Scan on orders\nSeq Scan on customers",
            "cartesian product",
        ],
    )
    def test_flags_plans_that_read_more_than_expected(self, plan):
        assert _looks_expensive(plan)

    @pytest.mark.parametrize(
        "plan",
        [
            "SCAN orders",  # one scan of a small table is ordinary
            "SEARCH orders USING INDEX idx_customer (customer_id=?)",
            "Index Scan using orders_pkey on orders",
        ],
    )
    def test_leaves_ordinary_plans_alone(self, plan):
        assert not _looks_expensive(plan)
