"""search_knowledge: the escape hatch for mid-conversation knowledge lookups.

`RetrievalContextEnhancer` only injects examples and instructions for the
opening question, so a `TABLE`-scoped rule for a table discovered on turn
three never reaches the model any other way. These tests are about that gap,
plus the two properties that keep this tool from becoming a disclosure: a
`CANDIDATE` example never counts as precedent under `verified_only`, and
tenants never see each other's knowledge.
"""

from __future__ import annotations

from vanna.capabilities.knowledge import (
    ExampleStatus,
    Instruction,
    InstructionScope,
)
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.knowledge import LocalExampleStore, LocalInstructionStore
from vanna.tools.knowledge import SearchKnowledgeArgs, SearchKnowledgeTool


def _context(tenant: str = "acme", user_id: str = "u1") -> ToolContext:
    return ToolContext(
        user=User(id=user_id, email=f"{user_id}@{tenant}.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


class TestSearchKnowledgeTool:
    def test_no_sql_argument_fields(self):
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        assert SearchKnowledgeTool(examples, instructions).sql_argument_fields == ()

    async def test_verified_example_is_returned(self):
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        ctx = _context()
        await examples.add(
            ctx,
            "monthly revenue by region",
            "SELECT region, SUM(amount) FROM orders GROUP BY region",
            status=ExampleStatus.VERIFIED,
        )
        tool = SearchKnowledgeTool(examples, instructions)

        result = await tool.execute(
            ctx, SearchKnowledgeArgs(question="revenue by region")
        )

        assert result.success
        assert "SELECT region, SUM(amount)" in result.result_for_llm

    async def test_candidate_excluded_under_verified_only(self):
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        ctx = _context()
        await examples.add(
            ctx,
            "monthly revenue by region",
            "SELECT region, SUM(amount) FROM orders GROUP BY region",
            status=ExampleStatus.CANDIDATE,
        )
        tool = SearchKnowledgeTool(examples, instructions)

        result = await tool.execute(
            ctx,
            SearchKnowledgeArgs(question="revenue by region", verified_only=True),
        )

        assert "SUM(amount)" not in result.result_for_llm

    async def test_absence_is_announced_not_silent(self):
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        ctx = _context()
        tool = SearchKnowledgeTool(examples, instructions)

        result = await tool.execute(
            ctx, SearchKnowledgeArgs(question="anything at all")
        )

        assert "do not assume a precedent exists" in result.result_for_llm
        assert "do not assume an unstated one exists" in result.result_for_llm

    async def test_table_scoped_instruction_fires_for_its_table_and_not_another(self):
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        ctx = _context()
        await instructions.add(
            ctx,
            Instruction(
                text="orders.amount is stored in cents",
                scope=InstructionScope.TABLE,
                scope_ref="orders",
            ),
        )
        tool = SearchKnowledgeTool(examples, instructions)

        for_orders = await tool.execute(
            ctx, SearchKnowledgeArgs(question="revenue", tables=["orders"])
        )
        for_customers = await tool.execute(
            ctx, SearchKnowledgeArgs(question="revenue", tables=["customers"])
        )

        assert "stored in cents" in for_orders.result_for_llm
        assert "stored in cents" not in for_customers.result_for_llm

    async def test_cross_tenant_isolation(self):
        examples = LocalExampleStore()
        instructions = LocalInstructionStore()
        acme = _context(tenant="acme")
        globex = _context(tenant="globex")

        await examples.add(
            acme,
            "acme's secret metric",
            "SELECT 1",
            status=ExampleStatus.VERIFIED,
        )
        await instructions.add(
            acme, Instruction(text="acme-only business rule")
        )

        tool = SearchKnowledgeTool(examples, instructions)
        result = await tool.execute(
            globex, SearchKnowledgeArgs(question="secret metric")
        )

        assert "acme's secret metric" not in result.result_for_llm
        assert "acme-only business rule" not in result.result_for_llm
