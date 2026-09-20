"""`request_clarification`, and the `END_TURN` signal that makes it work.

The tool on its own is small. The part worth testing is the contract it depends
on: a successful result carrying `END_TURN` must stop the agent loop, and the
tool's result must still be written to the conversation before it stops -- an
assistant message carrying `tool_calls` with no matching tool result is
rejected by the provider on the following turn.

`TestEndTurnContract` drives a real `Agent` with a scripted LLM to check that,
because it is a property of the loop in `vanna/core/agent/agent.py`, not of
this tool, and nothing else in the suite would catch its removal.
"""

from __future__ import annotations

import pytest

from vanna.core.llm import LlmResponse
from vanna.core.tool import END_TURN, ToolCall, ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.clarify import RequestClarificationArgs, RequestClarificationTool


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id="acme"),
        conversation_id="c1",
        request_id="r1",
        tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )


async def _clarify(**overrides):
    args = RequestClarificationArgs(
        **{
            "question": "'Top' could mean by revenue or by order count.",
            "options": [
                "Which 10 customers spent the most in 2024?",
                "Which 10 customers placed the most orders in 2024?",
            ],
            **overrides,
        }
    )
    return await RequestClarificationTool().execute(_context(), args)


class TestRequestClarificationTool:
    def test_declares_no_sql_argument_fields(self):
        assert RequestClarificationTool().sql_argument_fields == ()

    async def test_sets_the_end_turn_flag(self):
        result = await _clarify()
        assert result.success
        assert result.metadata[END_TURN] is True

    async def test_each_option_is_sent_verbatim_as_the_next_message(self):
        """The action is what gets sent, so it must be the whole question."""
        result = await _clarify()

        actions = [a["action"] for a in result.ui_component.rich_component.actions]
        assert actions == [
            "Which 10 customers spent the most in 2024?",
            "Which 10 customers placed the most orders in 2024?",
        ]

    async def test_long_option_is_truncated_in_the_label_only(self):
        long_question = "Which customers " + "x" * 200 + "?"
        result = await _clarify(options=[long_question, "Something shorter?"])

        button = result.ui_component.rich_component.actions[0]
        assert len(button["label"]) <= 60
        assert button["label"].endswith("...")
        # The button still carries the whole question.
        assert button["action"] == long_question

    async def test_duplicate_options_are_collapsed(self):
        result = await _clarify(
            options=["By revenue?", "By  revenue?", "By order count?"]
        )
        assert result.metadata["options"] == ["By revenue?", "By order count?"]

    async def test_refuses_when_deduplication_leaves_one_option(self):
        """Two identical buttons are not a choice."""
        result = await _clarify(options=["By revenue?", "By revenue?"])

        assert not result.success
        assert "distinct" in result.result_for_llm
        assert "stated assumption" in result.result_for_llm

    async def test_tells_the_model_not_to_answer_for_the_user(self):
        result = await _clarify()
        assert "Do not answer on their behalf" in result.result_for_llm

    def test_schema_bounds_the_number_of_options(self):
        schema = RequestClarificationTool().get_schema().parameters
        assert schema["properties"]["options"]["minItems"] == 2
        assert schema["properties"]["options"]["maxItems"] == 4


class TestEndTurnContract:
    """A property of the agent loop in `vanna/core/agent/agent.py`, not of this
    tool. Driven through the shared `make_agent` fixture, the same way
    `test_agent_send_message.py` drives the rest of the pipeline."""

    @staticmethod
    def _tool_call() -> LlmResponse:
        return LlmResponse(
            content=None,
            tool_calls=[
                ToolCall(id="call-1", name="request_clarification", arguments=_ARGS)
            ],
            finish_reason="tool_calls",
        )

    async def _run(self, make_agent, mock_llm, request_context, tool, *, extra=None):
        mock_llm.queue_response(self._tool_call())
        if extra is not None:
            mock_llm.queue_response(extra)

        agent, user = make_agent(llm=mock_llm, tools=[(tool, [])])
        components = [
            c
            async for c in agent.send_message(
                request_context, "top customers", conversation_id="conv-clarify"
            )
        ]
        saved = await agent.conversation_store.get_conversation("conv-clarify", user)
        return components, saved

    async def test_end_turn_stops_the_loop_after_one_llm_call(
        self, make_agent, mock_llm, request_context
    ):
        # Without END_TURN the agent calls the model again, and it composes a
        # reply to its own question.
        await self._run(
            make_agent, mock_llm, request_context, RequestClarificationTool()
        )
        assert mock_llm.call_count == 1

    async def test_tool_result_is_still_recorded_before_stopping(
        self, make_agent, mock_llm, request_context
    ):
        """A tool_call stored without its result is rejected on the next turn."""
        _, saved = await self._run(
            make_agent, mock_llm, request_context, RequestClarificationTool()
        )

        tool_messages = [m for m in saved.messages if m.role == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call-1"

    async def test_input_is_re_enabled_so_the_user_can_reply(
        self, make_agent, mock_llm, request_context
    ):
        components, _ = await self._run(
            make_agent, mock_llm, request_context, RequestClarificationTool()
        )

        placeholders = [
            getattr(c.rich_component, "placeholder", None)
            for c in components
            if getattr(c, "rich_component", None) is not None
        ]
        assert any(p and "rephrase" in p for p in placeholders)

    async def test_the_card_reaches_the_user(
        self, make_agent, mock_llm, request_context
    ):
        components, _ = await self._run(
            make_agent, mock_llm, request_context, RequestClarificationTool()
        )

        cards = [
            c.rich_component
            for c in components
            if getattr(c, "rich_component", None) is not None
            and getattr(c.rich_component, "actions", None)
        ]
        assert cards and len(cards[0].actions) == 2

    async def test_a_failing_tool_cannot_end_the_turn(
        self, make_agent, mock_llm, request_context
    ):
        """END_TURN is honoured only on a successful result."""

        class _FailsButAsksToEnd(RequestClarificationTool):
            async def execute(self, context, args):
                result = await super().execute(context, args)
                return result.model_copy(
                    update={"success": False, "error": "deliberate"}
                )

        await self._run(
            make_agent,
            mock_llm,
            request_context,
            _FailsButAsksToEnd(),
            extra=LlmResponse(
                content="Sorry, something went wrong.", finish_reason="stop"
            ),
        )
        assert mock_llm.call_count == 2


_ARGS = {
    "question": "'Top' could mean by revenue or by order count.",
    "options": [
        "Which 10 customers spent the most in 2024?",
        "Which 10 customers placed the most orders in 2024?",
    ],
}
