"""`search_tables` / `get_table_schema` -- the "describe the schema" tools.

Before this file, nothing exercised these directly: `test_schema_ui.py` covers
the admin schema-*editor*, and `test_sql_policy_scope.py` only imports
`SearchTablesTool` to check it is exempt from SQL-argument policy checks. The
docstring in `vanna/tools/schema.py` calls these "the escape hatch" for
anything the injected system-prompt context missed, so their empty-result
messaging, missing-table handling, and relationship-filtering logic are worth
covering on their own. `LocalSchemaCatalog` (in-memory, dependency-free) backs
these the same way it can back a real deployment with no vector store
configured.
"""

from __future__ import annotations

import pytest

from vanna.capabilities.schema_catalog.models import (
    ColumnMetadata,
    RelationshipMetadata,
    TableMetadata,
)
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog
from vanna.tools.schema import (
    GetTableSchemaArgs,
    GetTableSchemaTool,
    SearchTablesArgs,
    SearchTablesTool,
    create_schema_tools,
)


def _context(tenant: str = "acme") -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


async def _seeded_catalog(tenant: str = "acme") -> LocalSchemaCatalog:
    catalog = LocalSchemaCatalog()
    context = _context(tenant)
    await catalog.upsert_tables(
        context,
        [
            TableMetadata(
                table_name="orders",
                schema_name="public",
                description="Customer orders.",
                tenant_id=tenant,
                columns=[
                    ColumnMetadata(name="id", data_type="integer", is_primary_key=True),
                    ColumnMetadata(name="customer_id", data_type="integer"),
                    ColumnMetadata(name="total", data_type="numeric"),
                ],
            ),
            TableMetadata(
                table_name="customers",
                schema_name="public",
                description="Customer accounts.",
                tenant_id=tenant,
                columns=[
                    ColumnMetadata(name="id", data_type="integer", is_primary_key=True),
                    ColumnMetadata(name="name", data_type="text"),
                ],
            ),
            TableMetadata(
                table_name="products",
                schema_name="public",
                description="Product catalog, unrelated to orders in this fixture.",
                tenant_id=tenant,
                columns=[ColumnMetadata(name="id", data_type="integer", is_primary_key=True)],
            ),
        ],
    )
    await catalog.upsert_relationships(
        context,
        [
            RelationshipMetadata(
                name="orders_customer_fk",
                from_table="public.orders",
                from_column="customer_id",
                to_table="public.customers",
                to_column="id",
                tenant_id=tenant,
            )
        ],
    )
    return catalog


class TestCreateSchemaTools:
    def test_returns_both_tools_unrestricted(self):
        catalog = LocalSchemaCatalog()
        tools = create_schema_tools(catalog)
        assert {t.name for t in tools} == {"search_tables", "get_table_schema"}
        assert all(t.access_groups == [] for t in tools)

    def test_search_tables_declares_no_sql_argument_fields(self):
        assert SearchTablesTool(LocalSchemaCatalog()).sql_argument_fields == ()


class TestSearchTables:
    async def test_finds_relevant_tables(self):
        catalog = await _seeded_catalog()
        tool = SearchTablesTool(catalog)

        result = await tool.execute(_context(), SearchTablesArgs(query="orders"))

        assert result.success
        assert "orders" in result.result_for_llm
        assert "public.orders" in result.metadata["tables"]

    async def test_empty_catalog_says_so_without_inventing_a_table(self):
        tool = SearchTablesTool(LocalSchemaCatalog())

        result = await tool.execute(
            _context(), SearchTablesArgs(query="anything at all")
        )

        assert result.success
        assert "No tables matching" in result.result_for_llm

    async def test_catalog_exception_is_a_failure_not_a_crash(self):
        class _BrokenCatalog(LocalSchemaCatalog):
            async def search_tables(self, *args, **kwargs):
                raise RuntimeError("catalog index is corrupt")

        tool = SearchTablesTool(_BrokenCatalog())

        result = await tool.execute(_context(), SearchTablesArgs(query="orders"))

        assert not result.success
        assert "corrupt" in result.result_for_llm

    async def test_tenants_do_not_see_each_others_tables(self):
        catalog = await _seeded_catalog(tenant="acme")
        tool = SearchTablesTool(catalog)

        result = await tool.execute(
            _context(tenant="globex"), SearchTablesArgs(query="orders")
        )

        assert "No tables matching" in result.result_for_llm


