"""A ``ToolContext`` for work done while building a prompt.

An enhancer runs before any tool is called, so it has a ``User`` but no context
-- and the capabilities it wants to consult (a catalog, a grant store, a value
dictionary) all take one, because they all scope by tenant. This builds the
minimum that satisfies them.

The memory it carries refuses to be used. Building a prompt reads catalogs and
dictionaries and nothing else; passing a working memory would make that a
convention, while passing one that raises makes it a fact, and the first call
site to drift fails loudly instead of quietly recording prompt assembly as agent
activity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..tool import ToolContext
    from ..user import User

#: Used as both the conversation and request id, so anything that logs one can
#: be recognised as prompt assembly rather than a real turn.
PROMPT_ASSEMBLY = "system-prompt"


def system_prompt_context(user: "User") -> "ToolContext":
    """A tenant-scoped context for a lookup made while assembling a prompt.

    The tenant comes from the resolved user and nowhere else -- the same rule
    the rest of the system follows, because a tenant a caller can supply is not
    a boundary.
    """
    from ..tool import ToolContext

    return ToolContext(
        user=user,
        conversation_id=PROMPT_ASSEMBLY,
        request_id=PROMPT_ASSEMBLY,
        tenant_id=getattr(user, "tenant_id", "default") or "default",
        agent_memory=refusing_memory(),
    )


def refusing_memory() -> Any:
    """An ``AgentMemory`` that raises on every method."""
    from ...capabilities.agent_memory import AgentMemory

    class _RefusingMemory(AgentMemory):
        def _refuse(self, *args: Any, **kwargs: Any):
            raise NotImplementedError(
                "prompt assembly must not read or write agent memory"
            )

        save_tool_usage = _refuse
        save_text_memory = _refuse
        search_similar_usage = _refuse
        search_text_memories = _refuse
        get_recent_memories = _refuse
        get_recent_text_memories = _refuse
        delete_by_id = _refuse
        delete_text_memory = _refuse
        clear_memories = _refuse

    return _RefusingMemory()
