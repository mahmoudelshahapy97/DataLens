"""``vanna skills`` -- serving packaged workflow guides to an agent."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ..skills import get_skill, list_skills, read_reference, render_discovery_stub


@click.group()
def skills() -> None:
    """Workflow guides for AI agents driving Vanna."""


@skills.command("list")
def list_command() -> None:
    """List the available guides."""
    found = list_skills()
    if not found:
        click.echo("No skills are packaged with this build.")
        return
    for summary in found:
        click.echo(f"{summary.name:<18} {summary.description}")


@skills.command("get")
@click.argument("name")
@click.option(
    "--full",
    is_flag=True,
    help="Append the guide's reference files. Longer, and complete.",
)
@click.option("--reference", default=None, help="Print one reference file instead.")
def get_command(name: str, full: bool, reference: Optional[str]) -> None:
    """Print a guide, for an agent to follow."""
    if reference:
        click.echo(read_reference(name, reference))
        return
    click.echo(get_skill(name, full=full))


@skills.command("install")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to write it (default: ~/.claude/skills/vanna/SKILL.md).",
)
@click.option("--force", is_flag=True, help="Overwrite an existing stub.")
def install_command(path: Optional[Path], force: bool) -> None:
    """Install the discovery stub into an agent client.

    The stub is ~50 lines and carries no workflow content -- only the phrases
    that should route to Vanna and the commands that fetch the rest. That is the
    point: upgrading Vanna upgrades the instructions, with no cached copy left
    behind to contradict them.
    """
    target = path or (Path.home() / ".claude" / "skills" / "vanna" / "SKILL.md")

    if target.exists() and not force:
        raise click.ClickException(f"{target} already exists. Pass --force to replace it.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_discovery_stub(), encoding="utf-8")

    click.secho(f"Installed the discovery stub at {target}", fg="green")
    click.echo("Your agent can now run `vanna skills list` to find the guides.")
