"""`SaveDashboardTool` / `ListDashboardsTool`, through the tool registry.

Before this file, dashboards had test coverage for parameter substitution
(`test_dashboards_params.py`), static export (`test_dashboard_export*.py`),
and tile-to-figure rendering (`test_dashboard_tiles_ui.py`) -- but nothing
exercised `create_dashboard_tools`/`SaveDashboardTool.execute`/
`ListDashboardsTool.execute` themselves, and `test_interface_conformance.py`
only checks `PostgresDashboardStore`'s method signatures, never its behavior.
`LocalDashboardStore` (dependency-free, in-memory when given no path) stands
in for the real Postgres-backed store the same way it does in production for
single-process deployments.
"""

from __future__ import annotations

import pytest

from vanna.core.registry import ToolRegistry
from vanna.core.tool import ToolCall, ToolContext
from vanna.core.user import User
from vanna.dashboards.store import LocalDashboardStore
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.dashboard import (
    ListDashboardsTool,
    SaveDashboardArgs,
    SaveDashboardTool,
    create_dashboard_tools,
)


def _context(tenant: str = "acme", email: str = "author@acme.test") -> ToolContext:
    return ToolContext(
        user=User(id=email, email=email, tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


def _valid_args(**overrides) -> SaveDashboardArgs:
    defaults = dict(
        title="Revenue",
        tiles=[{"kind": "table", "query": {"source": "sql", "sql": "SELECT 1"}}],
    )
    defaults.update(overrides)
    return SaveDashboardArgs(**defaults)


class TestCreateDashboardTools:
    def test_returns_both_tools(self):
        store = LocalDashboardStore()
        tools = create_dashboard_tools(store)
        assert {t.name for t in tools} == {"save_dashboard", "list_dashboards"}


class TestSaveDashboard:
    async def test_saves_and_reports_tile_count(self):
        store = LocalDashboardStore()
        tool = SaveDashboardTool(store)

        result = await tool.execute(_context(), _valid_args())

        assert result.success
        assert "Saved dashboard 'Revenue'" in result.result_for_llm
        assert "1 tile(s)" in result.result_for_llm
        assert result.metadata["tiles"] == 1

    async def test_a_malformed_tile_is_rejected_not_a_crash(self):
        store = LocalDashboardStore()
        tool = SaveDashboardTool(store)

        result = await tool.execute(
            _context(),
            _valid_args(tiles=[{"kind": "not-a-real-kind"}]),
        )

        assert not result.success
        assert result.error is not None

    async def test_a_tile_with_an_embedded_secret_is_rejected(self):
        store = LocalDashboardStore()
        tool = SaveDashboardTool(store)

        result = await tool.execute(
            _context(),
            _valid_args(
                tiles=[
                    {
                        "kind": "table",
                        "query": {
                            "source": "sql",
                            "sql": "-- postgresql://admin:Sup3rSecret@prod-db/app\n"
                            "SELECT 1",
                        },
                    }
                ]
            ),
        )

        assert not result.success
        assert "rejected" in result.result_for_llm.lower()

    async def test_a_chart_tile_with_no_chart_spec_still_saves_with_a_warning(self):
        store = LocalDashboardStore()
        tool = SaveDashboardTool(store)

        result = await tool.execute(
            _context(),
            _valid_args(
                tiles=[
                    {
                        "kind": "chart",
                        "query": {"source": "sql", "sql": "SELECT 1"},
                    }
                ]
            ),
        )

        assert result.success
        assert "Warnings" in result.result_for_llm

    async def test_store_failure_surfaces_as_a_failed_result_not_an_exception(self):
        from vanna.core.errors import ErrorCode, ErrorPhase, VannaError

        class _BrokenStore(LocalDashboardStore):
            async def save(self, dashboard):
                raise VannaError(
                    ErrorCode.INVALID_REQUEST,
                    "the control plane is unavailable",
                    phase=ErrorPhase.VISUALIZATION,
                )

        tool = SaveDashboardTool(_BrokenStore())

        result = await tool.execute(_context(), _valid_args())

        assert not result.success
        assert "control plane is unavailable" in result.result_for_llm

    async def test_replacing_an_existing_dashboard_keeps_its_id(self):
        store = LocalDashboardStore()
        tool = SaveDashboardTool(store)
        context = _context()

        first = await tool.execute(context, _valid_args())
        dashboard_id = first.metadata["dashboard_id"]

        second = await tool.execute(
            context, _valid_args(dashboard_id=dashboard_id, title="Revenue (v2)")
        )

        assert second.metadata["dashboard_id"] == dashboard_id
        saved = await store.get(context.tenant_id, dashboard_id)
        assert saved.title == "Revenue (v2)"


class TestListDashboards:
    async def test_empty_workspace_says_so(self):
        tool = ListDashboardsTool(LocalDashboardStore())

        result = await tool.execute(_context(), tool.get_args_schema()())

        assert result.success
        assert "No dashboards" in result.result_for_llm
        assert result.metadata["count"] == 0

    async def test_lists_what_was_saved(self):
        store = LocalDashboardStore()
        save_tool = SaveDashboardTool(store)
        list_tool = ListDashboardsTool(store)
        context = _context()

        await save_tool.execute(context, _valid_args(title="Revenue"))
        await save_tool.execute(context, _valid_args(title="Churn"))

        result = await list_tool.execute(context, list_tool.get_args_schema()())

        assert "Revenue" in result.result_for_llm
        assert "Churn" in result.result_for_llm
        assert result.metadata["count"] == 2

    async def test_tenants_do_not_see_each_others_dashboards(self):
        store = LocalDashboardStore()
        save_tool = SaveDashboardTool(store)
        list_tool = ListDashboardsTool(store)

        await save_tool.execute(_context(tenant="acme"), _valid_args(title="Acme's"))

        result = await list_tool.execute(_context(tenant="globex"), list_tool.get_args_schema()())

        assert "Acme's" not in result.result_for_llm
        assert result.metadata["count"] == 0


class TestRoleGatingThroughTheRegistry:
    """Mirrors the production wiring in `vanna_app/platform.py`: both dashboard
    tools are registered `["admin", "analyst"]` -- a viewer cannot even list
    dashboards through the agent this way (only through the REST route, which
    uses its own `forbid_viewer` check)."""

    @pytest.fixture
    def registry(self):
        registry = ToolRegistry()
        store = LocalDashboardStore()
        for tool in create_dashboard_tools(store):
            registry.register_local_tool(tool, ["admin", "analyst"])
        return registry, store

    async def test_a_viewer_cannot_list_dashboards_via_the_agent(self, registry):
        registry, _ = registry
        viewer = User(id="v", email="v@acme.test", tenant_id="acme", group_memberships=[])
        context = ToolContext(
            user=viewer,
            conversation_id="c1",
            request_id="r1",
            tenant_id="acme",
            agent_memory=DemoAgentMemory(),
        )

        result = await registry.execute(
            ToolCall(id="1", name="list_dashboards", arguments={}), context
        )

        assert not result.success
        assert "access" in (result.error or "").lower()

    async def test_an_analyst_can_save_and_list(self, registry):
        registry, _ = registry
        analyst = User(
            id="a", email="a@acme.test", tenant_id="acme", group_memberships=["analyst"]
        )
        context = ToolContext(
            user=analyst,
            conversation_id="c1",
            request_id="r1",
            tenant_id="acme",
            agent_memory=DemoAgentMemory(),
        )

        saved = await registry.execute(
            ToolCall(
                id="1",
                name="save_dashboard",
                arguments={
                    "title": "By an analyst",
                    "tiles": [{"kind": "table", "query": {"source": "sql", "sql": "SELECT 1"}}],
                },
            ),
            context,
        )
        assert saved.success

        listed = await registry.execute(
            ToolCall(id="2", name="list_dashboards", arguments={}), context
        )
        assert "By an analyst" in listed.result_for_llm

    async def test_the_schema_offered_to_a_viewer_excludes_dashboard_tools(self, registry):
        registry, _ = registry
        viewer = User(id="v", email="v@acme.test", tenant_id="acme", group_memberships=[])

        schemas = await registry.get_schemas(viewer)

        assert {s.name for s in schemas} == set()
