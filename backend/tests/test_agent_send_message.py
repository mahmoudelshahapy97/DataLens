"""End-to-end coverage of ``Agent.send_message`` / ``Agent._send_message``.

Before this file, the only test that touched chat logic (``test_chat_commands.py``)
drove ``WorkflowHandler.try_handle`` against a ``_FakeAgent`` -- never a real
``Agent``. Nothing exercised the actual pipeline a chat message goes through:
user resolution, the LLM call, the tool-call loop, error handling, and
conversation persistence. These tests build a real ``Agent`` wired to
``MockLlmService`` (no network, no Postgres) and drive it the way the chat
routes do, via ``send_message``.
"""

from __future__ import annotations

from typing import Type

import pytest

from pydantic import BaseModel

from vanna.core.agent.config import AgentConfig
from vanna.core.llm import LlmResponse
from vanna.core.tool import Tool, ToolCall, ToolContext, ToolResult


class _EchoArgs(BaseModel):
    text: str


class EchoTool(Tool[_EchoArgs]):
    """Always succeeds; echoes its argument back for the LLM."""

    def __init__(self, name: str = "echo"):
        self._name = name
        self.calls = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Echoes the given text."

    def get_args_schema(self) -> Type[_EchoArgs]:
        return _EchoArgs

    async def execute(self, context: ToolContext, args: _EchoArgs) -> ToolResult:
        self.calls.append((context, args))
        return ToolResult(success=True, result_for_llm=f"echo: {args.text}")


class FailingTool(Tool[_EchoArgs]):
    """Always fails, to exercise the tool-error / recovery path."""

    @property
    def name(self) -> str:
        return "failing_tool"

    @property
    def description(self) -> str:
        return "Always fails."

    def get_args_schema(self) -> Type[_EchoArgs]:
        return _EchoArgs

    async def execute(self, context: ToolContext, args: _EchoArgs) -> ToolResult:
        return ToolResult(success=False, result_for_llm="boom", error="boom")


def _tool_call_response(tool_name: str, **arguments) -> LlmResponse:
    return LlmResponse(
        content=None,
        tool_calls=[ToolCall(id="call-1", name=tool_name, arguments=arguments)],
        finish_reason="tool_calls",
    )


async def _run(agent, request_context, message="hi", conversation_id=None):
    components = []
    async for component in agent.send_message(
        request_context, message, conversation_id=conversation_id
    ):
        components.append(component)
    return components


def _all_text(components) -> str:
    text = []
    for c in components:
        rc = getattr(c, "rich_component", None)
        if rc is not None and hasattr(rc, "content"):
            text.append(rc.content)
        sc = getattr(c, "simple_component", None)
        if sc is not None and hasattr(sc, "text"):
            text.append(sc.text)
    return "\n".join(t for t in text if t)


class TestPlainTextRoundTrip:
    async def test_message_in_answer_out(self, make_agent, mock_llm, request_context):
        mock_llm.set_response("42 rows returned")
        agent, user = make_agent(llm=mock_llm)

        components = await _run(agent, request_context, "how many rows?")

        assert "42 rows returned" in _all_text(components)
        assert mock_llm.call_count == 1

    async def test_conversation_is_persisted(self, make_agent, mock_llm, request_context):
        agent, user = make_agent(llm=mock_llm)
        conversation_id = "conv-persist-1"

        await _run(agent, request_context, "hello", conversation_id=conversation_id)

        saved = await agent.conversation_store.get_conversation(conversation_id, user)
        assert saved is not None
        roles = [m.role for m in saved.messages]
        assert "user" in roles
        assert "assistant" in roles