class TestGetTableSchema:
    async def test_describes_a_found_table(self):
        catalog = await _seeded_catalog()
        tool = GetTableSchemaTool(catalog)

        result = await tool.execute(
            _context(), GetTableSchemaArgs(tables=["orders"])
        )

        assert result.success
        assert "customer_id" in result.result_for_llm
        assert result.metadata["found"] == ["public.orders"]
        assert result.metadata["missing"] == []

    async def test_all_missing_names_the_misses_rather_than_a_blank_response(self):
        catalog = await _seeded_catalog()
        tool = GetTableSchemaTool(catalog)

        result = await tool.execute(
            _context(), GetTableSchemaArgs(tables=["not_a_real_table"])
        )

        assert result.success  # a clear miss, not a tool failure
        assert "not_a_real_table" in result.result_for_llm
        assert result.metadata["missing"] == ["not_a_real_table"]

    async def test_a_mix_of_found_and_missing_reports_both(self):
        catalog = await _seeded_catalog()
        tool = GetTableSchemaTool(catalog)

        result = await tool.execute(
            _context(), GetTableSchemaArgs(tables=["orders", "ghost_table"])
        )

        assert result.success
        assert "public.orders" in result.metadata["found"]
        assert "ghost_table" in result.metadata["missing"]
        assert "Not found in the catalog: ghost_table" in result.result_for_llm

    async def test_relationships_are_included_when_both_ends_are_in_scope(self):
        catalog = await _seeded_catalog()
        tool = GetTableSchemaTool(catalog)

        result = await tool.execute(
            _context(), GetTableSchemaArgs(tables=["orders", "customers"])
        )

        assert "orders_customer_fk" in result.result_for_llm or "customer_id" in result.result_for_llm

    async def test_relationship_to_a_table_out_of_scope_is_omitted(self):
        catalog = await _seeded_catalog()
        tool = GetTableSchemaTool(catalog)

        # Only "products" is requested; the orders<->customers relationship
        # touches neither end of it and must not show up as noise.
        result = await tool.execute(_context(), GetTableSchemaArgs(tables=["products"]))

        assert "orders_customer_fk" not in result.result_for_llm

    async def test_get_table_exception_is_a_failure_not_a_crash(self):
        class _BrokenCatalog(LocalSchemaCatalog):
            async def get_table(self, *args, **kwargs):
                raise RuntimeError("connection to the catalog store dropped")

        tool = GetTableSchemaTool(_BrokenCatalog())

        result = await tool.execute(_context(), GetTableSchemaArgs(tables=["orders"]))

        assert not result.success
        assert "dropped" in result.result_for_llm

    async def test_a_relationships_lookup_failure_does_not_fail_the_whole_call(self):
        """Relationships are "a bonus, not a requirement" per the tool's own
        comment -- a broken relationship lookup must still return the schema
        it already has."""
        catalog = await _seeded_catalog()

        class _RelationshipsExplode(LocalSchemaCatalog):
            async def get_relationships(self, *args, **kwargs):
                raise RuntimeError("relationship index is being rebuilt")

        catalog.__class__ = _RelationshipsExplode
        tool = GetTableSchemaTool(catalog)

        result = await tool.execute(_context(), GetTableSchemaArgs(tables=["orders"]))

        assert result.success
        assert "customer_id" in result.result_for_llm

    async def test_tenants_do_not_see_each_others_schema(self):
        catalog = await _seeded_catalog(tenant="acme")
        tool = GetTableSchemaTool(catalog)

        result = await tool.execute(
            _context(tenant="globex"), GetTableSchemaArgs(tables=["orders"])
        )

        assert "orders" in result.metadata["missing"]
