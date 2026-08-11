"""``vanna cube`` -- querying a cube by naming measures and dimensions.

The safe way to ask for an aggregate. A hand-written ``SUM`` has to get the
grain right; picking a measure by name cannot get it wrong, because whoever
defined the cube already decided how that number is computed. This is the same
argument that makes ``QueryCubeTool`` the aggregate surface for the agent, and
it applies just as well to a person at a terminal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional, Tuple

import click

from ..capabilities.sql_runner import RunSqlToolArgs
from ..core.errors import ErrorCode, ErrorPhase, VannaError
from ..semantic import load_built_manifest
from ..semantic.compiler import GRANULARITIES, compile_sql, truncate
from ._runtime import build_runner, load_context, system_context


def _manifest(path: Optional[Path]):
    project, profile = load_context(path)
    if project is None:
        raise click.ClickException("No project found. Run `vanna project init`.")

    manifest = load_built_manifest(project.paths)
    if manifest is None:
        raise click.ClickException(
            "No built manifest. Run `vanna project build` first."
        )
    return project, profile, manifest


@click.group()
def cube() -> None:
    """Query pre-defined measures and dimensions."""


@cube.command("list")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def list_cubes(path: Optional[Path]) -> None:
    """List the cubes this project defines."""
    _, _, manifest = _manifest(path)
    if not manifest.cubes:
        click.echo("No cubes defined. Add one under cubes/ and rebuild.")
        return
    for item in manifest.cubes:
        click.echo(
            f"{item.name:<22} over {item.base_object:<18} "
            f"{len(item.measures)} measures, "
            f"{len(item.dimensions) + len(item.time_dimensions)} dimensions"
        )


@cube.command("describe")
@click.argument("name")
@click.option("--path", type=click.Path(path_type=Path), default=None)
def describe(name: str, path: Optional[Path]) -> None:
    """Show a cube's measures and dimensions."""
    _, _, manifest = _manifest(path)
    item = manifest.cube(name)
    if item is None:
        raise click.ClickException(
            f"No cube named {name!r}. Known: "
            + (", ".join(c.name for c in manifest.cubes) or "none")
        )

    click.secho(f"{item.name} (over {item.base_object})", bold=True)
    if item.description:
        click.echo(f"  {item.description}")

    if item.measures:
        click.echo("\nMeasures:")
        for measure in item.measures:
            suffix = f"  -- {measure.description}" if measure.description else ""
            click.echo(f"  {measure.name:<22} {measure.expression}{suffix}")

    if item.dimensions:
        click.echo("\nDimensions:")
        for dimension in item.dimensions:
            click.echo(f"  {dimension.name:<22} {dimension.expression}")

    if item.time_dimensions:
        click.echo("\nTime dimensions (use --granularity):")
        for dimension in item.time_dimensions:
            click.echo(f"  {dimension.name:<22} {dimension.expression}")


@cube.command("query")
@click.argument("name")
@click.option("--measure", "measures", multiple=True, help="Measure to compute. Repeatable.")
@click.option("--dimension", "dimensions", multiple=True, help="Group by. Repeatable.")
@click.option("--time-dimension", default=None, help="Time column to bucket on.")
@click.option(
    "--granularity",
    type=click.Choice(GRANULARITIES),
    default=None,
    help="Bucket size for --time-dimension.",
)
@click.option("--filter", "filters", multiple=True, help="SQL predicate. Repeatable.")
@click.option("--limit", default=100, show_default=True)
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.option("--sql-only", is_flag=True, help="Print the SQL instead of running it.")
def query_cube(
    name: str,
    measures: tuple,
    dimensions: tuple,
    time_dimension: Optional[str],
    granularity: Optional[str],
    filters: tuple,
    limit: int,
    path: Optional[Path],
    sql_only: bool,
) -> None:
    """Compute measures, grouped by dimensions.

    Example:

        vanna cube query order_metrics --measure revenue --dimension region
    """
    project, profile, manifest = _manifest(path)
    item = manifest.cube(name)
    if item is None:
        raise click.ClickException(f"No cube named {name!r}.")

    _reject_unknown(item, measures, dimensions, time_dimension)

    if time_dimension and not granularity:
        # Bucketing is the whole reason to name a time dimension; without one
        # the result is one row per timestamp, which is never what was wanted.
        raise click.ClickException(
            "--time-dimension needs --granularity (" + ", ".join(GRANULARITIES) + ")."
        )

    dialect = project.config.dialect
    selected: List[str] = []
    grouping: List[str] = []

    if time_dimension:
        expression = item.dimension(time_dimension).expression
        bucket = truncate(expression, granularity, dialect)
        selected.append(f"{bucket} AS {time_dimension}")
        grouping.append(bucket)

    for dimension in dimensions:
        expression = item.dimension(dimension).expression
        selected.append(f"{expression} AS {dimension}")
        grouping.append(expression)

    for measure in measures:
        expression = item.measure(measure).expression
        selected.append(f"{expression} AS {measure}")

    statement = f"SELECT {', '.join(selected) or '*'} FROM {item.base_object}"
    if filters:
        statement += " WHERE " + " AND ".join(f"({f})" for f in filters)
    if grouping and measures:
        statement += " GROUP BY " + ", ".join(grouping)
    if grouping:
        statement += " ORDER BY " + ", ".join(grouping)
    statement += f" LIMIT {limit}"

    compiled = compile_sql(
        statement, manifest, dialect=dialect, fanout_guard=project.config.fanout_guard
    )

    for warning in compiled.warnings:
        click.secho(f"warning: {warning.message}", fg="yellow", err=True)

    if sql_only:
        click.echo(compiled.sql)
        return

    runner = build_runner(profile, max_rows=limit)
    frame = asyncio.run(
        runner.run_sql(RunSqlToolArgs(sql=compiled.sql), system_context())
    )
    click.echo(frame.to_string(index=False) if not frame.empty else "No rows.")


def _reject_unknown(item, measures, dimensions, time_dimension) -> None:
    """Name what does exist, rather than only what does not.

    A cube query is a short list of names; getting one wrong is the common
    case, and an error that lists the alternatives usually fixes it in one go.
    """
    for measure in measures:
        if item.measure(measure) is None:
            raise click.ClickException(
                f"{item.name!r} has no measure {measure!r}. Available: "
                + (", ".join(m.name for m in item.measures) or "none")
            )

    available = [d.name for d in (*item.dimensions, *item.time_dimensions)]
    for dimension in (*dimensions, *( [time_dimension] if time_dimension else [] )):
        if item.dimension(dimension) is None:
            raise click.ClickException(
                f"{item.name!r} has no dimension {dimension!r}. Available: "
                + (", ".join(available) or "none")
            )
