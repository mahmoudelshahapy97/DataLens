"""`CriticNode` -- the self-check between the model finishing and the user seeing it.

Two things are being tested, and the second matters more than the first.

The first is that it catches a wrong answer: it sends the turn back with the
gap named, and the analyst gets another go.

The second is everything that must make it *safe to leave on*. A check that
runs on every turn would double the bill; one that fails closed would be able
to block every answer in the product; one that retried without limit would burn
a user's quota arguing with itself. Those are the tests that decide whether
this is shippable, so there are more of them.
"""

from __future__ import annotations

import pytest

from vanna.core.agent.graph import TurnGraph, TurnState
from vanna.core.llm import LlmResponse
from vanna.core.storage import Conversation, Message
from vanna.core.tool import ToolCall
from vanna.core.user import User
from vanna_app.agent_nodes import CriticNode


class _Llm:
    """Returns queued verdicts; records the prompts it was asked to judge."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.seen = []

    async def send_request(self, request):
        self.seen.append(request)
        nxt = self.verdicts.pop(0) if self.verdicts else "OK"
        if isinstance(nxt, Exception):
            raise nxt
        return LlmResponse(content=nxt, finish_reason="stop")


def _user() -> User:
    return User(id="u1", email="u1@acme.test", tenant_id="acme")


def _conversation(*, tool: str = "run_sql", question: str = "How many tracks?"):
    conversation = Conversation(id="c1", user=_user())
    conversation.add_message(Message(role="user", content=question))
    if tool:
        conversation.add_message(
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="t1", name=tool, arguments={})],
            )
        )
        conversation.add_message(
            Message(role="tool", content="3503", tool_call_id="t1")
        )
    return conversation


def _state(
    *, answer="There are 3,503 tracks.", tool="run_sql", question="How many tracks?"
):
    return TurnState(
        user=_user(),
        conversation=_conversation(tool=tool, question=question),
        context=None,
        tool_schemas=[],
        system_prompt=None,
        config=None,
        request_id="r1",
        conversation_id="c1",
        response=LlmResponse(content=answer, finish_reason="stop"),
    )


async def _run(node, state):
    return [c async for c in node.run(state)]


class TestWhenItRuns:
    async def test_runs_after_a_query(self):
        assert await CriticNode(_Llm()).should_run(_state()) is True

    async def test_does_not_run_when_no_data_tool_was_used(self):
        """A schema lookup produces no result to hold against the question."""
        assert (
            await CriticNode(_Llm()).should_run(_state(tool="search_tables")) is False
        )

    async def test_does_not_run_on_a_turn_with_no_tools_at_all(self):
        assert await CriticNode(_Llm()).should_run(_state(tool="")) is False

    @pytest.mark.parametrize(
        "tool", ["run_sql", "analyze_timeseries", "compare_periods"]
    )
    async def test_runs_for_every_data_tool(self, tool):
        assert await CriticNode(_Llm()).should_run(_state(tool=tool)) is True

    async def test_does_not_run_on_an_empty_answer(self):
        assert await CriticNode(_Llm()).should_run(_state(answer="")) is False

    async def test_does_not_run_once_retries_are_spent(self):
        state = _state()
        state.notes["critic_retries"] = 1
        assert await CriticNode(_Llm(), max_retries=1).should_run(state) is False

    async def test_can_be_switched_off_entirely(self):
        assert await CriticNode(_Llm(), max_retries=0).should_run(_state()) is False


class TestVerdicts:
    async def test_an_approved_answer_goes_straight_through(self):
        state = _state()
        await _run(CriticNode(_Llm("OK")), state)

        assert state.goto == "answer"
        assert "critic_retries" not in state.notes

    async def test_a_rejected_answer_goes_back_to_the_model(self):
        state = _state()
        await _run(
            CriticNode(_Llm("The question asked for albums, not tracks.")), state
        )

        assert state.goto == "llm_turn"
        assert state.notes["critic_retries"] == 1

    async def test_the_critique_is_appended_for_the_model_to_act_on(self):
        state = _state()
        await _run(
            CriticNode(_Llm("The question asked for albums, not tracks.")), state
        )

        last = state.conversation.messages[-1]
        assert last.role == "user"
        assert "albums, not tracks" in last.content
        # The user must not see the machinery.
        assert "do not apologise or mention this review" in last.content

    async def test_the_cached_request_is_cleared_so_history_is_rebuilt(self):
        """Replaying the old request would resend the rejected answer's prompt."""
        state = _state()
        state.request = object()
        await _run(CriticNode(_Llm("Wrong measure.")), state)

        assert state.request is None

    async def test_the_judge_is_shown_the_question_and_the_answer(self):
        llm = _Llm("OK")
        await _run(CriticNode(llm), _state(question="How many albums?"))

        prompt = llm.seen[0].messages[0].content
        assert "How many albums?" in prompt
        assert "There are 3,503 tracks." in prompt

    async def test_the_judge_is_told_not_to_reject_for_style(self):
        llm = _Llm("OK")
        await _run(CriticNode(llm), _state())
        assert "Do not reject for style" in llm.seen[0].system_prompt


