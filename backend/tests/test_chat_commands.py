"""DataLensWorkflow: the app-layer `/memorise`, `/setup`, and the ungated `/delete`.

`Platform` wires `DataLensWorkflow` into every `Agent` it builds
(`vanna_app/platform.py`), so this is what actually answers those three
commands in production -- the vendored `DefaultWorkflowHandler` on its own
does not know about `/memorise` or `/setup`, and gates `/delete` on admin.

No Postgres needed: `DemoAgentMemory` is a dependency-free `AgentMemory`, and
a tiny fake tool registry stands in for the one `_analyze_setup` inspects.
"""

from __future__ import annotations

import pytest

from vanna.core.storage import Conversation
from vanna.core.tool import ToolSchema
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna_app.chat_commands import DataLensWorkflow


class _FakeRegistry:
    def __init__(self, tool_names):
        self._tool_names = tool_names

    async def get_schemas(self, user=None):
        return [
            ToolSchema(name=name, description=name, parameters={})
            for name in self._tool_names
        ]


class _FakeAgent:
    def __init__(self, tool_names=(), agent_memory=None):
        self.tool_registry = _FakeRegistry(tool_names)
        self.agent_memory = agent_memory


def _user(email: str = "viewer@acme.test", admin: bool = False) -> User:
    return User(
        id=email,
        email=email,
        tenant_id="acme",
        group_memberships=["admin"] if admin else [],
    )


def _conversation(user: User) -> Conversation:
    return Conversation(id="conv-1", user=user)


class TestMemorise:
    async def test_saves_text_and_reports_the_id(self):
        memory = DemoAgentMemory()
        agent = _FakeAgent(agent_memory=memory)
        user = _user()
        conversation = _conversation(user)
        workflow = DataLensWorkflow()

        result = await workflow.try_handle(
            agent, user, conversation, "/memorise revenue means net of refunds"
        )

        assert result.should_skip_llm is True
        text = result.components[0].rich_component.content
        assert "revenue means net of refunds" in text

        saved = await memory.get_recent_text_memories(
            _context(user, conversation, memory)
        )
        assert [m.content for m in saved] == ["revenue means net of refunds"]

    async def test_bare_memorise_does_not_save_and_explains_usage(self):
        memory = DemoAgentMemory()
        agent = _FakeAgent(agent_memory=memory)
        user = _user()
        conversation = _conversation(user)
        workflow = DataLensWorkflow()

        result = await workflow.try_handle(agent, user, conversation, "/memorise")

        assert result.should_skip_llm is True
        assert "Usage" in result.components[0].rich_component.content
        saved = await memory.get_recent_text_memories(
            _context(user, conversation, memory)
        )
        assert saved == []

    async def test_no_admin_gate(self):
        """A viewer must be able to record their own preference."""
        memory = DemoAgentMemory()
        agent = _FakeAgent(agent_memory=memory)
        user = _user(admin=False)
        conversation = _conversation(user)
        workflow = DataLensWorkflow()

        result = await workflow.try_handle(agent, user, conversation, "/memorise x")

        assert "Access Denied" not in result.components[0].rich_component.content


class TestSetup:
    async def test_reports_calculator_missing_by_default(self):
        agent = _FakeAgent(tool_names=["run_sql"])
        user = _user()
        conversation = _conversation(user)
        workflow = DataLensWorkflow()

        result = await workflow.try_handle(agent, user, conversation, "/setup")

        content = result.components[0].rich_component.content
        assert "Calculator" in content
        assert "❌" in content or "➖" in content

    async def test_reports_calculator_present_once_registered(self):
        agent = _FakeAgent(tool_names=["run_sql", "calculator"])
        user = _user()
        conversation = _conversation(user)
        workflow = DataLensWorkflow()

        result = await workflow.try_handle(agent, user, conversation, "/setup")

        content = result.components[0].rich_component.content
        assert "| Calculator | ✅ |" in content


class TestDeleteIsUngated:
    async def test_a_viewer_can_run_delete(self):
        memory = DemoAgentMemory()
        agent = _FakeAgent(agent_memory=memory)
        user = _user(admin=False)
        conversation = _conversation(user)
        workflow = DataLensWorkflow()

        saved = await memory.save_text_memory(
            "note", _context(user, conversation, memory)
        )

        result = await workflow.try_handle(
            agent, user, conversation, f"/delete {saved.memory_id}"
        )

        assert "Access Denied" not in result.components[0].rich_component.content
        assert "Deleted" in result.components[0].rich_component.content


def _context(user: User, conversation: Conversation, memory):
    from vanna.core.tool import ToolContext

    return ToolContext(
        user=user,
        conversation_id=conversation.id,
        request_id="test",
        agent_memory=memory,
    )
