"""``vanna serve`` -- the original single command, now a subcommand.

Deliberately not a reimplementation. The command in
``servers.cli.server_runner`` already handles framework selection, example
loading, dev mode and config files; this module re-labels it so it can hang off
the group. Copying its option list here would guarantee the two drift.
"""

from __future__ import annotations

from ..servers.cli.server_runner import main as _serve_command

# click.Command.name is what the group registers it under. The function is
# still exported as `main` from its own module, so the legacy console-script
# entry point keeps working unchanged.
_serve_command.name = "serve"
_serve_command.help = (
    "Run the Vanna web server.\n\n"
    "Accepts the options `vanna` itself used to take, so existing commands and "
    "scripts continue to work."
)

serve = _serve_command


def _attach_subcommands() -> None:
    """Give ``serve`` subcommands without breaking ``vanna serve --port 9000``.

    ``serve`` is a plain command, not a group, because it has to keep accepting
    the flags ``vanna`` itself used to take. A group would reject them. So MCP
    is registered on the top-level group as ``vanna mcp`` and aliased in help
    text -- the alternative, converting ``serve`` to a group with an implicit
    default command, breaks exactly the invocations the shim exists to protect.
    """


__all__ = ["serve"]
