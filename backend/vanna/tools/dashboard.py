"""Tools that let an agent build a dashboard.

The agent authors a *document*, not code. It says "a bar chart of revenue by
region, half width, top left"; the tiles it produces are executed later through
the tool registry like any other query, so access control applies to whatever it
builds without the agent being trusted to preserve it.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

from ..components import (
    ComponentType,
    NotificationComponent,
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from ..core.errors import VannaError
from ..core.tool import Tool, ToolContext, ToolResult
from ..dashboards.models import Dashboard, Tile
from ..dashboards.store import DashboardStore
from ..dashboards.verify import has_errors, verify_dashboard

logger = logging.getLogger(__name__)


class SaveDashboardArgs(BaseModel):
    """Arguments for save_dashboard."""

    title: str = Field(description="Dashboard title.")
    tiles: List[dict] = Field(
        description=(
            "Tiles to render. Each: {kind: chart|table|metric|text, title, "
            "query: {source: sql, sql: '...'} or {source: saved, saved_query_id}, "
            "chart: {type: bar|line|area|pie|scatter, x, y: [...]}, "
            "grid: {x, y, width, height} on a 12-column grid}."
        )
    )
    description: str = Field(default="", description="What this dashboard shows.")
    dashboard_id: Optional[str] = Field(
        default=None, description="Existing dashboard to replace. Omit to create."
    )


class SaveDashboardTool(Tool[SaveDashboardArgs]):
    """Create or replace a dashboard."""

    def __init__(self, store: DashboardStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "save_dashboard"

    @property
    def description(self) -> str:
        return (
            "Save a dashboard of chart, table, metric and text tiles. Each tile "
            "names a query, which is executed with the viewer's own permissions "
            "when the dashboard is opened."
        )

    def get_args_schema(self) -> Type[SaveDashboardArgs]:
        return SaveDashboardArgs

    async def execute(
        self, context: ToolContext, args: SaveDashboardArgs
    ) -> ToolResult:
        try:
            dashboard = Dashboard(
                id=args.dashboard_id or Dashboard.model_fields["id"].default_factory(),
                tenant_id=context.tenant_id,
                title=args.title,
                description=args.description,
                tiles=[Tile.model_validate(tile) for tile in args.tiles],
                created_by=getattr(context.user, "email", "") or context.user.id,
            )
        except Exception as exc:
            # A malformed tile is the agent's mistake to fix, so the message has
            # to say which field, not just "invalid".
            return _failure(f"That dashboard could not be built: {exc}")

        issues = verify_dashboard(dashboard)
        if has_errors(issues):
            return _failure(
                "The dashboard was rejected:\n"
                + "\n".join(str(i) for i in issues if i.severity == "error")
            )

        try:
            saved = await self.store.save(dashboard)
        except VannaError as exc:
            return _failure(exc.args[0] if exc.args else str(exc))

        warnings = [str(i) for i in issues if i.severity == "warning"]
        message = (
            f"Saved dashboard '{saved.title}' with {len(saved.tiles)} tile(s). "
            f"id={saved.id}"
        )
        if warnings:
            message += "\nWarnings:\n" + "\n".join(warnings)

        return ToolResult(
            success=True,
            result_for_llm=message,
            ui_component=UiComponent(
                rich_component=NotificationComponent(
                    type=ComponentType.NOTIFICATION, level="success", message=message
                ),
                simple_component=SimpleTextComponent(text=message),
            ),
            metadata={"dashboard_id": saved.id, "tiles": len(saved.tiles)},
        )


class ListDashboardsArgs(BaseModel):
    """No arguments."""


class ListDashboardsTool(Tool[ListDashboardsArgs]):
    """List the dashboards in this workspace."""

    def __init__(self, store: DashboardStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "list_dashboards"

    @property
    def description(self) -> str:
        return "List the dashboards saved in this workspace."

    def get_args_schema(self) -> Type[ListDashboardsArgs]:
        return ListDashboardsArgs

    async def execute(
        self, context: ToolContext, args: ListDashboardsArgs
    ) -> ToolResult:
        dashboards = await self.store.list(context.tenant_id)
        if not dashboards:
            text = "No dashboards yet."
        else:
            text = "\n".join(
                f"- {d.title} ({len(d.tiles)} tiles, id={d.id})" for d in dashboards
            )

        return ToolResult(
            success=True,
            result_for_llm=text,
            ui_component=UiComponent(
                rich_component=RichTextComponent(content=text, markdown=False),
                simple_component=SimpleTextComponent(text=text),
            ),
            metadata={"count": len(dashboards)},
        )


def _failure(message: str) -> ToolResult:
    return ToolResult(
        success=False,
        result_for_llm=message,
        ui_component=UiComponent(
            rich_component=NotificationComponent(
                type=ComponentType.NOTIFICATION, level="error", message=message
            ),
            simple_component=SimpleTextComponent(text=message),
        ),
        error=message,
        metadata={"error_type": "dashboard_rejected"},
    )


def create_dashboard_tools(store: DashboardStore) -> List[Tool]:
    """Both dashboard tools, for registering in one call."""
    return [SaveDashboardTool(store), ListDashboardsTool(store)]
