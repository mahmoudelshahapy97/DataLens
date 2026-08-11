"""Exposing Vanna's tools over the Model Context Protocol.

The whole design is one sentence: **enumerate the tool registry and execute
through it**.

That is not laziness, it is the only version that is correct. Every guarantee
Vanna has -- the SQL policy, semantic compilation, row-level and column-level
rules -- lives in ``ToolRegistry.transform_args``. Hand-writing an MCP tool per
capability, as the obvious implementation does, means each one either
re-implements those checks or skips them, and the one that skips them is the
breach. Going through ``execute`` means a tool a deployment registers next year
is exposed, and access-controlled, without anyone touching this file.

Identity is the genuinely hard part over stdio, where there is no session. See
:func:`build_server`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ...core.errors import ErrorCode, ErrorPhase, VannaError

logger = logging.getLogger(__name__)

#: Result payloads above this are truncated before being sent. An MCP client
#: puts tool output straight into a model's context, so an unbounded result is a
#: context-window failure rather than a large response.
MAX_RESULT_CHARS = 100_000


def _require_mcp():
    try:
        import mcp.server  # noqa: F401
        import mcp.types  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise VannaError(
            ErrorCode.DEPENDENCY_MISSING,
            "The MCP server needs the 'mcp' package.",
            phase=ErrorPhase.CONFIGURATION,
            hint="pip install 'vanna[mcp]'",
            cause=exc,
        )


class VannaMcpServer:
    """Serves one agent's tool registry over MCP.

    Args:
        agent: A configured ``Agent``. Its registry, resolver and memory are the
            same ones the HTTP server uses -- that shared identity is what makes
            "the same question returns the same rows over both transports" true
            rather than aspirational.
        user: The identity every call runs as. Required: see the note in
            :func:`build_server` about why this cannot default to an admin.
        allow_write: Permit a non-read-only SQL policy. Off by default.
    """

    def __init__(self, agent: Any, *, user: Any, allow_write: bool = False) -> None:
        self.agent = agent
        self.user = user
        self.allow_write = allow_write
        self.registry = agent.tool_registry

        self._check_write_policy()

    def _check_write_policy(self) -> None:
        """Refuse to start read-write unless it was asked for.

        A registry configured for writes, exposed over a protocol whose whole
        purpose is letting a model call it, is a decision someone should have
        made deliberately.
        """
        policy = getattr(self.registry, "policy", None)
        mode = getattr(policy, "mode", "read_only")
        if mode != "read_only" and not self.allow_write:
            raise VannaError(
                ErrorCode.MISCONFIGURED,
                "The tool registry allows writes but --allow-write was not passed.",
                phase=ErrorPhase.CONFIGURATION,
                hint="Pass --allow-write if that is intended, or use a read-only policy.",
            )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Every tool this user may call, in MCP's shape.

        Group membership is applied by ``get_schemas``, so a viewer sees fewer
        tools than an analyst without this method knowing what a viewer is.
        """
        schemas = await self.registry.get_schemas(self.user)
        return [
            {
                "name": schema.name,
                "description": schema.description,
                "inputSchema": schema.parameters,
            }
            for schema in schemas
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one tool through the registry.

        Everything of consequence happens inside ``registry.execute``:
        permission check, argument validation, semantic compilation, row and
        column rules, the SQL policy, then the tool. This method only shapes the
        result.
        """
        import uuid

        from ...core.tool import ToolCall, ToolContext

        context = ToolContext(
            user=self.user,
            conversation_id="mcp",
            request_id=str(uuid.uuid4()),
            tenant_id=self.user.tenant_id,
            agent_memory=self.agent.agent_memory,
        )

        try:
            result = await self.registry.execute(
                ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments),
                context,
            )
        except VannaError as exc:
            return _error_payload(exc)
        except Exception as exc:
            logger.exception("MCP tool %s failed", name)
            return _error_payload(
                VannaError.from_exception(exc, phase=ErrorPhase.TOOL_EXECUTION)
            )

        text = result.result_for_llm or ""
        truncated = len(text) > MAX_RESULT_CHARS
        if truncated:
            text = text[:MAX_RESULT_CHARS] + "\n\n[truncated]"

        payload: Dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": not result.success,
        }

        # Warnings the compiler raised -- a fan-out caveat, say -- must travel
        # with the answer. A warning that reaches only a log is a warning nobody
        # acts on.
        warnings = (context.metadata or {}).get("semantic_warnings")
        if warnings:
            payload["content"].append(
                {"type": "text", "text": "Caveats:\n" + "\n".join(warnings)}
            )

        if truncated:
            payload["content"].append(
                {"type": "text", "text": "Result truncated; narrow the query."}
            )
        return payload

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    async def list_resources(self) -> List[Dict[str, str]]:
        """Read-only context an agent can pull without calling a tool."""
        resources = [
            {
                "uri": "vanna://catalog/tables",
                "name": "Schema catalog",
                "description": "Tables and columns the agent can query.",
                "mimeType": "text/plain",
            }
        ]

        if getattr(self.registry, "manifest", None) is not None:
            resources.append(
                {
                    "uri": "vanna://semantic/manifest",
                    "name": "Semantic layer",
                    "description": "Models, calculated columns, metrics and joins.",
                    "mimeType": "text/plain",
                }
            )

        from ...skills import list_skills

        for skill in list_skills():
            resources.append(
                {
                    "uri": f"vanna://skills/{skill.name}",
                    "name": f"Skill: {skill.name}",
                    "description": skill.description,
                    "mimeType": "text/markdown",
                }
            )
        return resources

    async def read_resource(self, uri: str) -> str:
        import uuid

        from ...core.tool import ToolContext

        if uri.startswith("vanna://skills/"):
            from ...skills import get_skill

            return get_skill(uri.removeprefix("vanna://skills/"))

        context = ToolContext(
            user=self.user,
            conversation_id="mcp",
            request_id=str(uuid.uuid4()),
            tenant_id=self.user.tenant_id,
            agent_memory=self.agent.agent_memory,
        )

        if uri == "vanna://semantic/manifest":
            manifest = getattr(self.registry, "manifest", None)
            if manifest is None:
                return "No semantic layer is configured."
            from ...semantic import describe_manifest

            return describe_manifest(manifest)

        if uri == "vanna://catalog/tables":
            catalog = getattr(self.registry, "catalog", None)
            if catalog is None:
                return "No catalog is configured."
            from ...capabilities.schema_catalog.describe import describe_schema

            return describe_schema(
                await catalog.get_tables(context),
                await catalog.get_relationships(context),
            )

        raise VannaError(
            ErrorCode.OBJECT_NOT_FOUND,
            f"Unknown resource {uri!r}.",
            phase=ErrorPhase.SKILL_DELIVERY,
        )


def _error_payload(exc: VannaError) -> Dict[str, Any]:
    """A failure, in the one shape every transport uses."""
    return {
        "content": [{"type": "text", "text": json.dumps(exc.to_dict(), indent=2)}],
        "isError": True,
    }
