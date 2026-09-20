"""The node interface, and the adapter that turns an agent method into one.

A node is one step of a turn. It reads and writes :class:`TurnState`, yields UI
components as it goes, and sets ``state.goto`` to name the next node.

Two things are deliberate:

* **Nodes yield.** A node that returned its components instead would have to
  finish before any of them reached the user, which would turn a streamed
  answer into a blocking one.
* **`should_run` is separate from `run`.** An optional node -- a planner that
  only fires on complex questions, a critic that only fires after a query --
  must be able to decline without being entered, because entering it is what
  costs an LLM call. A node that declines does not get to set ``goto``, so the
  graph's static edge is followed instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, AsyncGenerator, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...components import UiComponent
    from .state import TurnState


class TurnNode(ABC):
    """One step in a turn."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name. This is what ``state.goto`` and the edge map use."""

    @abstractmethod
    def run(self, state: "TurnState") -> AsyncGenerator["UiComponent", None]:
        """Advance the turn.

        Mutate *state*, yield any UI, and set ``state.goto`` to the next node's
        name or to ``END``. Leaving ``goto`` unset is an error: the runner
        raises rather than repeating the node, because a node that does not
        name its successor cannot be distinguished from one that meant to stop.
        """

    async def should_run(self, state: "TurnState") -> bool:
        """Whether this node should be entered at all.

        Returning False skips it without cost, and the graph's static edge for
        this node is followed instead. The default enters always, which is what
        the three built-in nodes want.
        """
        return True


class CallableTurnNode(TurnNode):
    """Adapts a bound method to the node interface.

    This exists so the three built-in nodes can stay methods on ``Agent``.
    Their bodies reach for ``self.config``, ``self.tool_registry``,
    ``self.audit_logger`` and a dozen other collaborators; moving them into
    free-standing classes would mean either passing the agent into every one or
    rewriting every reference. Neither buys anything -- the seam that matters
    is the graph, not where the default implementations happen to live.

    A node someone adds later has no such history and should subclass
    :class:`TurnNode` directly.
    """

    def __init__(
        self,
        name: str,
        run: Callable[["TurnState"], AsyncGenerator["UiComponent", None]],
    ) -> None:
        self._name = name
        self._run = run

    @property
    def name(self) -> str:
        return self._name

    def run(self, state: "TurnState") -> AsyncGenerator["UiComponent", None]:
        return self._run(state)
