"""``vanna dashboard`` -- run a saved dashboard and export it as a file.

The export exists because a dashboard is otherwise only reachable by someone
with an account in the workspace, and the person who asked for the number
usually has neither. It produces one HTML file with the figures already in it:
no server, no login, no network.

The file is a **snapshot**. It carries no connection details and cannot refresh
itself, which is the property that makes it safe to send and the reason the
header inside it says when it was taken.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import click

from ._runtime import build_runner, load_context, system_context


@click.group()
def dashboard() -> None:
    """Run and export dashboards."""


def _load(file: Path):
    from ..dashboards import Dashboard

    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{file} is not valid JSON: {exc}")
    try:
        return Dashboard.model_validate(document)
    except Exception as exc:  # noqa: BLE001 - pydantic's message is the useful part
        raise click.ClickException(f"{file} is not a valid dashboard: {exc}")


@dashboard.command("export")
@click.argument("definition", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file. Defaults to a dated name in the current directory.",
)
@click.option("--path", type=click.Path(path_type=Path), default=None, help="Project directory.")
@click.option("--profile", "profile_name", default=None, help="Connection profile.")
@click.option(
    "--as-user",
    default="",
    help="Stamp this address into the file as the person who exported it.",
)
def export(
    definition: Path,
    out: Optional[Path],
    path: Optional[Path],
    profile_name: Optional[str],
    as_user: str,
) -> None:
    """Execute the dashboard in DEFINITION and write a self-contained HTML file.

    DEFINITION is a dashboard JSON document -- what `save_dashboard` stores, and
    what the portal's `/dashboards/{id}` returns.
    """
    from ..dashboards import export_filename, export_html, has_errors, render_dashboard, verify_dashboard

    board = _load(definition)

    issues = verify_dashboard(board)
    if has_errors(issues):
        for issue in issues:
            if issue.severity == "error":
                click.echo(f"  error: {issue}", err=True)
        raise click.ClickException("The dashboard has errors and was not run.")
    for issue in issues:
        if issue.severity != "error":
            click.echo(f"  warning: {issue}", err=True)

    project, profile = load_context(path, profile_name)
    runner = build_runner(profile)
    context = system_context()

    # The CLI has no tool registry, so tiles run through the runner directly.
    # That means **no row- or column-level access control** -- there is no user
    # to apply it for. Said out loud below, because a file produced here is not
    # the same artifact as one exported from the portal.
    from ..dashboards.render import render_dashboard as _render

    async def run():
        class _DirectRegistry:
            """Minimal registry shim: execute run_sql, nothing else."""

            async def execute(self, name, args, context):  # noqa: ANN001
                from ..capabilities.sql_runner import RunSqlToolArgs

                return await runner.run_sql(RunSqlToolArgs(**args), context)

        return await _render(
            board,
            registry=_DirectRegistry(),
            user=getattr(context, "user", None),
            agent_memory=None,
            saved_query_sql={},
        )

    try:
        results = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"Could not run the dashboard: {exc}")

    html = export_html(
        board,
        results,
        exported_by=as_user or "the vanna CLI",
        workspace=(project.name if project and hasattr(project, "name") else ""),
        data_source=getattr(profile, "safe_label", lambda: "")()
        if hasattr(profile, "safe_label")
        else "",
    )

    target = out or Path(export_filename(board))
    target.write_text(html, encoding="utf-8")

    failed = [r for r in results if r.error]
    rows = sum(r.row_count for r in results)
    click.echo(f"Wrote {target} ({len(html):,} bytes)")
    click.echo(f"  {len(results)} tile(s), {rows:,} row(s) embedded")
    if failed:
        click.echo(f"  {len(failed)} tile(s) failed and say so in the file:")
        for r in failed:
            click.echo(f"    {r.tile_id}: {r.error}")
    click.echo(
        "  This file contains the data and no access rules. Exported from the "
        "portal instead, it is filtered to that user."
    )
