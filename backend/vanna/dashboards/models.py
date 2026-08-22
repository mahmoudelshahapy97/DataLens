"""Dashboards as declarative documents.

The tempting design -- the one WrenAI uses -- is to have the agent *write a
dashboard application*: HTML, a chart library, queries baked in. It demos
beautifully and it cannot be secured. Generated code is opaque to the tool
registry, so nothing applies the SQL policy to it, nothing compiles its model
references, and nothing injects a row-level predicate. Every guarantee built in
the phases before this one stops at the boundary of a generated file.

So a dashboard here is *data*: a list of tiles, each naming a query. At render
time every tile is executed through ``ToolRegistry.execute`` like any other
query, which means row and column rules apply to dashboards **for free** and
without this module knowing they exist.

The cost is that a tile can only do what a tile can do. That is the right trade:
a bespoke visual is still available through ``ArtifactComponent``, and it is
correctly *not* how dashboards work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TileKind(str, Enum):
    """What a tile renders.

    A closed set on purpose. ``verify`` rejects anything not listed here rather
    than skipping it -- an unrecognised kind is far more likely to be a typo
    than a feature, and skipping it silently drops a tile from a dashboard
    somebody is reading numbers off.
    """

    CHART = "chart"
    TABLE = "table"
    METRIC = "metric"
    """A single number, large. The most-read tile type there is."""
    TEXT = "text"
    """Static markdown. Section headings and caveats."""


class ChartType(str, Enum):
    BAR = "bar"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    SCATTER = "scatter"
    HEATMAP = "heatmap"


class ChartSpec(BaseModel):
    """How to draw a result set.

    This closes a real gap. ``VisualizeDataTool`` takes only a filename and a
    title, leaving ``PlotlyChartGenerator``'s heuristics to guess the chart type
    from column dtypes -- so a model that knows perfectly well the user asked
    for a trend line has no way to say so. Every field here is optional and the
    heuristics remain the fallback, so nothing that works today changes.
    """

    type: Optional[ChartType] = None
    x: Optional[str] = Field(default=None, description="Column for the x axis.")
    y: Optional[List[str]] = Field(default=None, description="Columns to plot.")
    color_by: Optional[str] = Field(
        default=None, description="Column to split series on."
    )
    stacked: bool = False
    sort_by: Optional[str] = None
    descending: bool = False
    limit: Optional[int] = Field(
        default=None, description="Categories to show before grouping the rest."
    )
    x_label: Optional[str] = None
    y_label: Optional[str] = None

    @field_validator("y")
    @classmethod
    def _non_empty(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return value or None


# ----------------------------------------------------------------------
# Query sources
# ----------------------------------------------------------------------


class SavedQueryRef(BaseModel):
    """Points at a saved query by id.

    The preferred shape: the SQL lives in one place, so fixing it fixes every
    dashboard that uses it.
    """

    source: str = "saved"
    saved_query_id: str


class InlineSql(BaseModel):
    """SQL written into the tile.

    Semantic SQL, when a manifest is configured -- it is compiled and
    access-checked at render time exactly like a chat query, because it goes
    through the same registry.
    """

    source: str = "sql"
    sql: str


class CubeQuery(BaseModel):
    """A structured aggregate over a cube.

    Safer than inline SQL for anything aggregated: the measure's aggregation was
    decided by whoever defined the cube, so a tile cannot invent a SUM that
    double-counts across a one-to-many join.
    """

    source: str = "cube"
    cube: str
    measures: List[str] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)
    time_dimension: Optional[str] = None
    granularity: Optional[str] = None
    filters: List[str] = Field(default_factory=list)


TileQuery = Union[SavedQueryRef, InlineSql, CubeQuery]


# ----------------------------------------------------------------------
# Tiles and dashboards
# ----------------------------------------------------------------------


class GridPosition(BaseModel):
    """Where a tile sits, on a 12-column grid.

    Twelve because it divides by 2, 3, 4 and 6 -- every layout anyone actually
    asks for is expressible without fractions.
    """

    x: int = Field(default=0, ge=0, le=11)
    y: int = Field(default=0, ge=0)
    width: int = Field(default=6, ge=1, le=12)
    height: int = Field(default=4, ge=1, le=24)


class Tile(BaseModel):
    """One panel."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    kind: TileKind
    title: str = ""
    description: str = ""

    query: Optional[TileQuery] = None
    chart: Optional[ChartSpec] = None
    text: Optional[str] = Field(default=None, description="Markdown, for TEXT tiles.")

    grid: GridPosition = Field(default_factory=GridPosition)
    #: Client-side refresh interval. None means only on load -- the default,
    #: because a dashboard nobody is watching should not be issuing warehouse
    #: queries all day.
    refresh_seconds: Optional[int] = Field(default=None, ge=15)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: TileKind) -> TileKind:
        return value

    def requires_query(self) -> bool:
        return self.kind in (TileKind.CHART, TileKind.TABLE, TileKind.METRIC)


class Dashboard(BaseModel):
    """A named collection of tiles, owned by a tenant."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    tenant_id: str = "default"
    title: str
    description: str = ""
    tiles: List[Tile] = Field(default_factory=list)

    created_by: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def tile(self, tile_id: str) -> Optional[Tile]:
        return next((t for t in self.tiles if t.id == tile_id), None)

    def to_json_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class TileResult(BaseModel):
    """One tile's data, after execution.

    ``error`` is per tile rather than per dashboard: one broken query should
    leave the other eleven panels readable, with the failure visible on the one
    that failed.
    """

    tile_id: str
    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    """Compiler caveats -- a fan-out warning, say. Rendered on the tile, because
    a warning that only reaches a log is a warning nobody acts on."""
