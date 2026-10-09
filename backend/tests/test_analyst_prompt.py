"""What the analyst system prompt tells the model it is allowed to do.

The builder composes its plan from the tools actually registered, which was
already covered by construction. What was not covered -- and what this file
pins -- is the other half of the prompt: the rules that decide whether a
question without a query behind it gets an answer at all.

The prompt used to say "Answer from the query result only" unconditionally, and
describe the job as querying the database. Read literally by a model, that is an
instruction to have nothing to say to "hello" or "what data do you have?", even
though the agent loop has always been free to answer in prose.
"""

from __future__ import annotations

import pytest

from vanna.core.system_prompt.analyst import AnalystSystemPromptBuilder
from vanna.core.tool.models import ToolSchema


@pytest.fixture
def user(chat_user_factory):
    return chat_user_factory()


def _schema(name: str) -> ToolSchema:
    return ToolSchema(name=name, description=f"The {name} tool.", parameters={})


class TestNonDataGuidance:
    @pytest.mark.asyncio
    async def test_present_even_with_no_tools_at_all(self, user):
        # An agent with no SQL tools still gets asked what it can do.
        prompt = await AnalystSystemPromptBuilder().build_system_prompt(user, [])

        assert "Questions that don't need a query" in prompt
        assert "Greetings and small talk" in prompt
        assert "Always reply with something" in prompt

    @pytest.mark.asyncio
    async def test_present_alongside_the_sql_plan(self, user):
        prompt = await AnalystSystemPromptBuilder(
            dialect="postgres"
        ).build_system_prompt(user, [_schema("run_sql")])

        assert "Questions that don't need a query" in prompt
        assert "SQL rules (postgres)" in prompt

    @pytest.mark.asyncio
    async def test_sql_rules_stay_absent_without_a_sql_tool(self, user):
        prompt = await AnalystSystemPromptBuilder().build_system_prompt(
            user, [_schema("calculator")]
        )

        assert "SQL rules" not in prompt
        assert "Questions that don't need a query" in prompt

    @pytest.mark.asyncio
    async def test_covers_follow_ups_on_data_already_returned(self, user):
        prompt = await AnalystSystemPromptBuilder().build_system_prompt(
            user, [_schema("run_sql")]
        )

        assert "Follow-ups about a result you already returned" in prompt


class TestAnswerRulesAreScoped:
    @pytest.mark.asyncio
    async def test_heading_names_its_precondition(self, user):
        prompt = await AnalystSystemPromptBuilder().build_system_prompt(
            user, [_schema("run_sql")]
        )

        assert "## Answering a data question (when you have a query result)" in prompt

    @pytest.mark.asyncio
    async def test_result_only_rule_is_conditional(self, user):
        prompt = await AnalystSystemPromptBuilder().build_system_prompt(
            user, [_schema("run_sql")]
        )

        assert "When you are answering from a query result" in prompt
        # The unconditional imperative is what silenced non-data questions.
        assert "Answer from the query result only." not in prompt

    @pytest.mark.asyncio
    async def test_toggle_still_removes_the_block(self, user):
        prompt = await AnalystSystemPromptBuilder(
            include_answer_rules=False
        ).build_system_prompt(user, [_schema("run_sql")])

        assert "Answering a data question" not in prompt
        # Turning off result-interpretation rules should not turn off the
        # model's licence to reply to a greeting.
        assert "Questions that don't need a query" in prompt


class TestPersona:
    @pytest.mark.asyncio
    async def test_default_persona_admits_non_data_questions(self, user):
        prompt = await AnalystSystemPromptBuilder().build_system_prompt(user, [])

        assert "your capabilities" in prompt
        assert "already returned in this conversation" in prompt

    @pytest.mark.asyncio
    async def test_base_prompt_still_short_circuits_everything(self, user):
        prompt = await AnalystSystemPromptBuilder(
            base_prompt="Only this."
        ).build_system_prompt(user, [_schema("run_sql")])

        assert prompt == "Only this."
