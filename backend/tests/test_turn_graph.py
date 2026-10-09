"""The turn graph runner, tested without an agent.

These cover the runner's own contract -- edges, skipping, insertion, the cycle
guard -- with toy nodes, so a failure here points at `graph.py` rather than at
anything the agent does. The agent's own behaviour through the default graph is
covered by `test_agent_send_message.py` and `test_agent_streaming.py`, which
were written against the old loop and must keep passing unchanged.
"""

from __future__ import annotations

import pytest

from vanna.components import RichTextComponent, UiComponent
from vanna.core.agent.graph import (
    END,
    CallableTurnNode,
    TurnGraph,
    TurnGraphError,
    TurnNode,
    TurnState,
)


def _component(text: str) -> UiComponent:
    return UiComponent(rich_component=RichTextComponent(content=text, markdown=False))


class _Recorder(TurnNode):
    """Names its successor, records that it ran, optionally yields."""

    def __init__(self, name: str, goto: str, *, emits: bool = True, runs=None):
        self._name = name
        self._goto = goto
        self._emits = emits
        self.runs = runs if runs is not None else []

    @property
    def name(self) -> str:
        return self._name

    async def run(self, state):
        self.runs.append(self._name)
        if self._emits:
            yield _component(self._name)
        state.goto = self._goto


def _state(**overrides) -> TurnState:
    return TurnState(
        **{
            "user": None,
            "conversation": None,
            "context": None,
            "tool_schemas": [],
            "system_prompt": None,
            "config": None,
            "request_id": "r1",
            "conversation_id": "c1",
            **overrides,
        }
    )


async def _walk(graph: TurnGraph, state: TurnState) -> list:
    return [c async for c in graph.run(state)]


class TestEdges:
    async def test_runs_nodes_in_the_order_their_gotos_name(self):
        seen = []
        graph = TurnGraph(
            [
                _Recorder("a", "b", runs=seen),
                _Recorder("b", "c", runs=seen),
                _Recorder("c", END, runs=seen),
            ],
            entry="a",
        )

        await _walk(graph, _state())
        assert seen == ["a", "b", "c"]

    async def test_components_are_yielded_as_the_nodes_produce_them(self):
        graph = TurnGraph(
            [_Recorder("a", "b"), _Recorder("b", END)],
            entry="a",
        )

        components = await _walk(graph, _state())
        assert [c.rich_component.content for c in components] == ["a", "b"]

    async def test_a_node_may_be_revisited(self):
        """The tool loop is llm_turn -> tools -> llm_turn."""
        seen = []

        class _Twice(TurnNode):
            name = "loop"

            def __init__(self):
                self.count = 0

            async def run(self, state):
                self.count += 1
                seen.append(self.count)
                state.goto = "done" if self.count == 3 else "loop"
                return
                yield  # pragma: no cover - makes this an async generator

        node = _Twice()
        graph = TurnGraph([node, _Recorder("done", END, runs=seen)], entry="loop")

        await _walk(graph, _state())
        assert node.count == 3


class TestSkipping:
    async def test_a_skipped_node_falls_through_to_its_static_edge(self):
        seen = []

        class _Skipped(_Recorder):
            async def should_run(self, state):
                return False

        graph = TurnGraph(
            [
                _Recorder("a", "optional", runs=seen),
                _Skipped("optional", "never", runs=seen),
                _Recorder("z", END, runs=seen),
            ],
            entry="a",
            edges={"optional": "z"},
        )

        await _walk(graph, _state())
        # 'optional' declined, so its static edge to 'z' was taken -- not the
        # 'never' it would have named had it run.
        assert seen == ["a", "z"]

    async def test_a_skipped_node_with_no_static_edge_ends_the_turn(self):
        class _Skipped(_Recorder):
            async def should_run(self, state):
                return False

        graph = TurnGraph(
            [_Recorder("a", "optional"), _Skipped("optional", "never")],
            entry="a",
        )

        components = await _walk(graph, _state())
        assert [c.rich_component.content for c in components] == ["a"]

    async def test_should_run_can_read_state(self):
        class _OnlyWhenNoted(_Recorder):
            async def should_run(self, state):
                return state.notes.get("go") is True

        graph = TurnGraph(
            [_Recorder("a", "gated"), _OnlyWhenNoted("gated", END)],
            entry="a",
            edges={"gated": END},
        )

        quiet = await _walk(graph, _state())
        loud = await _walk(graph, _state(notes={"go": True}))

        assert len(quiet) == 1
        assert len(loud) == 2


class TestInsertion:
    async def test_insert_after_rewires_the_static_edge(self):
        base = TurnGraph(
            [_Recorder("a", "b"), _Recorder("b", END)],
            entry="a",
            edges={"a": "b", "b": END},
        )
        extended = base.insert_after(_Recorder("middle", END), after="a")

        assert "middle" in extended.names
        # The original is untouched: a graph is shared across requests.
        assert "middle" not in base.names

    async def test_inserting_does_not_divert_a_node_that_names_its_successor(self):
        """The built-in nodes set goto; inserting must not hijack the tool loop."""
        seen = []
        base = TurnGraph(
            [_Recorder("a", "b", runs=seen), _Recorder("b", END, runs=seen)],
            entry="a",
            edges={"a": "b"},
        )
        extended = base.insert_after(_Recorder("middle", END, runs=seen), after="a")

        await _walk(extended, _state())
        assert seen == ["a", "b"]

    def test_inserting_after_an_unknown_node_is_refused(self):
        graph = TurnGraph([_Recorder("a", END)], entry="a")
        with pytest.raises(TurnGraphError, match="unknown node"):
            graph.insert_after(_Recorder("x", END), after="nope")


