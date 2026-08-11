"""``vanna query`` -- run SQL through the policy, the compiler and the runner.

Not a database shell. Everything typed here goes through the same SQL policy the
agent's tools do, and through the semantic compiler when the project has a
manifest -- so ``SELECT revenue FROM orders`` works, and ``DROP TABLE`` does not.

That is the point of it existing rather than telling people to use ``psql``: it
is the command the packaged skills tell an agent to run, and an agent running
raw SQL against the warehouse with no policy is the thing this project spends
most of its code preventing.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
from pathlib import Path
from typing import Optional

import click

from ..capabilities.sql_runner import RunSqlToolArgs
from ..core.errors import ErrorCode, ErrorPhase, VannaError
from ..core.sql_policy import (
    SqlPolicy,
    SqlPolicyError,
    SqlPolicyValidator,
    apply_row_limit,
)
from ._runtime import build_runner, load_context, system_context


@click.command()
@click.option("--sql", required=True, help="SQL to run. Semantic SQL when a manifest exists.")
@click.option("--path", type=click.Path(path_type=Path), default=None, help="Project directory.")
@click.option("--profile", "profile_name", default=None, help="Connection profile.")
@click.option("--limit", default=100, show_default=True, help="Maximum rows.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "csv", "json"]),
    default="table",
    show_default=True,
)
@click.option(
    "--explain",
    is_flag=True,
    help="Print the compiled SQL and stop, without running it.",
)
def query(
    sql: str,
    path: Optional[Path],
    profile_name: Optional[str],
    limit: int,
    output_format: str,
    explain: bool,
) -> None:
    """Run a query and print the result."""
    project, profile = load_context(path, profile_name=profile_name)
    dialect = project.config.dialect if project else profile.dialect

    statement = sql.strip().rstrip(";")
    warnings: list = []

    # -- compile, when there is a semantic layer ------------------------
    manifest = None
    if project is not None:
        from ..semantic import load_built_manifest

        manifest = load_built_manifest(project.paths)

    if manifest is not None and manifest.models:
        from ..semantic.compiler import compile_sql

        compiled = compile_sql(
            statement,
            manifest,
            dialect=dialect,
            fanout_guard=project.config.fanout_guard,
        )
        statement = compiled.sql
        warnings = [w.message for w in compiled.warnings]

    # -- policy, always -------------------------------------------------
    #
    # After compilation, so `require_catalog_tables` sees real tables and the
    # function rules apply to what will actually execute.
    policy = SqlPolicy(default_limit=limit)
    try:
        SqlPolicyValidator().validate_or_raise(statement, dialect=dialect, policy=policy)
    except SqlPolicyError as exc:
        error = exc.to_vanna_error()
        click.secho(f"Rejected: {error.args[0]}", fg="red", err=True)
        raise SystemExit(1)

    limited = apply_row_limit(statement, limit, dialect)

    if explain:
        click.echo(limited)
        for warning in warnings:
            click.secho(f"warning: {warning}", fg="yellow", err=True)
        return

    runner = build_runner(profile, max_rows=limit)
    frame = asyncio.run(
        runner.run_sql(RunSqlToolArgs(sql=limited), system_context())
    )

    for warning in warnings:
        click.secho(f"warning: {warning}", fg="yellow", err=True)

    _emit(frame, output_format)

    if frame.attrs.get("truncated"):
        click.secho(
            f"Showing {len(frame)} rows; more matched. Raise --limit or filter.",
            fg="yellow",
            err=True,
        )


def _emit(frame, output_format: str) -> None:
    """Write results to stdout.

    Warnings and notices go to stderr throughout, so ``vanna query --format csv``
    can be piped into a file without a caveat line corrupting the data.
    """
    if frame.empty:
        click.secho("No rows.", fg="yellow", err=True)
        return

    if output_format == "json":
        click.echo(frame.to_json(orient="records", indent=2))
        return

    if output_format == "csv":
        buffer = io.StringIO()
        frame.to_csv(buffer, index=False, quoting=csv.QUOTE_MINIMAL)
        click.echo(buffer.getvalue().rstrip("\n"))
        return

    click.echo(frame.to_string(index=False, max_rows=None))
