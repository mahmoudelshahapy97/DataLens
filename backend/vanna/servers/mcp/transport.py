"""Binding :class:`VannaMcpServer` to an MCP transport.

Kept apart from the server so the server can be exercised without the ``mcp``
package installed -- and so the identity decision below sits in one place
instead of being repeated per transport.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ...core.errors import ErrorCode, ErrorPhase, VannaError
from .server import VannaMcpServer, _require_mcp

logger = logging.getLogger(__name__)


async def resolve_identity(
    agent: Any, *, email: Optional[str], tenant: Optional[str]
):
    """Work out who an stdio session is.

    There is no HTTP session here, so identity comes from the command line and
    is checked against the same ``UserResolver`` the web server uses.

    **This refuses to start anonymous.** An MCP server is a thing an agent
    calls unattended; starting one with no identity would mean either denying
    everything (useless) or granting everything (a hole that stays open for as
    long as the process runs). Requiring the operator to name a user makes the
    blast radius a decision rather than an accident.
    """
    if not email:
        raise VannaError(
            ErrorCode.MISCONFIGURED,
            "An MCP session must run as a named user.",
            phase=ErrorPhase.USER_RESOLUTION,
            hint="Pass --user you@example.com, or set VANNA_MCP_USER.",
        )

    from ...core.user import RequestContext

    headers = {"X-User-Email": email}
    if tenant:
        headers["X-Tenant-Id"] = tenant

    resolver = getattr(agent, "user_resolver", None)
    if resolver is None:
        raise VannaError(
            ErrorCode.MISCONFIGURED,
            "The agent has no user resolver, so identity cannot be established.",
            phase=ErrorPhase.USER_RESOLUTION,
        )

    try:
        user = await resolver.resolve_user(
            RequestContext(headers=headers, cookies={}, metadata={})
        )
    except PermissionError as exc:
        # The resolver rejected them -- membership revoked, wrong tenant. Say so
        # now rather than at the first tool call.
        raise VannaError(
            ErrorCode.PERMISSION_DENIED,
            str(exc),
            phase=ErrorPhase.USER_RESOLUTION,
        )

    logger.info(
        "MCP session as %s (tenant=%s, groups=%s)",
        user.id,
        user.tenant_id,
        ",".join(user.group_memberships or []) or "-",
    )
    return user


async def serve_stdio(
    agent: Any,
    *,
    email: Optional[str] = None,
    tenant: Optional[str] = None,
    allow_write: bool = False,
) -> None:
    """Run the MCP server over stdio until the client disconnects."""
    _require_mcp()

    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    user = await resolve_identity(agent, email=email, tenant=tenant)
    vanna = VannaMcpServer(agent, user=user, allow_write=allow_write)

    server = Server("vanna")

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            types.Tool(
                name=tool["name"],
                description=tool["description"],
                inputSchema=tool["inputSchema"],
            )
            for tool in await vanna.list_tools()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list:
        payload = await vanna.call_tool(name, arguments or {})
        return [
            types.TextContent(type="text", text=item["text"])
            for item in payload["content"]
        ]

    @server.list_resources()
    async def _list_resources() -> list:
        return [
            types.Resource(
                uri=resource["uri"],
                name=resource["name"],
                description=resource["description"],
                mimeType=resource["mimeType"],
            )
            for resource in await vanna.list_resources()
        ]

    @server.read_resource()
    async def _read_resource(uri: str) -> str:
        return await vanna.read_resource(str(uri))

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )
