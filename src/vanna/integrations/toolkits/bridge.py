"""Turn a ``ToolRegistry`` into plain callables an agent framework can adopt.

Shared by the LangChain and Pydantic-AI adapters. Neither reimplements a tool:
the registry is where the SQL policy, semantic compilation, row- and
column-level access control and the audit trail live, so a toolkit that reached
past it would be a second route to the database with none of the guarantees. It
would also be the route nobody remembers to update.

So the adapters are thin on purpose. Each one takes what :func:`tool_specs`
returns -- a name, a description, a JSON Schema and an async callable -- and
wraps it in whatever object its framework expects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class ToolSpec:
    """One registry tool, in framework-neutral terms."""

    name: str
    description: str
    parameters: Dict[str, Any]
    """JSON Schema for the arguments."""
    run: Callable[..., Awaitable[str]]
    """Async callable taking keyword arguments and returning text for the model."""


def _context_factory(user: Any, agent_memory: Any = None) -> Callable[[], Any]:
    """Build the ``ToolContext`` each execution needs.

    A fresh context per call: it carries the request id the audit trail
    correlates on, so reusing one would attribute an agent's whole run to its
    first tool call.

    ``ToolContext`` requires a real ``AgentMemory`` -- it is typed, not optional
    -- so a scratch one is supplied when the caller has none. That memory is
    per-toolkit and disappears with the process, which is the right default for
    an embedded agent: silently persisting one application's memories into
    another's store would be worse than not remembering.
    """
    if agent_memory is None:
        from ..local.agent_memory.in_memory import DemoAgentMemory

        agent_memory = DemoAgentMemory()

    def make() -> Any:
        import uuid

        from ...core.tool import ToolContext

        return ToolContext(
            user=user,
            agent_memory=agent_memory,
            conversation_id="",
            request_id=str(uuid.uuid4()),
        )

    return make


async def tool_specs(
    registry: Any,
    user: Any,
    *,
    agent_memory: Any = None,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> List[ToolSpec]:
    """Describe the tools *user* is allowed to call, ready to be wrapped.

    Async because the registry's schema lookup is: it consults the user's groups
    to decide what they may see. Doing that behind a synchronous call would
    deadlock inside any framework already running an event loop, which is all of
    them.

    Args:
        registry: A ``ToolRegistry``.
        user: The ``User`` every call runs as. Their groups decide which tools
            appear **and** what each may do -- an analyst driving this toolkit
            still cannot write, because the registry checks, not this wrapper.
        agent_memory: Passed through to the tool context.
        include: Only these tool names, if given.
        exclude: Never these tool names.
    """
    from ...core.tool import ToolCall

    make_context = _context_factory(user, agent_memory)
    schemas = await registry.get_schemas(user)

    specs: List[ToolSpec] = []
    for schema in schemas:
        if include and schema.name not in include:
            continue
        if exclude and schema.name in exclude:
            continue

        def make_runner(tool_name: str) -> Callable[..., Awaitable[str]]:
            async def run(**kwargs: Any) -> str:
                import uuid

                result = await registry.execute(
                    # A fresh id per call: it is what the audit trail correlates
                    # on, so reusing one would merge separate calls into one
                    # entry.
                    ToolCall(
                        id=str(uuid.uuid4()), name=tool_name, arguments=kwargs
                    ),
                    make_context(),
                )
                if not result.success:
                    # Returned, not raised. A tool failure is information the
                    # model can act on -- "that column does not exist" usually
                    # leads to a corrected second attempt, where an exception
                    # ends the run.
                    return f"Error: {result.error or result.result_for_llm}"
                return result.result_for_llm

            return run

        specs.append(
            ToolSpec(
                name=schema.name,
                description=schema.description,
                parameters=schema.parameters,
                run=make_runner(schema.name),
            )
        )
    return specs


async def system_prompt(
    registry: Any, user: Any, *, dialect: str = "sqlite"
) -> str:
    """The same system prompt this project's own agent runs with.

    Reused rather than written afresh so an embedded agent inherits the dialect
    rules, the aggregation conventions and the refusal behaviour. A second prompt
    maintained alongside the first drifts from it within a release, and the
    symptom is an embedded agent that is subtly worse for reasons nobody can see.

    It is built from the *same tool list* the caller will be given, because the
    builder tailors its instructions to which tools exist -- telling a model to
    look up the schema first is unhelpful when it has no schema tool.
    """
    from ...core.system_prompt import AnalystSystemPromptBuilder

    builder = AnalystSystemPromptBuilder(dialect=dialect or "sqlite")
    schemas = await registry.get_schemas(user)
    return await builder.build_system_prompt(user, schemas) or ""


def as_json_schema(spec: ToolSpec) -> Dict[str, Any]:
    """OpenAI/Anthropic-style function description, for frameworks that want one."""
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def describe(specs: List[ToolSpec]) -> str:
    """A readable listing, for logs and `--help` output."""
    return json.dumps([as_json_schema(s) for s in specs], indent=2)
