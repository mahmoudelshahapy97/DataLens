"""Expose a ``ToolRegistry`` to Pydantic-AI.

    from vanna.integrations.toolkits.pydantic_ai import build_agent

    agent = await build_agent(registry, user, model="anthropic:claude-opus-5")
    answer = await agent.run("How many customers signed up last month?")

As with the LangChain adapter, every tool routes through ``registry.execute``,
so the SQL policy and the caller's access rules apply. Nothing is enforced here.

Requires ``pydantic-ai``. Install with ``pip install 'vanna[pydantic-ai]'``.
"""

from __future__ import annotations

from typing import Any, List, Optional

from .bridge import ToolSpec, system_prompt, tool_specs


def _require_pydantic_ai():
    try:
        from pydantic_ai import Agent
        from pydantic_ai.tools import Tool
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ImportError(
            "Pydantic-AI support needs pydantic-ai. "
            "Install with: pip install 'vanna[pydantic-ai]'"
        ) from exc
    return Agent, Tool


def _as_tool(spec: ToolSpec) -> Any:
    """Wrap one spec as a Pydantic-AI ``Tool``.

    ``takes_ctx=False`` because the callable wants only the model's arguments --
    the identity the call runs as was fixed when the toolkit was built, and is
    not something the model gets to influence per call.
    """
    _, Tool = _require_pydantic_ai()

    return Tool(
        spec.run,
        name=spec.name,
        description=spec.description,
        takes_ctx=False,
    )


async def build_tools(
    registry: Any,
    user: Any,
    *,
    agent_memory: Any = None,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> List[Any]:
    """The tools *user* may call, as Pydantic-AI ``Tool`` objects."""
    specs = await tool_specs(
        registry, user, agent_memory=agent_memory, include=include, exclude=exclude
    )
    return [_as_tool(spec) for spec in specs]


async def build_agent(
    registry: Any,
    user: Any,
    *,
    model: str,
    agent_memory: Any = None,
    dialect: str = "sqlite",
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
    **agent_kwargs: Any,
) -> Any:
    """A ready Pydantic-AI agent over this project's tools.

    Args:
        registry: A ``ToolRegistry``.
        user: The identity every tool call runs as.
        model: A Pydantic-AI model string, e.g. ``"anthropic:claude-opus-5"``.
        dialect: SQL dialect, so the system prompt tells the model the truth
            about what it is writing.
        **agent_kwargs: Passed through to ``pydantic_ai.Agent``.
    """
    Agent, _ = _require_pydantic_ai()

    tools = await build_tools(
        registry, user, agent_memory=agent_memory, include=include, exclude=exclude
    )
    prompt = await system_prompt(registry, user, dialect=dialect)
    return Agent(model, tools=tools, system_prompt=prompt, **agent_kwargs)
