"""The state one turn of the agent carries between its nodes.

Before this existed, every one of these values was a local variable inside
``Agent._send_message``, which is why a new step could not be added without
editing that method: there was nothing to hand a step. Naming the state is what
makes the turn a graph rather than a loop.

The split between the three groups below is the useful part:

* **Inputs** are settled before the turn starts and are not written again. A
  node that rewrites ``system_prompt`` mid-turn is doing something the prompt
  cache cannot see, so they are documented as read-only rather than enforced --
  enforcement would mean copying the conversation on every hop.
* **Working values** are what one node leaves for the next. ``response`` is the
  obvious one: the LLM node produces it and the tool node consumes it.
* **Control** is ``goto``, the edge. A node sets it to the name of the next
  node, or to :data:`END` to finish the turn.

``notes`` is the extension point. A node added later -- a planner, a critic --
keeps its working data there rather than growing this class, so a new node
needs no change to the state it travels in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...llm.models import LlmRequest, LlmResponse
    from ...storage import Conversation
    from ...tool.models import ToolContext, ToolSchema
    from ...user.models import User
    from ..config import AgentConfig

#: The terminal node name. A node sets ``state.goto = END`` to finish the turn.
#: A string rather than a sentinel object so a graph can be described, logged
#: and asserted on in plain text.
END = "__end__"


@dataclass
class TurnState:
    """Everything one turn needs, and everything it accumulates."""

    # -- Inputs: settled before the turn begins, treated as read-only --------
    user: "User"
    conversation: "Conversation"
    context: "ToolContext"
    tool_schemas: List["ToolSchema"]
    system_prompt: Optional[str]
    config: "AgentConfig"
    request_id: str
    conversation_id: Optional[str]
    ui_features_available: List[str] = field(default_factory=list)

    # -- Working values: what one node leaves for the next -------------------
    request: Optional["LlmRequest"] = None
    response: Optional["LlmResponse"] = None
    #: Id of the bubble the answer was streamed into, so the final text can
    #: patch it in place instead of appearing twice. Reset every iteration --
    #: a second round of prose gets its own bubble.
    streamed_id: Optional[str] = None
    truncated: bool = False
    iterations: int = 0

    # -- Outputs read by the epilogue after the graph finishes ---------------
    hit_tool_limit: bool = False

    # -- Control -------------------------------------------------------------
    #: Name of the next node to run. A node that leaves this unchanged is
    #: treated as an error by the runner rather than silently repeating: an
    #: unset edge is a bug in the node, and looping on it would hang the turn.
    goto: Optional[str] = None

    #: Scratchpad for nodes added later. Keyed by node name by convention, so
    #: two nodes cannot quietly collide on the same key.
    notes: Dict[str, Any] = field(default_factory=dict)
