"""Vanna over the Model Context Protocol.

    vanna serve mcp --user you@example.com

Tools are enumerated from the agent's registry and executed through it, so the
SQL policy, semantic compilation and access rules apply on this transport
exactly as they do over HTTP -- without this package restating any of them.
"""

from .server import MAX_RESULT_CHARS, VannaMcpServer

__all__ = ["VannaMcpServer", "MAX_RESULT_CHARS", "serve_stdio", "resolve_identity"]


def __getattr__(name: str):
    # Deferred: transport imports the optional `mcp` package, and
    # `from vanna.servers.mcp import VannaMcpServer` must work without it.
    if name in ("serve_stdio", "resolve_identity"):
        from . import transport

        return getattr(transport, name)
    raise AttributeError(name)
