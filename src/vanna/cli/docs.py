"""``vanna docs`` -- reference material the CLI can answer without a browser.

Right now that is one thing: what each database needs in order to be connected
to. It is generated from the same registry the admin console's form and the URL
builder read (``vanna.core.datasource``), so a field cannot be documented here
and missing there.

Aimed as much at an agent as at a person. "Which fields does Snowflake need?" is
exactly the question an agent asks before it can fill in a connection, and the
answer being one command away is the difference between it succeeding and it
guessing.
"""

from __future__ import annotations

import json

import click

from ..core.datasource import ENGINES, all_engines, describe, get_engine


@click.group()
def docs() -> None:
    """Reference documentation."""


@docs.command("connection-info")
@click.argument("engine_name", metavar="[ENGINE]", required=False)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def connection_info(engine_name: str, as_json: bool) -> None:
    """Fields needed to connect to ENGINE. Omit ENGINE to list them all."""
    if not engine_name:
        if as_json:
            click.echo(json.dumps({"engines": all_engines()}, indent=2))
            return
        click.echo("Supported engines:\n")
        width = max(len(e.name) for e in ENGINES.values())
        for engine in ENGINES.values():
            port = f":{engine.default_port}" if engine.default_port else ""
            click.echo(f"  {engine.name:<{width}}  {engine.label} ({engine.scheme}{port})")
        click.echo("\nRun 'vanna docs connection-info <engine>' for its fields.")
        return

    engine = get_engine(engine_name)
    if engine is None:
        known = ", ".join(sorted(ENGINES))
        raise click.ClickException(f"Unknown engine {engine_name!r}. Known: {known}")

    if as_json:
        click.echo(json.dumps(describe(engine), indent=2))
        return

    click.echo(f"{engine.label}  ({engine.scheme}://)")
    if engine.default_port:
        click.echo(f"Default port: {engine.default_port}")
    click.echo("")

    def show(title: str, fields: list) -> None:
        if not fields:
            return
        click.echo(f"{title}:")
        width = max(len(f.name) for f in fields)
        for f in fields:
            default = f"  [default: {f.default}]" if f.default else ""
            click.echo(f"  {f.name:<{width}}  {f.label}{default}")
            if f.help:
                click.echo(f"  {'':<{width}}  {f.help}")
        click.echo("")

    show("Required", engine.required_fields())
    show("Optional", engine.optional_fields())

    if engine.notes:
        click.echo(f"Note: {engine.notes}")
