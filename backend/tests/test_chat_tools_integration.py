"""The four new tools (`632d71e`), driven through the real chat pipeline.

`test_calculator.py`, `test_knowledge_tool.py`, `test_query_history_tool.py`,
and `test_value_dictionary_tool.py` each test a tool's `execute()` in
isolation, by calling it directly or through a bare `ToolRegistry.execute`.
None of them prove the integration that actually matters for chat: that the
tool's schema reaches the LLM, the LLM's `tool_calls` response is parsed and
routed to the right tool, and the tool's result is folded back into the
model's final answer. These tests drive that whole path via a real `Agent`
wired to `MockLlmService`.
"""

from __future__ import annotations

import pytest

from vanna.capabilities.knowledge import ExampleStatus
from vanna.capabilities.values import ReviewStatus, SampledValue
from vanna.core.generation import GenerationStatus, LocalGenerationStore, SqlGeneration
from vanna.core.llm import LlmResponse
from vanna.core.tool import ToolCall
from vanna.integrations.local import MemoryValueStore
from vanna.integrations.local.knowledge import LocalExampleStore, LocalInstructionStore
from vanna.tools.calculator import CalculatorTool
from vanna.tools.knowledge import SearchKnowledgeTool
from vanna.tools.query_history import SearchQueryHistoryTool
from vanna.tools.value_dictionary import ListKnownValuesTool


class _AllVisibleCatalog:
    """No `get_table`/`column_uses` at all -- everything stays visible."""


def _tool_call_response(tool_name: str, **arguments) -> LlmResponse:
    return LlmResponse(
        content=None,
        tool_calls=[ToolCall(id="call-1", name=tool_name, arguments=arguments)],
        finish_reason="tool_calls",
    )


async def _run(agent, request_context, message):
    components = []
    async for component in agent.send_message(request_context, message):
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


class TestCalculatorThroughChat:
    async def test_llm_invoked_calculator_result_reaches_the_answer(
        self, make_agent, mock_llm, request_context
    ):
        mock_llm.queue_response(_tool_call_response("calculator", expression="21 * 2"))
        mock_llm.queue_response(LlmResponse(content="That's 42.", finish_reason="stop"))
        agent, _ = make_agent(llm=mock_llm, tools=[(CalculatorTool(), [])])

        components = await _run(agent, request_context, "what is 21 times 2?")

        assert "That's 42." in _all_text(components)


class TestKnowledgeThroughChat:
    async def test_verified_example_surfaces_in_the_answer(
        self, make_agent, mock_llm, request_context, chat_user_factory
    ):
        user = chat_user_factory(tenant_id="acme")
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        from vanna.core.tool import ToolContext
        from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

        await examples.add(
            ToolContext(
                user=user,
                conversation_id="seed",
                request_id="seed",
                tenant_id="acme",
                agent_memory=DemoAgentMemory(),
            ),
            "monthly revenue by region",
            "SELECT region, SUM(amount) FROM orders GROUP BY region",
            status=ExampleStatus.VERIFIED,
        )
        mock_llm.queue_response(
            _tool_call_response("search_knowledge", question="revenue by region")
        )
        mock_llm.queue_response(
            LlmResponse(
                content="Here's a known-good query for that.", finish_reason="stop"
            )
        )
        agent, _ = make_agent(
            llm=mock_llm,
            user=user,
            tools=[(SearchKnowledgeTool(examples, instructions), [])],
        )

        components = await _run(agent, request_context, "how do I get revenue by region?")

        assert "Here's a known-good query" in _all_text(components)


class TestQueryHistoryThroughChat:
    async def test_own_recent_query_surfaces_without_leaking_sql(
        self, make_agent, mock_llm, request_context, chat_user_factory
    ):
        user = chat_user_factory(tenant_id="acme")
        store = LocalGenerationStore()
        from vanna.core.tool import ToolContext
        from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

        await store.record(
            ToolContext(
                user=user,
                conversation_id="seed",
                request_id="seed",
                tenant_id="acme",
                agent_memory=DemoAgentMemory(),
            ),
            SqlGeneration(
                question="revenue last quarter",
                sql="SELECT * FROM orders",
                status=GenerationStatus.VALID,
                row_count=12,
            ),
        )
        mock_llm.queue_response(_tool_call_response("search_query_history"))
        mock_llm.queue_response(
            LlmResponse(content="You asked about revenue last quarter.", finish_reason="stop")
        )
        agent, _ = make_agent(
            llm=mock_llm,
            user=user,
            tools=[(SearchQueryHistoryTool(store, _AllVisibleCatalog()), [])],
        )

        components = await _run(agent, request_context, "what did I ask before?")

        assert "revenue last quarter" in _all_text(components)
        assert "SELECT * FROM orders" not in _all_text(components)


class TestValueDictionaryThroughChat:
    async def test_approved_value_surfaces_pending_does_not(
        self, make_agent, mock_llm, request_context, chat_user_factory
    ):
        user = chat_user_factory(tenant_id="acme")
        store = MemoryValueStore()
        from vanna.core.tool import ToolContext
        from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

        ctx = ToolContext(
            user=user,
            conversation_id="seed",
            request_id="seed",
            tenant_id="acme",
            agent_memory=DemoAgentMemory(),
        )
        await store.record_samples(
            ctx,
            [
                SampledValue(table="orders", column="status", value="COMPLETE"),
                SampledValue(table="orders", column="status", value="SHADOW_PENDING"),
            ],
        )
        await store.set_status(
            ctx, table="orders", column="status", values=["COMPLETE"],
            status=ReviewStatus.APPROVED,
        )
        mock_llm.queue_response(
            _tool_call_response("list_known_values", table="orders", column="status")
        )
        mock_llm.queue_response(
            LlmResponse(content="Status is one of: COMPLETE.", finish_reason="stop")
        )
        agent, _ = make_agent(
            llm=mock_llm,
            user=user,
            tools=[(ListKnownValuesTool(store, _AllVisibleCatalog()), [])],
        )

        components = await _run(agent, request_context, "what statuses exist?")

        text = _all_text(components)
        assert "Status is one of: COMPLETE." in text
