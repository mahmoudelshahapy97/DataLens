"""The ``vanna`` command group.

Until now ``vanna`` was a single command that booted a demo server, and its
options are documented in the README and burned into people's shell history.
Turning it into a group would ordinarily break every one of those invocations,
so :class:`CompatGroup` routes them to ``vanna serve`` instead, with a warning.

That compatibility shim is the only clever thing in this module. Everything
else is registration.
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional, Tuple

import click

from ..core.errors import VannaError

logger = logging.getLogger(__name__)


class CompatGroup(click.Group):
    """A group that still answers to the old single-command interface.

    ``vanna --framework fastapi --example mock_quickstart`` used to start a
    server. It still does. Anything whose first token is an option, or is not a
    known subcommand, is handed to ``serve``.
    """

    #: Options that belong to the group itself and must not be re-routed.
    _GROUP_OPTIONS = {"--help", "-h", "--version", "-V"}

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        if args and args[0] not in self._GROUP_OPTIONS:
            first = args[0]
            is_option = first.startswith("-")
            is_known = first in self.commands
            if is_option or not is_known:
                if is_option:
                    click.echo(
                        "Note: bare options now mean `vanna serve`. Use "
                        "`vanna serve ...` explicitly; this shim will be "
                        "removed in a future release.",
                        err=True,
                    )
                    args = ["serve", *args]
                else:
                    # An unknown word: let click produce its own "No such
                    # command" error, which lists the real ones. Rewriting it
                    # to `serve` here would turn a typo into a server start.
                    pass
        return super().parse_args(ctx, args)


def _format_error(exc: VannaError) -> str:
    """Render a VannaError for a terminal: what broke, then what to do."""
    lines = [click.style(f"Error [{exc.code.value}]", fg="red", bold=True)]
    lines.append(f"  {exc.args[0] if exc.args else exc}")
    if exc.hint:
        lines.append(click.style(f"  -> {exc.hint}", fg="yellow"))
    return "\n".join(lines)


class VannaCli(CompatGroup):
    """Renders :class:`VannaError` as a message instead of a traceback.

    A stack trace is the right output for a bug and the wrong output for "you
    have not set PGPASSWORD" -- it buries the one line that matters under
    twenty that do not. Unexpected exceptions still raise normally.
    """

    def invoke(self, ctx: click.Context):
        try:
            return super().invoke(ctx)
        except VannaError as exc:
            click.echo(_format_error(exc), err=True)
            logger.debug("VannaError detail: %s", exc.to_dict(redact=False))
            ctx.exit(1)


@click.group(cls=VannaCli, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="vanna", prog_name="vanna")
@click.option("--debug", is_flag=True, help="Verbose logging.")
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
    """Vanna -- natural-language querying over your database.

    Start with `vanna project init` and `vanna profile add`.
    """
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


def _register() -> None:
    """Attach subcommands.

    Imported lazily inside the function so that a broken optional dependency in
    one command group cannot stop the whole CLI from starting -- `vanna --help`
    must work even when, say, the eval extra is half-installed.
    """
    from . import ask as ask_cmds
    from . import catalog as catalog_cmds
    from . import cube as cube_cmds
    from . import mcp as mcp_cmds
    from . import profile as profile_cmds
    from . import project as project_cmds
    from . import query as query_cmds
    from . import serve as serve_cmds
    from . import skills as skills_cmds

    cli.add_command(serve_cmds.serve)
    cli.add_command(profile_cmds.profile)
    cli.add_command(project_cmds.project)
    cli.add_command(catalog_cmds.catalog)
    cli.add_command(query_cmds.query)
    cli.add_command(cube_cmds.cube)
    cli.add_command(skills_cmds.skills)
    cli.add_command(ask_cmds.ask)
    # Top-level rather than under `serve`: `serve` must stay a plain command so
    # it keeps accepting the flags the old single-command `vanna` took, and a
    # click group cannot do that.
    cli.add_command(mcp_cmds.mcp)


_register()


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Returns a process exit code."""
    return cli.main(args=argv if argv is not None else sys.argv[1:], standalone_mode=True)


if __name__ == "__main__":  # pragma: no cover
    main()
