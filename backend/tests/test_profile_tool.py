"""`profile_column` -- what a column actually contains.

Backed by a real SQLite database rather than a stubbed runner, the way
`test_write_flow.py` does it. The point of this tool is the SQL it generates,
so a stub that returns a canned DataFrame would assert only that the Python
around the query is wired up, and would pass just as happily if the aggregate
were syntactically invalid.

The NULL-heavy `amount` column is the case worth covering: a model that reads
"38% NULL" still tends to write a bare `AVG()`, so the tool is expected to say
what to do, not just report a percentage.
"""

from __future__ import annotations

import sqlite3

import pytest

from vanna.capabilities.schema_catalog.models import ColumnMetadata, TableMetadata
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog
from vanna.integrations.sqlite.sql_runner import SqliteRunner
from vanna.tools.profile import ProfileColumnArgs, ProfileColumnTool


def _context(tenant: str = "acme") -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "shop.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
          order_id INTEGER PRIMARY KEY AUTOINCREMENT,
          status   TEXT NOT NULL,
          amount   REAL);
        INSERT INTO orders (status, amount) VALUES
          ('new', 10.0), ('new', 20.0), ('new', NULL),
          ('shipped', 30.0), ('shipped', NULL),
          ('cancelled', NULL), ('cancelled', NULL), ('cancelled', 40.0);
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def runner(database):
    return SqliteRunner(database, read_only=True)


class TestProfileColumnTool:
    def test_declares_no_sql_argument_fields(self, runner):
        assert ProfileColumnTool(runner).sql_argument_fields == ()

    async def test_rejects_an_identifier_that_is_not_one(self, runner):
        """The name is interpolated, so the whitelist is the only defence."""
        result = await ProfileColumnTool(runner).execute(
            _context(),
            ProfileColumnArgs(table="orders; DROP TABLE orders", column="status"),
        )
        assert not result.success
        assert "not a valid identifier" in result.error

    async def test_counts_rows_nulls_and_distinct_values(self, runner):
        result = await ProfileColumnTool(runner).execute(
            _context(), ProfileColumnArgs(table="orders", column="amount")
        )

        assert result.success
        assert result.metadata["rows"] == 8
        assert result.metadata["nulls"] == 4
        assert result.metadata["distinct"] == 4

    async def test_warns_when_a_column_is_heavily_null(self, runner):
        result = await ProfileColumnTool(runner).execute(
            _context(), ProfileColumnArgs(table="orders", column="amount")
        )

        text = result.result_for_llm
        assert "50.0%" in text
        # Reporting the percentage is not enough; it must say what to do.
        assert "IS NOT NULL" in text

    async def test_reports_range(self, runner):
        result = await ProfileColumnTool(runner).execute(
            _context(), ProfileColumnArgs(table="orders", column="amount")
        )
        assert "10.0" in result.result_for_llm
        assert "40.0" in result.result_for_llm

    async def test_breaks_down_a_low_cardinality_column(self, runner):
        result = await ProfileColumnTool(runner).execute(
            _context(), ProfileColumnArgs(table="orders", column="status")
        )

        text = result.result_for_llm
        assert "Most common values" in text
        assert "'cancelled'" in text and "3" in text

    async def test_skips_the_breakdown_when_every_value_is_unique(self, runner):
        result = await ProfileColumnTool(runner).execute(
            _context(), ProfileColumnArgs(table="orders", column="order_id")
        )

        assert "Every value is unique" in result.result_for_llm

    async def test_uses_the_catalog_when_one_is_supplied(self, runner):
        catalog = LocalSchemaCatalog()
        context = _context()
        await catalog.upsert_tables(
            context,
            [
                TableMetadata(
                    table_name="orders",
                    row_count_estimate=8,
                    columns=[
                        ColumnMetadata(
                            name="status",
                            data_type="text",
                            description="Order lifecycle state.",
                            low_cardinality=True,
                            categories=["new", "shipped", "cancelled"],
                        )
                    ],
                )
            ],
        )

        result = await ProfileColumnTool(runner, catalog=catalog).execute(
            context, ProfileColumnArgs(table="orders", column="status")
        )

        text = result.result_for_llm
        assert "Order lifecycle state." in text
        assert "Known categories (3)" in text
        assert "roughly 8 rows" in text

    async def test_catalog_answer_survives_an_unreachable_database(self, tmp_path):
        """A scanned column still profiles when the warehouse is down."""
        catalog = LocalSchemaCatalog()
        context = _context()
        await catalog.upsert_tables(
            context,
            [
                TableMetadata(
                    table_name="orders",
                    columns=[ColumnMetadata(name="status", data_type="text")],
                )
            ],
        )
        broken = SqliteRunner(str(tmp_path / "missing.db"), read_only=True)

        result = await ProfileColumnTool(broken, catalog=catalog).execute(
            context, ProfileColumnArgs(table="orders", column="status")
        )

        assert result.success
        assert "orders.status (text)" in result.result_for_llm
        assert "may be out of date" in result.result_for_llm

    async def test_failure_is_reported_when_there_is_nothing_to_fall_back_on(
        self, tmp_path
    ):
        broken = SqliteRunner(str(tmp_path / "missing.db"), read_only=True)
        result = await ProfileColumnTool(broken).execute(
            _context(), ProfileColumnArgs(table="orders", column="status")
        )
        assert not result.success
        assert "Could not profile orders.status" in result.error
