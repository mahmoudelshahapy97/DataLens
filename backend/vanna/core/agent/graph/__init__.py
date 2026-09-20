"""The turn graph: the shape of one agent turn, as nodes and edges.

``Agent._send_message`` used to be a single method with the whole turn inside
it -- setup, a ``while`` loop alternating LLM call and tool execution, then
persistence. Adding a step meant editing that method, and none of the seven
existing extension points could branch control flow or sit between the model
call and the tools.

The turn is now a graph. The three built-in nodes reproduce the old loop
exactly::

    llm_turn  --(tool calls)-->  tools  --> llm_turn
        |
        +-----(no tool calls)-->  answer  --> END

so wiring no graph at all leaves behaviour unchanged. A new step is a
:class:`TurnNode` inserted into a copy of the graph, not an edit to the loop.
"""

from .graph import TurnGraph, TurnGraphError
from .node import CallableTurnNode, TurnNode
from .state import END, TurnState

__all__ = [
    "END",
    "CallableTurnNode",
    "TurnGraph",
    "TurnGraphError",
    "TurnNode",
    "TurnState",
]
