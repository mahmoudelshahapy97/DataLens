"""The streaming answer path: deltas, reconciliation, and truncation.

``AgentConfig.stream_responses`` defaults to True, so this is the path every
production chat turn takes -- and until now it had no coverage at all. The
method it exercises used to drain the provider stream and return a single
response, emitting nothing until the turn finished; these tests pin the
incremental behaviour that replaced it.

The contract under test has three parts:

* the answer arrives as several text components sharing one id, the first
  ``create`` and the rest ``update``, so a rich client patches one bubble;
* exactly one component carries a ``simple_component``, so a simple client with
  no lifecycle notion sees one message rather than one per delta;
* a provider that stops at its token ceiling says so, and a tool call truncated
  mid-arguments is not executed.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from vanna.components import ComponentLifecycle
from vanna.core.agent.config import AgentConfig
from vanna.core.llm import LlmResponse
from vanna.core.rich_component import ComponentType
from vanna.core.tool import ToolCall
from vanna.tools.calculator import CalculatorTool

from test_agent_send_message import EchoTool


STREAMING = AgentConfig(stream_responses=True)


def _text_components(components: List[Any]) -> List[Any]:
    """The rich text components from a turn, in emission order."""
    return [
        c.rich_component
        for c in components
        if c.rich_component is not None
        and c.rich_component.type == ComponentType.TEXT
    ]


async def _collect(agent, request_context, message="How many orders?"):
    return [c async for c in agent.send_message(request_context, message)]


class TestIncrementalDeltas:
    """Text should appear while the model is still talking, not after."""

    @pytest.mark.asyncio
    async def test_answer_arrives_in_several_frames(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        texts = _text_components(await _collect(agent, request_context))

        assert len(texts) >= 2, "a streamed answer should emit more than one frame"

    @pytest.mark.asyncio
    async def test_frames_share_one_id_and_patch_it(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        texts = _text_components(await _collect(agent, request_context))

        assert len({t.id for t in texts}) == 1, "all frames must patch one bubble"
        assert texts[0].lifecycle == ComponentLifecycle.CREATE
        assert all(t.lifecycle == ComponentLifecycle.UPDATE for t in texts[1:])

    @pytest.mark.asyncio
    async def test_content_only_grows(self, make_agent, mock_llm, request_context):
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        lengths = [len(t.content) for t in _text_components(
            await _collect(agent, request_context)
        )]

        assert lengths == sorted(lengths)
        assert lengths[0] > 0

    @pytest.mark.asyncio
    async def test_final_frame_holds_the_whole_answer(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        texts = _text_components(await _collect(agent, request_context))

        assert texts[-1].content == "Hello! This is a mock response. (Streamed #1)"


class TestSimpleClientContract:
    """A client without lifecycle support must not see N copies of the answer."""

    @pytest.mark.asyncio
    async def test_exactly_one_simple_payload(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        components = await _collect(agent, request_context)
        simple = [
            c.simple_component for c in components if c.simple_component is not None
        ]

        assert len(simple) == 1
        assert simple[0].text == "Hello! This is a mock response. (Streamed #1)"


class TestNonStreamingUnchanged:
    """The non-streaming branch is the kill switch; it must not have moved."""

    @pytest.mark.asyncio
    async def test_single_create_component(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(
            llm=mock_llm, config=AgentConfig(stream_responses=False)
        )

        texts = _text_components(await _collect(agent, request_context))

        assert len(texts) == 1
        assert texts[0].lifecycle == ComponentLifecycle.CREATE


class TestToolCallAfterPartialText:
    """Prose already on screen when the response turns out to be a tool call.

    You cannot know a response is a tool call until it ends, so a preamble is
    always streamed before its tool_calls arrive. Whether that preamble belongs
    in the transcript is a UI-feature decision, and both answers have to leave
    the screen consistent.

    The preamble here is deliberately longer than the delta flush threshold, so
    it is genuinely on screen by the time the tool call is known.
    """

    PREAMBLE = "Let me work that out for you, one moment please."

    def _queue_preamble_then_answer(self, mock_llm):
        mock_llm.queue_response(
            LlmResponse(
                content=self.PREAMBLE,
                tool_calls=[
                    ToolCall(
                        id="c1", name="calculator", arguments={"expression": "2+2"}
                    )
                ],
            )
        )
        mock_llm.queue_response(LlmResponse(content="The answer is 4."))

    @pytest.mark.asyncio
    async def test_admin_sees_the_preamble_patched_not_duplicated(
        self, make_agent, mock_llm, request_context, chat_user_factory
    ):
        self._queue_preamble_then_answer(mock_llm)
        agent, _ = make_agent(
            llm=mock_llm,
            tools=[(CalculatorTool(), [])],
            user=chat_user_factory(admin=True),
            config=STREAMING,
        )

        texts = _text_components(await _collect(agent, request_context))
        preamble_frames = [t for t in texts if self.PREAMBLE in t.content]

        assert len(preamble_frames) >= 2, "streamed, then reconciled"
        assert len({t.id for t in preamble_frames}) == 1
        assert preamble_frames[0].lifecycle == ComponentLifecycle.CREATE
        assert preamble_frames[-1].lifecycle == ComponentLifecycle.UPDATE

    @pytest.mark.asyncio
    async def test_non_admin_has_the_preamble_taken_back(
        self, make_agent, mock_llm, request_context
    ):
        # For this user the preamble belongs in the status bar, not the
        # transcript -- but streaming already put it there. It has to be removed,
        # not left orphaned above the answer.
        self._queue_preamble_then_answer(mock_llm)
        agent, _ = make_agent(
            llm=mock_llm, tools=[(CalculatorTool(), [])], config=STREAMING
        )

        texts = _text_components(await _collect(agent, request_context))
        preamble_frames = [t for t in texts if self.PREAMBLE in t.content]

        assert preamble_frames, "the preamble was streamed before it could be judged"
        assert preamble_frames[-1].lifecycle == ComponentLifecycle.REMOVE
        assert preamble_frames[-1].id == preamble_frames[0].id

    @pytest.mark.asyncio
    async def test_the_answer_gets_its_own_bubble(
        self, make_agent, mock_llm, request_context, chat_user_factory
    ):
        # streamed_id must reset per iteration, or round two would patch round
        # one's bubble and the preamble would be overwritten by the answer.
        self._queue_preamble_then_answer(mock_llm)
        agent, _ = make_agent(
            llm=mock_llm,
            tools=[(CalculatorTool(), [])],
            user=chat_user_factory(admin=True),
            config=STREAMING,
        )

        texts = _text_components(await _collect(agent, request_context))
        answer = [t for t in texts if "The answer is 4." in t.content]
        preamble = [t for t in texts if self.PREAMBLE in t.content]

        assert answer
        assert answer[0].id != preamble[0].id


class TestTruncation:
    """A provider that ran out of room should say so, not pretend it finished."""

    @pytest.mark.asyncio
    async def test_cut_off_answer_gets_a_notice(
        self, make_agent, mock_llm, request_context
    ):
        mock_llm.queue_response(
            LlmResponse(content="The total is 1,2", finish_reason="max_tokens")
        )
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        rendered = " ".join(
            t.content for t in _text_components(await _collect(agent, request_context))
        )

        assert "The total is 1,2" in rendered
        assert "cut off" in rendered

    @pytest.mark.asyncio
    async def test_openai_spelling_is_recognised(
        self, make_agent, mock_llm, request_context
    ):
        # Anthropic says "max_tokens", OpenAI says "length".
        mock_llm.queue_response(
            LlmResponse(content="Partial answer", finish_reason="length")
        )
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        rendered = " ".join(
            t.content for t in _text_components(await _collect(agent, request_context))
        )

        assert "cut off" in rendered

    @pytest.mark.asyncio
    async def test_truncated_tool_call_is_not_executed(
        self, make_agent, mock_llm, request_context
    ):
        # Arguments cut mid-JSON can be a truncated SQL string. Refusing to run
        # them is the whole point of plumbing finish_reason through.
        spy = EchoTool(name="recorder")
        mock_llm.queue_response(
            LlmResponse(
                content="Checking.",
                tool_calls=[
                    ToolCall(id="c1", name="recorder", arguments={"text": "SELECT"})
                ],
                finish_reason="max_tokens",
            )
        )
        agent, _ = make_agent(llm=mock_llm, tools=[(spy, [])], config=STREAMING)

        rendered = " ".join(
            t.content for t in _text_components(await _collect(agent, request_context))
        )

        assert spy.calls == []
        assert "ran out of room" in rendered

    @pytest.mark.asyncio
    async def test_normal_stop_gets_no_notice(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm, config=STREAMING)

        rendered = " ".join(
            t.content for t in _text_components(await _collect(agent, request_context))
        )

        assert "cut off" not in rendered