class TestFailingSafe:
    async def test_a_critic_that_errors_approves(self):
        """A torn safety net beats a blocked product."""
        state = _state()
        await _run(CriticNode(_Llm(RuntimeError("provider down"))), state)

        assert state.goto == "answer"

    async def test_an_empty_verdict_approves(self):
        state = _state()
        await _run(CriticNode(_Llm("")), state)
        assert state.goto == "answer"

    @pytest.mark.parametrize("verdict", ["OK", "ok", "OK.", "OK - reads fine"])
    async def test_approval_is_recognised_however_it_is_spelled(self, verdict):
        state = _state()
        await _run(CriticNode(_Llm(verdict)), state)
        assert state.goto == "answer"

    async def test_it_is_cheap_when_it_approves(self):
        """Exactly one extra call, and only on a data turn."""
        llm = _Llm("OK")
        await _run(CriticNode(llm), _state())
        assert len(llm.seen) == 1


class TestGraphWiring:
    def test_it_slots_between_llm_turn_and_answer(self):
        """The shape `platform.py` builds, asserted here so a rename is caught."""
        from vanna.core.agent.graph import END, CallableTurnNode

        async def noop(state):
            state.goto = END
            return
            yield  # pragma: no cover

        graph = TurnGraph(
            [
                CallableTurnNode("llm_turn", noop),
                CallableTurnNode("tools", noop),
                CallableTurnNode("answer", noop),
                CriticNode(_Llm()),
            ],
            entry="llm_turn",
            # The static edge is what a spent critic falls through to.
            edges={"critic": "answer", "tools": "llm_turn", "answer": END},
        )
        assert "critic" in graph.names


class TestPlannerNode:
    """Off by default, so the gate matters more than the plan it writes."""

    @staticmethod
    def _state(question: str):
        conversation = Conversation(id="c1", user=_user())
        conversation.add_message(Message(role="user", content=question))
        return TurnState(
            user=_user(),
            conversation=conversation,
            context=None,
            tool_schemas=[],
            system_prompt=None,
            config=None,
            request_id="r1",
            conversation_id="c1",
        )

    async def test_skips_a_short_simple_question(self):
        from vanna_app.agent_nodes import PlannerNode

        state = self._state("How many customers?")
        assert await PlannerNode(_Llm()).should_run(state) is False

    async def test_skips_a_long_question_with_no_multi_step_words(self):
        from vanna_app.agent_nodes import PlannerNode

        state = self._state(
            "Please list every single customer name and their email address."
        )
        assert await PlannerNode(_Llm()).should_run(state) is False

    async def test_fires_on_a_comparison(self):
        from vanna_app.agent_nodes import PlannerNode

        state = self._state(
            "Compare revenue by genre this year against last year and explain it"
        )
        assert await PlannerNode(_Llm()).should_run(state) is True

    async def test_plans_only_once_per_turn(self):
        """The tool loop passes back through the entry node."""
        from vanna_app.agent_nodes import PlannerNode

        state = self._state(
            "Compare revenue by genre this year against last year and explain it"
        )
        state.notes["planned"] = True
        assert await PlannerNode(_Llm()).should_run(state) is False

    async def test_a_drafted_plan_is_shown_and_given_to_the_model(self):
        from vanna_app.agent_nodes import PlannerNode

        llm = _Llm("- Find revenue by genre for each year\n- Compare the two")
        state = self._state("Compare revenue by genre year on year and explain")

        components = [c async for c in PlannerNode(llm).run(state)]

        assert state.notes["plan"] == [
            "Find revenue by genre for each year",
            "Compare the two",
        ]
        assert len(components) == 1
        assert components[0].rich_component.title == "Approach"
        assert "work to this approach" in state.conversation.messages[-1].content
        assert state.request is None
        assert state.goto == "llm_turn"

    async def test_the_model_may_veto_the_plan(self):
        from vanna_app.agent_nodes import PlannerNode

        llm = _Llm("SIMPLE")
        state = self._state("Compare revenue by genre year on year and explain")

        components = [c async for c in PlannerNode(llm).run(state)]

        assert components == []
        assert "plan" not in state.notes
        assert state.goto == "llm_turn"

    async def test_a_one_step_plan_is_not_a_plan(self):
        from vanna_app.agent_nodes import PlannerNode

        llm = _Llm("- Just query it")
        state = self._state("Compare revenue by genre year on year and explain")

        components = [c async for c in PlannerNode(llm).run(state)]
        assert components == []

    async def test_a_planner_that_errors_does_not_stop_the_turn(self):
        from vanna_app.agent_nodes import PlannerNode

        state = self._state("Compare revenue by genre year on year and explain")
        components = [
            c async for c in PlannerNode(_Llm(RuntimeError("down"))).run(state)
        ]

        assert components == []
        assert state.goto == "llm_turn"