class TestMalformedGraphs:
    def test_duplicate_node_names_are_refused(self):
        with pytest.raises(TurnGraphError, match="both named"):
            TurnGraph([_Recorder("a", END), _Recorder("a", END)], entry="a")

    def test_unknown_entry_is_refused(self):
        with pytest.raises(TurnGraphError, match="Entry node"):
            TurnGraph([_Recorder("a", END)], entry="b")

    def test_edge_to_an_unknown_node_is_refused(self):
        with pytest.raises(TurnGraphError, match="Edge to unknown"):
            TurnGraph([_Recorder("a", END)], entry="a", edges={"a": "ghost"})

    async def test_a_node_that_sets_no_edge_is_an_error(self):
        """Repeating it instead would hang the request."""

        class _Forgetful(TurnNode):
            name = "forgetful"

            async def run(self, state):
                return
                yield  # pragma: no cover

        graph = TurnGraph([_Forgetful()], entry="forgetful")
        with pytest.raises(TurnGraphError, match="without setting state.goto"):
            await _walk(graph, _state())

    async def test_a_cycle_is_stopped_rather_than_hanging(self):
        graph = TurnGraph(
            [_Recorder("a", "b"), _Recorder("b", "a")],
            entry="a",
            max_visits=10,
        )
        with pytest.raises(TurnGraphError, match="cycle"):
            await _walk(graph, _state())

    async def test_goto_to_an_unknown_node_is_an_error(self):
        graph = TurnGraph([_Recorder("a", "ghost")], entry="a")
        with pytest.raises(TurnGraphError, match="No node named"):
            await _walk(graph, _state())


class TestCallableTurnNode:
    async def test_wraps_a_plain_async_generator(self):
        async def body(state):
            yield _component("from a method")
            state.goto = END

        graph = TurnGraph([CallableTurnNode("wrapped", body)], entry="wrapped")
        components = await _walk(graph, _state())

        assert [c.rich_component.content for c in components] == ["from a method"]


def _with_entry(agent, node: TurnNode) -> TurnGraph:
    """The agent's default graph with *node* in front of it.

    `insert_after` rewires a static edge, which is the wrong tool here: all
    three built-in nodes name their own successor, so nothing would reach a
    node inserted between them. Putting one in front needs a new entry.
    """
    base = agent._default_turn_graph()
    return TurnGraph(
        [node] + [base._nodes[name] for name in base.names],
        entry=node.name,
        edges={node.name: "llm_turn"},
    )


class TestAgentUsesTheGraph:
    """The point of the refactor: a step can be added without editing the loop.

    `test_agent_send_message.py` and `test_agent_streaming.py` already prove the
    default graph behaves exactly as the old loop did -- they were written
    against that loop and pass unchanged. What they cannot show is the thing the
    graph was built for, so it is shown here.
    """

    def test_the_default_graph_is_the_old_loop(self, make_agent, mock_llm):
        agent, _ = make_agent(llm=mock_llm)
        graph = agent._default_turn_graph()

        assert graph.names == ["llm_turn", "tools", "answer"]
        assert graph.entry == "llm_turn"

    async def test_an_inserted_node_runs_inside_a_real_turn(
        self, make_agent, mock_llm, request_context
    ):
        """No edit to agent.py, no new extension point -- just a node."""
        ran = []

        class _Observer(TurnNode):
            name = "observer"

            async def run(self, state):
                ran.append(state.conversation_id)
                yield _component("observed")
                state.goto = END

        mock_llm.set_response("42 rows")
        agent, _ = make_agent(llm=mock_llm)

        # The observer runs before the model, so it becomes the entry and hands
        # on to llm_turn. Nothing in agent.py knows it exists.
        agent.turn_graph = _with_entry(agent, _Observer())

        components = [
            c
            async for c in agent.send_message(
                request_context, "how many rows?", conversation_id="conv-graph"
            )
        ]

        assert ran == ["conv-graph"]
        assert any(
            getattr(c.rich_component, "content", None) == "observed"
            for c in components
            if getattr(c, "rich_component", None) is not None
        )

    async def test_an_inserted_node_can_stop_the_turn_before_the_model(
        self, make_agent, mock_llm, request_context
    ):
        """A gate node: the thing no existing extension point could express."""

        class _Gate(TurnNode):
            name = "gate"

            async def run(self, state):
                yield _component("refused")
                state.goto = END

        agent, _ = make_agent(llm=mock_llm)
        agent.turn_graph = _with_entry(agent, _Gate())

        components = [
            c
            async for c in agent.send_message(
                request_context, "hi", conversation_id="conv-gate"
            )
        ]

        assert mock_llm.call_count == 0
        assert any(
            getattr(c.rich_component, "content", None) == "refused"
            for c in components
            if getattr(c, "rich_component", None) is not None
        )
