"""Dashboards as declarative documents, not generated code.

    from vanna.dashboards import Dashboard, Tile, render_dashboard

A dashboard is a list of tiles, each naming a query. Tiles are executed through
the tool registry at render time, so the SQL policy, semantic compilation and
row/column rules apply to a dashboard exactly as they do to a chat question --
which is the property generated dashboard code cannot have.
"""

from .models import (
    ChartSpec,
    ChartType,
    CubeQuery,
    Dashboard,
    GridPosition,
    InlineSql,
    SavedQueryRef,
    Tile,
    TileKind,
    TileResult,
)
from .params import (
    Parameter,
    ParameterError,
    ParameterType,
    declared_placeholders,
    placeholders_in,
    resolve,
    substitute,
)
from .export import export_filename, export_html
from .render import MAX_TILE_ROWS, render_dashboard
from .store import DashboardStore, LocalDashboardStore, validated
from .verify import VerifyIssue, has_errors, verify_dashboard, verify_tile

__all__ = [
    "export_html",
    "export_filename",
    "Parameter",
    "ParameterError",
    "ParameterType",
    "declared_placeholders",
    "placeholders_in",
    "resolve",
    "substitute",
    "Dashboard",
    "Tile",
    "TileKind",
    "TileResult",
    "ChartSpec",
    "ChartType",
    "GridPosition",
    "SavedQueryRef",
    "InlineSql",
    "CubeQuery",
    "DashboardStore",
    "LocalDashboardStore",
    "validated",
    "render_dashboard",
    "MAX_TILE_ROWS",
    "verify_dashboard",
    "verify_tile",
    "VerifyIssue",
    "has_errors",
]