class TestRuntimeWiring:
    """`_install_turn_nodes` is what decides whether any of this is switched on."""

    @staticmethod
    def _agent():
        from vanna.core.agent.agent import Agent
        from vanna.core.registry import ToolRegistry
        from vanna.integrations.local import MemoryConversationStore
        from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

        class _Resolver:
            async def resolve_user(self, request_context):
                return _user()

        return Agent(
            llm_service=_Llm(),
            tool_registry=ToolRegistry(),
            user_resolver=_Resolver(),
            agent_memory=DemoAgentMemory(),
            conversation_store=MemoryConversationStore(),
        )

    @staticmethod
    def _settings(**overrides):
        from types import SimpleNamespace

        return SimpleNamespace(
            **{
                "enable_critic": False,
                "max_critic_retries": 1,
                "enable_planner": False,
                **overrides,
            }
        )

    def test_nothing_is_wired_when_both_are_off(self):
        from vanna_app.platform import _install_turn_nodes

        agent = self._agent()
        _install_turn_nodes(agent, _Llm(), self._settings())

        assert agent.turn_graph is None
        assert agent.answer_node == "answer"

    def test_the_critic_takes_over_the_answer_edge(self):
        from vanna_app.platform import _install_turn_nodes

        agent = self._agent()
        _install_turn_nodes(agent, _Llm(), self._settings(enable_critic=True))

        assert "critic" in agent.turn_graph.names
        # This is what puts it between the model and the user.
        assert agent.answer_node == "critic"
        # And this is where it falls through once out of retries.
        assert agent.turn_graph.edges["critic"] == "answer"

    def test_zero_retries_switches_the_critic_off(self):
        from vanna_app.platform import _install_turn_nodes

        agent = self._agent()
        _install_turn_nodes(
            agent, _Llm(), self._settings(enable_critic=True, max_critic_retries=0)
        )
        assert agent.turn_graph is None

    def test_the_planner_becomes_the_entry_node(self):
        from vanna_app.platform import _install_turn_nodes

        agent = self._agent()
        _install_turn_nodes(agent, _Llm(), self._settings(enable_planner=True))

        assert agent.turn_graph.entry == "planner"
        assert agent.turn_graph.edges["planner"] == "llm_turn"
        assert agent.answer_node == "answer"

    def test_both_can_be_wired_at_once(self):
        from vanna_app.platform import _install_turn_nodes

        agent = self._agent()
        _install_turn_nodes(
            agent, _Llm(), self._settings(enable_critic=True, enable_planner=True)
        )

        graph = agent.turn_graph
        assert graph.entry == "planner"
        assert agent.answer_node == "critic"
        assert set(graph.names) == {"llm_turn", "tools", "answer", "critic", "planner"}