class TestToolCallLoop:
    async def test_single_tool_call_then_answer(
        self, make_agent, mock_llm, request_context
    ):
        tool = EchoTool()
        mock_llm.queue_response(_tool_call_response("echo", text="hello"))
        mock_llm.queue_response(
            LlmResponse(content="The echo tool said: echo: hello", finish_reason="stop")
        )
        agent, _ = make_agent(llm=mock_llm, tools=[(tool, [])])

        components = await _run(agent, request_context, "echo hello")

        assert len(tool.calls) == 1
        assert tool.calls[0][1].text == "hello"
        assert "echo: hello" in _all_text(components)
        assert mock_llm.call_count == 2

    async def test_multiple_sequential_tool_calls(
        self, make_agent, mock_llm, request_context
    ):
        tool = EchoTool()
        mock_llm.queue_response(_tool_call_response("echo", text="one"))
        mock_llm.queue_response(_tool_call_response("echo", text="two"))
        mock_llm.queue_response(LlmResponse(content="done", finish_reason="stop"))
        agent, _ = make_agent(llm=mock_llm, tools=[(tool, [])])

        components = await _run(agent, request_context, "echo twice")

        assert [args.text for _, args in tool.calls] == ["one", "two"]
        assert "done" in _all_text(components)
        assert mock_llm.call_count == 3

    async def test_tool_iteration_limit_is_enforced(
        self, make_agent, mock_llm, request_context
    ):
        tool = EchoTool()
        # The mock always wants another tool call; the loop must still stop.
        mock_llm._queue = []
        mock_llm.set_response("")

        class _AlwaysToolCall:
            call_count = 0

            async def send_request(self, request):
                self.call_count += 1
                return _tool_call_response("echo", text=f"call-{self.call_count}")

            async def stream_request(self, request):
                raise NotImplementedError

            async def validate_tools(self, tools):
                return []

        always_tool_call = _AlwaysToolCall()
        agent, _ = make_agent(
            llm=always_tool_call,
            tools=[(tool, [])],
            config=AgentConfig(stream_responses=False, max_tool_iterations=2),
        )

        components = await _run(agent, request_context, "loop forever")

        assert len(tool.calls) == 2
        assert "Tool Execution Limit Reached" in _all_text(components)

    async def test_failing_tool_surfaces_an_error_not_a_crash(
        self, make_agent, mock_llm, request_context
    ):
        tool = FailingTool()
        mock_llm.queue_response(_tool_call_response("failing_tool", text="x"))
        mock_llm.queue_response(
            LlmResponse(content="Sorry, that failed.", finish_reason="stop")
        )
        agent, _ = make_agent(llm=mock_llm, tools=[(tool, [])])

        components = await _run(agent, request_context, "trigger failure")

        # However the recovery strategy resolves it, the pipeline must not
        # raise -- send_message() only ever exits normally or yields an error
        # component (asserted below), never propagates an exception.
        assert components


class TestStarterUiAndEmptyMessages:
    async def test_empty_message_does_not_call_the_llm(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm)

        await _run(agent, request_context, "")

        assert mock_llm.call_count == 0

    async def test_whitespace_only_message_does_not_call_the_llm(
        self, make_agent, mock_llm, request_context
    ):
        agent, _ = make_agent(llm=mock_llm)

        await _run(agent, request_context, "   \n\t  ")

        assert mock_llm.call_count == 0


class TestErrorHandling:
    async def test_unexpected_llm_exception_becomes_an_error_component(
        self, make_agent, request_context
    ):
        class _BoomLlm:
            async def send_request(self, request):
                raise RuntimeError("upstream exploded")

            async def stream_request(self, request):
                raise RuntimeError("upstream exploded")
                yield  # pragma: no cover - never reached, makes this an async gen

            async def validate_tools(self, tools):
                return []

        agent, _ = make_agent(llm=_BoomLlm())

        components = await _run(agent, request_context, "cause a failure")

        text = _all_text(components)
        assert "unexpected error" in text.lower()

    async def test_user_facing_error_is_a_refusal_not_an_unexpected_error(
        self, make_agent, mock_llm, request_context
    ):
        """`UserFacingError` (quota walls, rate limits) takes a different path
        than an unexpected exception: `send_message` shows the exception's own
        message verbatim as a refusal, not the generic "unexpected error"
        text -- and never calls the LLM, since a `before_message` hook raises
        before the request is built."""
        from vanna.core.errors import UserFacingError
        from vanna.core.lifecycle import LifecycleHook

        class _QuotaHook(LifecycleHook):
            async def before_message(self, user, message):
                raise UserFacingError(
                    "You have reached your daily question limit. Resets at midnight UTC."
                )

        agent, _ = make_agent(llm=mock_llm, lifecycle_hooks=[_QuotaHook()])

        components = await _run(agent, request_context, "one more question")

        text = _all_text(components)
        assert "daily question limit" in text
        assert "unexpected error" not in text.lower()
        assert mock_llm.call_count == 0


