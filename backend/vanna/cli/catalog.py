"""``vanna catalog`` -- scanning a database into the catalog.

The catalog is what the model is shown. Without it the agent has no schema and
invents table names, which is why the onboarding skill refuses to ask a question
before this has run -- and why it needed to be a command rather than a library
call buried in a server's startup path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import click

from ..capabilities.schema_catalog import SchemaScanner
from ..integrations.local import LocalSchemaCatalog
from ._runtime import build_runner, load_context, system_context


@click.group()
def catalog() -> None:
    """Scan and inspect the schema catalog."""


@catalog.command("scan")
@click.option("--path", type=click.Path(path_type=Path), default=None, help="Project directory.")
@click.option("--profile", "profile_name", default=None, help="Connection profile.")
@click.option(
    "--sample-rows",
    is_flag=True,
    help="Also capture example values for high-cardinality columns.",
)
def scan(path: Optional[Path], profile_name: Optional[str], sample_rows: bool) -> None:
    """Scan the database into target/catalog.json.

    Records structure *and* the real values of low-cardinality columns, which is
    what stops the agent guessing a filter literal like 'cancelled' when the
    data says 'CANCELLED'. Columns whose names look sensitive are skipped
    entirely -- their values never enter the catalog and therefore never enter a
    prompt.
    """
    project, profile = load_context(path, profile_name=profile_name)
    if project is None:
        raise click.ClickException(
            "No project found. Run `vanna project init <name>` first."
        )

    runner = build_runner(profile)
    dialect = getattr(runner, "dialect", profile.dialect)
    store = LocalSchemaCatalog(str(project.paths.catalog_file))
    context = system_context()

    click.echo(f"Scanning {profile.name} ({dialect}) ...")
    report = asyncio.run(
        SchemaScanner(runner, dialect=dialect, sample_rows=sample_rows).scan(
            context, store
        )
    )

    click.secho(report.summary(), fg="green")
    for error in report.errors:
        click.secho(f"  {error}", fg="yellow")

    click.echo(f"\nWrote {project.paths.catalog_file}")
    click.echo("Next: vanna project from-catalog && vanna project build")


@catalog.command("show")
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.argument("table", required=False)
def show(path: Optional[Path], table: Optional[str]) -> None:
    """List scanned tables, or one table's columns."""
    project, _ = load_context(path)
    if project is None or not project.paths.catalog_file.is_file():
        raise click.ClickException("No catalog yet. Run `vanna catalog scan`.")

    raw = json.loads(project.paths.catalog_file.read_text(encoding="utf-8"))
    tables = raw.get("tables", [])

    if not table:
        click.echo(f"{len(tables)} table(s):")
        for entry in sorted(tables, key=lambda t: t.get("table_name", "")):
            click.echo(
                f"  {entry.get('table_name'):<28} {len(entry.get('columns') or [])} columns"
            )
        return

    match = next(
        (t for t in tables if (t.get("table_name") or "").lower() == table.lower()), None
    )
    if match is None:
        raise click.ClickException(f"No table named {table!r} in the catalog.")

    click.secho(match["table_name"], bold=True)
    for column in match.get("columns") or []:
        line = f"  {column['name']:<26} {column.get('data_type', '?')}"
        if column.get("is_primary_key"):
            line += "  [pk]"
        # The profiled values are the reason to read this at all.
        if column.get("categories"):
            line += "  values: " + ", ".join(str(v) for v in column["categories"][:8])
        click.echo(line)
