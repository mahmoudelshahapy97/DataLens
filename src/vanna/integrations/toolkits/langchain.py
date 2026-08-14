"""Expose a ``ToolRegistry`` to LangChain and LangGraph.

    from vanna.integrations.toolkits.langchain import VannaToolkit

    toolkit = await VannaToolkit.create(registry, user, dialect="postgres")
    agent = create_agent(
        model="anthropic:claude-opus-5",
        tools=toolkit.get_tools(),
        system_prompt=toolkit.system_prompt,
    )

Every tool routes through ``registry.execute``, so the SQL policy, semantic
compilation and the caller's row- and column-level rules apply exactly as they
do in this project's own agent. The wrapper enforces nothing itself, which is
the point: there is one place where "may this user run this" is decided.

Requires ``langchain-core``. Install with ``pip install 'vanna[langchain]'``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .bridge import ToolSpec, system_prompt, tool_specs


def _require_langchain():
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ImportError(
            "LangChain support needs langchain-core. "
            "Install with: pip install 'vanna[langchain]'"
        ) from exc
    return StructuredTool


def _as_langchain_tool(spec: ToolSpec) -> Any:
    """Wrap one spec as a LangChain ``StructuredTool``.

    The JSON Schema goes across as ``args_schema`` rather than being converted
    into a pydantic model here: LangChain accepts a schema dict, and rebuilding
    the model would be a second definition of the arguments that could disagree
    with the one the registry validates against.
    """
    StructuredTool = _require_langchain()

    return StructuredTool(
        name=spec.name,
        description=spec.description,
        args_schema=spec.parameters,
        coroutine=spec.run,
        # No sync `func`. The registry is async all the way down, and offering a
        # blocking entry point would either deadlock inside a running loop or
        # quietly spawn a second one.
        func=None,
    )


class VannaToolkit:
    """This project's tools, as LangChain tools."""

    def __init__(self, specs: List[ToolSpec], prompt: str) -> None:
        self._specs = specs
        self.system_prompt = prompt

    @classmethod
    async def create(
        cls,
        registry: Any,
        user: Any,
        *,
        agent_memory: Any = None,
        dialect: str = "sqlite",
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
    ) -> "VannaToolkit":
        """Build a toolkit for *user*.

        Async because deciding which tools this user may see is a registry
        question, and the registry is async.
        """
        specs = await tool_specs(
            registry,
            user,
            agent_memory=agent_memory,
            include=include,
            exclude=exclude,
        )
        prompt = await system_prompt(registry, user, dialect=dialect)
        return cls(specs, prompt)

    def get_tools(self) -> List[Any]:
        """The LangChain tool objects."""
        return [_as_langchain_tool(spec) for spec in self._specs]

    def tool_names(self) -> List[str]:
        return [spec.name for spec in self._specs]

    def __len__(self) -> int:
        return len(self._specs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<VannaToolkit {len(self._specs)} tool(s): {', '.join(self.tool_names())}>"
