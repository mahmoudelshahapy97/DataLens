"""search_query_history: read-back of the caller's own generations.

`GenerationStore.list_recent` is tenant-scoped but not user-scoped
(`vanna_app/stores.py`), so the tool itself must filter to the caller's own
rows -- that is the regression this file exists to catch. The other two
properties: list mode never leaks SQL (a 200-row dump with SQL is worse than
no tool), and a table whose grant was revoked after the query ran must
suppress the record rather than show it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from vanna.core.generation import GenerationStatus, LocalGenerationStore, SqlGeneration
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.query_history import SearchQueryHistoryArgs, SearchQueryHistoryTool


class _AllVisibleCatalog:
    """No `get_table` at all -- the tool must treat every table as visible."""


class _RevokingCatalog:
    """Visible unless the table name is in `revoked`."""

    def __init__(self, revoked: set):
        self.revoked = revoked

    async def get_table(self, context: Any, name: str) -> Optional[Dict]:
        if name.lower() in self.revoked:
            return None
        return {"name": name}


def _context(tenant: str = "acme", user_id: str = "u1") -> ToolContext:
    return ToolContext(
        user=User(id=user_id, email=f"{user_id}@{tenant}.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


async def _seed(store: LocalGenerationStore, ctx: ToolContext, **overrides) -> SqlGeneration:
    generation = SqlGeneration(
        question=overrides.pop("question", "revenue last quarter"),
        sql=overrides.pop("sql", "SELECT * FROM orders"),
        status=overrides.pop("status", GenerationStatus.VALID),
        row_count=overrides.pop("row_count", 12),
        **overrides,
    )
    return await store.record(ctx, generation)


class TestListModeHasNoSql:
    async def test_no_sql_in_list_output(self):
        store = LocalGenerationStore()
        ctx = _context()
        await _seed(store, ctx, sql="SELECT customer_id, total FROM orders")
        tool = SearchQueryHistoryTool(store, _AllVisibleCatalog())

        result = await tool.execute(ctx, SearchQueryHistoryArgs())

        assert "SELECT" not in result.result_for_llm

    async def test_drill_down_returns_the_sql(self):
        store = LocalGenerationStore()
        ctx = _context()
        record = await _seed(store, ctx, sql="SELECT customer_id, total FROM orders")
        tool = SearchQueryHistoryTool(store, _AllVisibleCatalog())

        result = await tool.execute(
            ctx, SearchQueryHistoryArgs(generation_id=record.id)
        )

        assert "SELECT customer_id, total FROM orders" in result.result_for_llm


class TestUserScope:
    async def test_another_user_in_the_same_tenant_is_invisible(self):
        store = LocalGenerationStore()
        alice = _context(tenant="acme", user_id="alice")
        bob = _context(tenant="acme", user_id="bob")
        await _seed(store, alice, question="alice's private question")
        tool = SearchQueryHistoryTool(store, _AllVisibleCatalog())

        result = await tool.execute(bob, SearchQueryHistoryArgs())

        assert "alice's private question" not in result.result_for_llm

    async def test_drill_down_on_someone_elses_id_reads_as_not_found(self):
        store = LocalGenerationStore()
        alice = _context(tenant="acme", user_id="alice")
        bob = _context(tenant="acme", user_id="bob")
        record = await _seed(store, alice)
        tool = SearchQueryHistoryTool(store, _AllVisibleCatalog())

        result = await tool.execute(
            bob, SearchQueryHistoryArgs(generation_id=record.id)
        )

        assert "No generation with id" in result.result_for_llm
        assert record.sql not in result.result_for_llm


class TestCrossTenantIsolation:
    async def test_a_generation_never_crosses_tenants(self):
        store = LocalGenerationStore()
        acme = _context(tenant="acme", user_id="u1")
        globex = _context(tenant="globex", user_id="u1")
        await _seed(store, acme, question="acme's question")
        tool = SearchQueryHistoryTool(store, _AllVisibleCatalog())

        result = await tool.execute(globex, SearchQueryHistoryArgs())

        assert "acme's question" not in result.result_for_llm


class TestGrantDrift:
    async def test_a_record_referencing_a_revoked_table_is_suppressed_and_counted(self):
        store = LocalGenerationStore()
        ctx = _context()
        await _seed(store, ctx, sql="SELECT * FROM secret_table", question="q1")
        tool = SearchQueryHistoryTool(store, _RevokingCatalog({"secret_table"}))

        result = await tool.execute(ctx, SearchQueryHistoryArgs())

        assert "q1" not in result.result_for_llm
        assert "1 older entry not shown" in result.result_for_llm

    async def test_drill_down_on_a_revoked_table_withholds_the_sql(self):
        store = LocalGenerationStore()
        ctx = _context()
        record = await _seed(store, ctx, sql="SELECT * FROM secret_table")
        tool = SearchQueryHistoryTool(store, _RevokingCatalog({"secret_table"}))

        result = await tool.execute(
            ctx, SearchQueryHistoryArgs(generation_id=record.id)
        )

        assert "secret_table" not in result.result_for_llm
        assert record.sql not in result.result_for_llm
