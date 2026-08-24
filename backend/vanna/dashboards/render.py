"""Executing a dashboard's tiles.

Every tile goes through ``ToolRegistry.execute``, the same path a chat question
takes. That single decision is what gives dashboards access control: the
registry's ``transform_args`` compiles semantic SQL, injects the caller's
row-level predicate, drops columns they may not read, and applies the SQL
policy -- and none of it is mentioned in this file, because none of it is this
file's business.

The alternative, running ``runner.run_sql`` directly, is one line shorter and
bypasses all four.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from ..core.errors import ErrorPhase, VannaError
from .params import ParameterError, resolve, substitute
from .models import (
    CubeQuery,
    Dashboard,
    InlineSql,
    SavedQueryRef,
    Tile,
    TileKind,
    TileResult,
)

logger = logging.getLogger(__name__)

#: Rows a tile may return. A dashboard panel that needs more than this is a
#: report, and should be an export.
MAX_TILE_ROWS = 1000

#: Tiles executed at once. Bounded because a twelve-panel dashboard opening
#: twelve concurrent warehouse queries is how one person's page load becomes
#: everyone's slow afternoon.
MAX_CONCURRENCY = 4


async def render_dashboard(
    dashboard: Dashboard,
    *,
    registry: Any,
    user: Any,
    agent_memory: Any,
    saved_query_sql: Optional[Dict[str, str]] = None,
    max_rows: int = MAX_TILE_ROWS,
    params: Optional[Dict[str, Any]] = None,
) -> List[TileResult]:
    """Execute every tile, as this user.

    Args:
        registry: The tool registry. Must be the tenant's real one -- it carries
            the policy and the access rules.
        saved_query_sql: Saved-query id -> SQL, resolved by the caller. Passed in
            rather than looked up here so this module needs no storage
            dependency.
        params: Values for the dashboard's declared parameters. Resolved once here
            rather than per tile, so every panel on the page is answering the same
            question -- a report whose tiles disagreed about the date range would
            be worse than one that failed.
    """
    try:
        rendered_params = resolve(dashboard.parameters, params)
    except ParameterError as exc:
        # One bad value fails the whole render on purpose. Rendering the other
        # tiles would produce a page that looks complete and answers a different
        # question than the controls claim.
        return [
            TileResult(tile_id=tile.id, error=str(exc)) for tile in dashboard.tiles
        ]

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def run(tile: Tile) -> TileResult:
        async with semaphore:
            return await _render_tile(
                tile,
                registry=registry,
                user=user,
                agent_memory=agent_memory,
                saved_query_sql=saved_query_sql or {},
                max_rows=max_rows,
                params=rendered_params,
            )

    # return_exceptions: one tile failing must leave the other panels readable.
    results = await asyncio.gather(
        *(run(tile) for tile in dashboard.tiles), return_exceptions=True
    )

    rendered: List[TileResult] = []
    for tile, result in zip(dashboard.tiles, results):
        if isinstance(result, BaseException):
            logger.warning("Tile %s failed: %s", tile.id, result)
            rendered.append(
                TileResult(tile_id=tile.id, error=_message(result))
            )
        else:
            rendered.append(result)
    return rendered


async def _render_tile(
    tile: Tile,
    *,
    registry: Any,
    user: Any,
    agent_memory: Any,
    saved_query_sql: Dict[str, str],
    max_rows: int,
    params: Optional[Dict[str, str]] = None,
) -> TileResult:
    if tile.kind is TileKind.TEXT:
        return TileResult(tile_id=tile.id)

    sql = _resolve_sql(tile, saved_query_sql)
    if sql is None:
        return TileResult(tile_id=tile.id, error="This tile has no query.")

    # Before the registry, which is what puts the finished statement in front of the
    # SQL policy. Substituting afterwards would mean the policy approved a statement
    # that is not the one that runs.
    try:
        sql = substitute(sql, params or {})
    except ParameterError as exc:
        return TileResult(tile_id=tile.id, error=str(exc))

    from ..core.tool import ToolCall, ToolContext

    context = ToolContext(
        user=user,
        conversation_id=f"dashboard:{tile.id}",
        request_id=str(uuid.uuid4()),
        tenant_id=user.tenant_id,
        agent_memory=agent_memory,
    )

    result = await registry.execute(
        ToolCall(id=str(uuid.uuid4()), name="run_sql", arguments={"sql": sql}),
        context,
    )

    warnings = list((context.metadata or {}).get("semantic_warnings") or [])

    if not result.success:
        return TileResult(
            tile_id=tile.id, error=result.error or "The query failed.", warnings=warnings
        )

    metadata = result.metadata or {}
    rows = metadata.get("results") or []
    columns = metadata.get("columns") or []

    return TileResult(
        tile_id=tile.id,
        columns=list(columns),
        rows=[list(row.values()) if isinstance(row, dict) else list(row)
              for row in rows[:max_rows]],
        row_count=int(metadata.get("row_count") or len(rows)),
        truncated=bool(metadata.get("truncated")) or len(rows) > max_rows,
        warnings=warnings,
    )


def _resolve_sql(tile: Tile, saved_query_sql: Dict[str, str]) -> Optional[str]:
    """The statement a tile runs, before compilation.

    Semantic SQL where a manifest exists -- the registry compiles it. Building
    physical SQL here would skip that, and with it the access rules.
    """
    query = tile.query
    if query is None:
        return None

    if isinstance(query, InlineSql):
        return query.sql

    if isinstance(query, SavedQueryRef):
        return saved_query_sql.get(query.saved_query_id)

    if isinstance(query, CubeQuery):
        return _cube_to_sql(query)

    return None


def _cube_to_sql(query: CubeQuery) -> str:
    """Render a structured cube request as semantic SQL.

    Emitted against model names so it goes through the compiler like everything
    else. The measures are names, not expressions -- their aggregation was
    decided when the cube was defined, which is what stops a tile inventing a
    SUM that double-counts across a one-to-many join.
    """
    selected = [*query.dimensions, *query.measures]
    if query.time_dimension:
        selected.insert(0, query.time_dimension)

    statement = f"SELECT {', '.join(selected) or '*'} FROM {query.cube}"

    if query.filters:
        statement += " WHERE " + " AND ".join(f"({f})" for f in query.filters)

    grouping = [*query.dimensions]
    if query.time_dimension:
        grouping.insert(0, query.time_dimension)
    if grouping and query.measures:
        statement += " GROUP BY " + ", ".join(grouping)

    return statement


def _message(error: BaseException) -> str:
    """A tile-sized error, sanitised.

    Driver messages echo the statement; a dashboard is the last place that
    should be rendered, since the reader is often not the author.
    """
    if isinstance(error, VannaError):
        return error.args[0] if error.args else str(error)
    return VannaError.from_exception(
        error, phase=ErrorPhase.VISUALIZATION
    ).args[0]