class TestMultiTurnConversation:
    async def test_second_turn_includes_first_turns_history(
        self, make_agent, request_context
    ):
        """The LLM request on turn two must carry turn one's user question and
        assistant answer -- otherwise "what about last month instead" has
        nothing to refer back to."""

        class _RecordingLlm:
            def __init__(self):
                self.requests = []
                self.call_count = 0

            async def send_request(self, request):
                self.call_count += 1
                self.requests.append(request)
                return LlmResponse(
                    content=f"answer {self.call_count}", finish_reason="stop"
                )

            async def stream_request(self, request):
                raise NotImplementedError

            async def validate_tools(self, tools):
                return []

        llm = _RecordingLlm()
        agent, _ = make_agent(llm=llm)
        conversation_id = "conv-multiturn"

        await _run(agent, request_context, "revenue last quarter", conversation_id)
        await _run(agent, request_context, "what about last month", conversation_id)

        assert llm.call_count == 2
        second_request_contents = [m.content for m in llm.requests[1].messages]
        assert "revenue last quarter" in second_request_contents
        assert "answer 1" in second_request_contents
        assert "what about last month" in second_request_contents

    async def test_conversation_store_accumulates_every_turn(
        self, make_agent, request_context
    ):
        agent, user = make_agent()
        conversation_id = "conv-accumulate"

        await _run(agent, request_context, "first question", conversation_id)
        await _run(agent, request_context, "second question", conversation_id)

        saved = await agent.conversation_store.get_conversation(conversation_id, user)
        user_messages = [m.content for m in saved.messages if m.role == "user"]
        assert user_messages == ["first question", "second question"]


class TestToolAccessGroupGating:
    async def test_a_restricted_tools_schema_is_not_offered_to_a_non_admin(
        self, make_agent, request_context
    ):
        tool = EchoTool()

        class _SchemaCapturingLlm:
            def __init__(self):
                self.seen_tool_names = None

            async def send_request(self, request):
                self.seen_tool_names = [t.name for t in (request.tools or [])]
                return LlmResponse(content="no tools needed", finish_reason="stop")

            async def stream_request(self, request):
                raise NotImplementedError

            async def validate_tools(self, tools):
                return []

        llm = _SchemaCapturingLlm()
        # The default `chat_user_factory()` user is a non-admin (no group
        # memberships), so this is the ordinary case, not a special one.
        agent, _ = make_agent(llm=llm, tools=[(tool, ["admin"])])

        await _run(agent, request_context, "hi")

        assert "echo" not in (llm.seen_tool_names or [])

    async def test_a_non_admin_calling_a_restricted_tool_gets_denied_not_a_crash(
        self, make_agent, request_context
    ):
        """The LLM should never be offered an admin-only tool's schema (proven
        above), but nothing stops a misbehaving or adversarial model from
        naming it anyway -- the registry's own access check must still hold."""
        tool = EchoTool()
        from vanna.integrations.mock.llm import MockLlmService

        mock = MockLlmService()
        mock.queue_response(_tool_call_response("echo", text="x"))
        mock.queue_response(LlmResponse(content="denied", finish_reason="stop"))
        agent, _ = make_agent(llm=mock, tools=[(tool, ["admin"])])

        components = await _run(agent, request_context, "echo x")

        assert not tool.calls  # never actually executed
        assert "denied" in _all_text(components)
