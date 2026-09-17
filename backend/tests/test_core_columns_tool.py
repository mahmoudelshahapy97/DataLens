"""check_core_columns: confirming an admin's curated column selection.

The tool has two jobs: list a table's core columns, and (given `columns`)
say which of them are core and which are not. Both read the control-plane
curation store, never the raw catalog scan, and never guess.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.core_columns import CheckCoreColumnsArgs, CheckCoreColumnsTool


class _FakeCatalog:
    """A table with two columns, and a settable core-column map."""

    def __init__(self, core: Optional[Dict[str, List[str]]] = None):
        self.core = core or {}

    async def get_table(self, context, name, *, data_source_id=None):
        key = name.split(".")[-1].lower()
        if key != "orders":
            return None
        return TableMetadata(
            table_name="orders",
            columns=[
                ColumnMetadata(name="id", data_type="int"),
                ColumnMetadata(name="status", data_type="text"),
                ColumnMetadata(name="total", data_type="numeric"),
            ],
            tenant_id=getattr(context, "tenant_id", "default"),
            data_source_id=data_source_id or "default",
        )

    async def get_core_columns_map(self, tenant_id, data_source_id, table_keys):
        return {key: self.core.get(key, []) for key in table_keys}


def _context(tenant: str = "acme") -> ToolContext:
    return ToolContext(
        user=User(id="u1", email=f"u1@{tenant}.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


class TestListingCoreColumns:
    async def test_no_columns_marked_says_so(self):
        tool = CheckCoreColumnsTool(_FakeCatalog())
        result = await tool.execute(_context(), CheckCoreColumnsArgs(table="orders"))
        assert result.success
        assert "No columns" in result.result_for_llm

    async def test_lists_the_marked_columns(self):
        tool = CheckCoreColumnsTool(
            _FakeCatalog(core={"orders": ["id", "status"]})
        )
        result = await tool.execute(_context(), CheckCoreColumnsArgs(table="orders"))
        assert "id" in result.result_for_llm
        assert "status" in result.result_for_llm
        assert result.metadata["core"] == ["id", "status"]


class TestCheckingSpecificColumns:
    async def test_splits_core_and_not_core(self):
        tool = CheckCoreColumnsTool(
            _FakeCatalog(core={"orders": ["id", "status"]})
        )
        result = await tool.execute(
            _context(),
            CheckCoreColumnsArgs(table="orders", columns=["status", "total"]),
        )
        assert result.metadata["is_core"] == ["status"]
        assert result.metadata["not_core"] == ["total"]
        assert "Core: status" in result.result_for_llm
        assert "Not core: total" in result.result_for_llm


class TestUnknownTable:
    async def test_missing_table_is_reported_not_guessed(self):
        tool = CheckCoreColumnsTool(_FakeCatalog())
        result = await tool.execute(
            _context(), CheckCoreColumnsArgs(table="does_not_exist")
        )
        assert result.success
        assert "not found" in result.result_for_llm
