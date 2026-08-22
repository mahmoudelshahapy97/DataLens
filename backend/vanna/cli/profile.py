"""``vanna profile`` -- managing connection profiles.

Every command here is built so that no output can contain a credential. That is
not achieved by masking (which a later edit can forget) but by storing
``${VAR}`` placeholders and resolving them only at connect time -- so there is
usually nothing secret in the file to print.

``vanna profile debug`` is the one that earns its keep: it shows where each
value came from, which turns "why is it connecting to the wrong database" from
an afternoon into a glance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ..config import (
    Profile,
    ProfileStore,
    env_search_path,
    load_env,
    looks_like_literal_secret,
    mask,
)
from ..config.env import find_project_root
from ..core.errors import ErrorCode, ErrorPhase, VannaError


def _store(ctx: click.Context) -> ProfileStore:
    return ProfileStore(ctx.obj.get("profiles_path") if ctx.obj else None)


@click.group()
@click.option(
    "--file",
    "profiles_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Profiles file (default: ~/.vanna/profiles.yml).",
)
@click.pass_context
def profile(ctx: click.Context, profiles_path: Optional[Path]) -> None:
    """Manage database connection profiles."""
    ctx.ensure_object(dict)
    ctx.obj["profiles_path"] = profiles_path


@profile.command("list")
@click.pass_context
def list_profiles(ctx: click.Context) -> None:
    """List profiles, marking the active one."""
    store = _store(ctx)
    profiles = store.list()
    if not profiles:
        click.echo("No profiles yet. Add one with `vanna profile add`.")
        return

    active = store.active_name()
    for item in profiles:
        marker = click.style("*", fg="green") if item.name == active else " "
        summary = item.description or item.settings.get("database") or ""
        click.echo(f"{marker} {item.name:<20} {item.dialect:<12} {summary}")


@profile.command("show")
@click.argument("name", required=False)
@click.pass_context
def show_profile(ctx: click.Context, name: Optional[str]) -> None:
    """Show one profile as stored, without resolving placeholders."""
    store = _store(ctx)
    item = store.get(name) if name else store.active()
    if item is None:
        raise VannaError(
            ErrorCode.MISCONFIGURED,
            "No active profile.",
            phase=ErrorPhase.PROFILE_RESOLUTION,
            hint="Pass a name, or select one with `vanna profile switch <name>`.",
        )

    click.echo(f"{click.style(item.name, bold=True)}  ({item.dialect})")
    if item.description:
        click.echo(f"  {item.description}")
    for key, value in sorted(item.settings.items()):
        click.echo(f"  {key:<18} {mask(value)}")

    literal = item.literal_secret_fields()
    if literal:
        click.secho(
            f"  ! {', '.join(literal)} hold literal values that look like "
            "credentials. Prefer ${ENV_VAR} so they never sit in this file.",
            fg="yellow",
        )


@profile.command("add")
@click.argument("name")
@click.option("--dialect", required=True, help="postgres, sqlite, mysql, ...")
@click.option(
    "--set",
    "settings",
    multiple=True,
    metavar="KEY=VALUE",
    help="Setting to store. Use ${ENV_VAR} for anything secret.",
)
@click.option("--description", default="", help="What this connection is.")
@click.option("--activate/--no-activate", default=False, help="Make it active.")
@click.option(
    "--allow-literal-secret",
    is_flag=True,
    help="Store a value that looks like a real credential anyway.",
)
@click.pass_context
def add_profile(
    ctx: click.Context,
    name: str,
    dialect: str,
    settings: tuple,
    description: str,
    activate: bool,
    allow_literal_secret: bool,
) -> None:
    """Add or replace a profile.

    Example:

        vanna profile add prod --dialect postgres \\
          --set dsn='postgresql://vanna:${PGPASSWORD}@db:5432/analytics'
    """
    parsed = {}
    for pair in settings:
        key, sep, value = pair.partition("=")
        if not sep:
            raise VannaError(
                ErrorCode.INVALID_REQUEST,
                f"--set expects KEY=VALUE, got {pair!r}.",
                phase=ErrorPhase.PROFILE_RESOLUTION,
            )
        parsed[key.strip()] = value

    item = Profile(
        name=name, dialect=dialect.lower(), settings=parsed, description=description
    )

    literal = item.literal_secret_fields()
    if literal and not allow_literal_secret:
        raise VannaError(
            ErrorCode.INVALID_REQUEST,
            f"{', '.join(literal)} looks like a real credential.",
            phase=ErrorPhase.PROFILE_RESOLUTION,
            hint=(
                "Store it as ${ENV_VAR} and export the variable, so the secret "
                "never reaches the profiles file. Use --allow-literal-secret to "
                "override."
            ),
        )

    _store(ctx).save(item, activate=activate)
    click.secho(f"Saved profile {name!r}.", fg="green")


@profile.command("rm")
@click.argument("name")
@click.pass_context
def remove_profile(ctx: click.Context, name: str) -> None:
    """Delete a profile."""
    _store(ctx).remove(name)
    click.secho(f"Removed {name!r}.", fg="green")


@profile.command("switch")
@click.argument("name")
@click.pass_context
def switch_profile(ctx: click.Context, name: str) -> None:
    """Make a profile the active one."""
    _store(ctx).set_active(name)
    click.secho(f"Active profile is now {name!r}.", fg="green")


@profile.command("debug")
@click.argument("name", required=False)
@click.pass_context
def debug_profile(ctx: click.Context, name: Optional[str]) -> None:
    """Explain how a profile resolves: which file, which variables, what's missing."""
    store = _store(ctx)
    item = store.get(name) if name else store.active()
    if item is None:
        click.echo("No active profile.")
        return

    click.echo(f"profile      {click.style(item.name, bold=True)} ({item.dialect})")
    click.echo(f"profiles     {store.path}")

    root = find_project_root()
    click.echo(f"project      {root or '(none found)'}")

    click.echo("env files    (nearest first)")
    for path in env_search_path(root):
        state = "found" if path.is_file() else "absent"
        click.echo(f"  {'*' if path.is_file() else ' '} {path}  [{state}]")

    environment = load_env(root)
    click.echo("settings")
    for key, value in sorted(item.settings.items()):
        text = str(value)
        if "${" in text:
            names = [
                n
                for n in __import__("re").findall(r"\$\{([_A-Z][_A-Z0-9]*)\}", text)
            ]
            unresolved = [n for n in names if n not in environment]
            state = (
                click.style(f"missing: {', '.join(unresolved)}", fg="red")
                if unresolved
                else click.style("resolved from environment", fg="green")
            )
            click.echo(f"  {key:<18} {text}  -> {state}")
        else:
            warn = " (literal)" if looks_like_literal_secret(text) else ""
            click.echo(f"  {key:<18} {mask(text)}{warn}")

    try:
        item.resolve(environment)
    except VannaError as exc:
        click.secho(f"\nWould fail to connect: {exc.args[0]}", fg="red")
        if exc.hint:
            click.secho(f"  -> {exc.hint}", fg="yellow")
    else:
        click.secho("\nAll settings resolve.", fg="green")
