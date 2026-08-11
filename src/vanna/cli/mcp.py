"""``vanna serve mcp`` -- expose this project's tools to an MCP client."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import click

from ..core.errors import ErrorCode, ErrorPhase, VannaError


@click.command("mcp")
@click.option(
    "--user",
    "email",
    default=lambda: os.getenv("VANNA_MCP_USER", ""),
    help="Identity every call runs as. Required -- there is no session on stdio.",
)
@click.option("--tenant", default=None, help="Tenant to act in.")
@click.option(
    "--path", type=click.Path(path_type=Path), default=None, help="Project directory."
)
@click.option(
    "--example",
    default=None,
    help="Load a bundled example agent instead of building one from the project.",
)
@click.option(
    "--allow-write",
    is_flag=True,
    help="Permit a read-write SQL policy. Off by default, deliberately.",
)
def mcp(
    email: str,
    tenant: Optional[str],
    path: Optional[Path],
    example: Optional[str],
    allow_write: bool,
) -> None:
    """Serve Vanna's tools over MCP on stdio.

    Point an MCP client at this command and the agent gets every tool the named
    user may call -- with the SQL policy, semantic compilation and row/column
    rules applied, because execution goes through the same registry the web
    server uses.

    Example client configuration:

        {"command": "vanna", "args": ["serve", "mcp", "--user", "you@example.com"]}
    """
    if not email:
        raise VannaError(
            ErrorCode.MISCONFIGURED,
            "An MCP session must run as a named user.",
            phase=ErrorPhase.USER_RESOLUTION,
            hint="Pass --user you@example.com, or set VANNA_MCP_USER.",
        )

    agent = _load_agent(example)

    from ..servers.mcp import serve_stdio

    asyncio.run(
        serve_stdio(agent, email=email, tenant=tenant, allow_write=allow_write)
    )


def _load_agent(example: Optional[str]):
    """Build the agent to serve.

    Only the bundled examples are wired up here. Assembling an agent from a
    project needs an LLM service, a runner factory, and the knowledge stores --
    which ``docker/app.py`` already does, and duplicating it in the CLI would
    give two assemblies that drift. Until that is factored out of the reference
    stack, this says so plainly rather than half-building one.
    """
    if not example:
        raise VannaError(
            ErrorCode.NOT_IMPLEMENTED,
            "Building an agent from a project is not wired into the CLI yet.",
            phase=ErrorPhase.CONFIGURATION,
            hint=(
                "Pass --example mock_sqlite_example to try the transport, or "
                "mount the MCP server in your own assembly:\n"
                "    from vanna.servers.mcp import serve_stdio"
            ),
        )

    from ..servers.cli.server_runner import ExampleAgentLoader

    return ExampleAgentLoader.load_example_agent(example)
