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


class TestCaseNormalization:
    """`columns` is checked against the curation set case-insensitively, via
    `normalize_identifier` -- a model that writes `STATUS` because that is how
    it appeared in a schema listing must still match a `status` curated
    lower-case."""

    async def test_mixed_case_input_still_matches(self):
        tool = CheckCoreColumnsTool(
            _FakeCatalog(core={"orders": ["id", "status"]})
        )

        result = await tool.execute(
            _context(),
            CheckCoreColumnsArgs(table="orders", columns=["STATUS", "Total"]),
        )

        assert result.metadata["is_core"] == ["status"]
        assert result.metadata["not_core"] == ["total"]

    async def test_schema_qualified_table_name_still_resolves(self):
        tool = CheckCoreColumnsTool(
            _FakeCatalog(core={"orders": ["id"]})
        )

        result = await tool.execute(
            _context(), CheckCoreColumnsArgs(table="Public.ORDERS")
        )

        assert result.success
        assert result.metadata["core"] == ["id"]


class TestCoreColumnsLookupFailure:
    async def test_a_store_exception_is_a_failure_not_a_crash(self):
        class _BrokenCatalog(_FakeCatalog):
            async def get_core_columns_map(self, tenant_id, data_source_id, table_keys):
                raise RuntimeError("control-plane database is unreachable")

        tool = CheckCoreColumnsTool(_BrokenCatalog())

        result = await tool.execute(_context(), CheckCoreColumnsArgs(table="orders"))

        assert not result.success
        assert "unreachable" in result.result_for_llm


class TestTenantIsolation:
    """`_FakeCatalog` above ignores tenant scoping entirely (it is keyed only
    by table name), which is fine for the tool-logic tests it backs but would
    silently pass even if the tool forgot to pass `tenant_scope(context)`
    through. This uses a tenant-aware fake to prove that value actually flows
    into `get_core_columns_map`."""

    class _TenantAwareCatalog(_FakeCatalog):
        def __init__(self, core_by_tenant):
            super().__init__()
            self.core_by_tenant = core_by_tenant

        async def get_core_columns_map(self, tenant_id, data_source_id, table_keys):
            core = self.core_by_tenant.get(tenant_id, {})
            return {key: core.get(key, []) for key in table_keys}

    async def test_a_tenants_curation_is_invisible_to_another_tenant(self):
        catalog = self._TenantAwareCatalog(
            core_by_tenant={"acme": {"orders": ["id", "status"]}}
        )
        tool = CheckCoreColumnsTool(catalog)

        acme_result = await tool.execute(
            _context("acme"), CheckCoreColumnsArgs(table="orders")
        )
        globex_result = await tool.execute(
            _context("globex"), CheckCoreColumnsArgs(table="orders")
        )

        assert acme_result.metadata["core"] == ["id", "status"]
        assert globex_result.metadata["core"] == []
