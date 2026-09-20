"""The runner: nodes, the edges between them, and a budget.

A graph is a name-to-node map plus a static edge per node. A node normally
names its own successor by setting ``state.goto``; the static edge is what a
*skipped* node falls through to, which is how an optional node can be inserted
between two others without either of them knowing it exists.

The budget is not the same as ``max_tool_iterations``. That one counts trips to
the model, and is the user-visible "I've been at this a while" limit. This one
counts node visits and exists only to stop a malformed graph -- two nodes
pointing at each other -- from hanging a request. Hitting it is a bug, so it
raises rather than producing a polite message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncGenerator, Dict, List, Optional

from .state import END

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...components import UiComponent
    from .node import TurnNode
    from .state import TurnState

logger = logging.getLogger(__name__)

#: Node visits allowed in one turn. Generous: a twelve-iteration tool loop
#: already costs twenty-five visits through llm_turn and tools, and a graph
#: with a critic retrying costs more. Low enough that a cycle still stops.
_MAX_VISITS = 256


class TurnGraphError(RuntimeError):
    """The graph is malformed. Always a programming error, never user input."""


class TurnGraph:
    """An ordered set of nodes and the edges between them.

    Args:
        nodes: The nodes, in no particular order.
        entry: Name of the node a turn starts at.
        edges: Static successor per node name, used when a node is skipped by
            ``should_run``. A node with no entry here falls through to ``END``
            when skipped.
        max_visits: Cycle guard. See the module docstring.
    """

    def __init__(
        self,
        nodes: List["TurnNode"],
        *,
        entry: str,
        edges: Optional[Dict[str, str]] = None,
        max_visits: int = _MAX_VISITS,
    ) -> None:
        self._nodes: Dict[str, "TurnNode"] = {}
        for node in nodes:
            if node.name in self._nodes:
                raise TurnGraphError(f"Two nodes are both named {node.name!r}.")
            self._nodes[node.name] = node

        if entry not in self._nodes:
            raise TurnGraphError(f"Entry node {entry!r} is not in the graph.")

        self._edges = dict(edges or {})
        for source, target in self._edges.items():
            if source not in self._nodes:
                raise TurnGraphError(f"Edge from unknown node {source!r}.")
            if target != END and target not in self._nodes:
                raise TurnGraphError(f"Edge to unknown node {target!r}.")

        self.entry = entry
        self.max_visits = max_visits

    # ------------------------------------------------------------------

    @property
    def names(self) -> List[str]:
        """Node names, in insertion order. Mostly for tests and logging."""
        return list(self._nodes)

    @property
    def nodes(self) -> List["TurnNode"]:
        """The nodes themselves, for building a wider graph around this one."""
        return list(self._nodes.values())

    @property
    def edges(self) -> Dict[str, str]:
        """A copy of the static edge map. Copied so a caller cannot rewire a
        graph other requests are already running."""
        return dict(self._edges)

    def insert_after(self, node: "TurnNode", *, after: str) -> "TurnGraph":
        """A copy with *node* inserted, taking over ``after``'s static edge.

        Returns a new graph rather than mutating: a graph is shared by every
        request on a runtime, so mutating one after boot would change the shape
        of turns already in flight.

        Note this rewires only the **static** edge. A node that names its own
        successor -- as all three built-in nodes do -- is unaffected, which is
        deliberate: inserting a critic must not silently divert the tool loop.
        The inserted node is reached because the node before it sets ``goto``,
        or because it is skipped and falls through.
        """
        if after not in self._nodes:
            raise TurnGraphError(f"Cannot insert after unknown node {after!r}.")

        edges = dict(self._edges)
        edges[node.name] = edges.get(after, END)
        edges[after] = node.name
        return TurnGraph(
            list(self._nodes.values()) + [node],
            entry=self.entry,
            edges=edges,
            max_visits=self.max_visits,
        )

    async def run(self, state: "TurnState") -> AsyncGenerator["UiComponent", None]:
        """Walk the graph until a node says ``END``."""
        current: Optional[str] = self.entry
        visits = 0

        while current is not None and current != END:
            node = self._nodes.get(current)
            if node is None:
                raise TurnGraphError(f"No node named {current!r}.")

            visits += 1
            if visits > self.max_visits:
                raise TurnGraphError(
                    f"Turn exceeded {self.max_visits} node visits; the graph "
                    f"has a cycle. Last node: {current!r}."
                )

            if not await node.should_run(state):
                current = self._edges.get(node.name, END)
                continue

            # Cleared before the node runs, so "did this node set an edge?" is
            # answerable afterwards. Carrying the previous node's goto forward
            # would make a node that forgot to set one silently inherit it.
            state.goto = None
            async for component in node.run(state):
                yield component

            if state.goto is None:
                raise TurnGraphError(
                    f"Node {node.name!r} finished without setting state.goto."
                )
            current = state.goto
