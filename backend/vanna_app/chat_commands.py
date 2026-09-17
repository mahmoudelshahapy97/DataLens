"""App-layer chat slash commands.

`Platform` never passes `workflow_handler` to `Agent`, so it falls back to
`DefaultWorkflowHandler`. Subclassing here (rather than editing the vendored
`vanna.core.workflow.default`) keeps this app-specific behaviour out of the
library tree.
"""

import uuid

from vanna.components import RichTextComponent, UiComponent
from vanna.core.agent.agent import Agent
from vanna.core.storage import Conversation
from vanna.core.tool import ToolContext
from vanna.core.user.models import User
from vanna.core.workflow import WorkflowResult
from vanna.core.workflow.default import DefaultWorkflowHandler


class DataLensWorkflow(DefaultWorkflowHandler):
    """Adds `/memorise` and `/setup`, and drops the admin gate on `/delete`."""

    async def try_handle(
        self, agent: Agent, user: User, conversation: Conversation, message: str
    ) -> WorkflowResult:
        stripped = message.strip()

        if stripped.lower().startswith("/memorise"):
            return await self._memorise(agent, user, conversation, stripped)

        if stripped.lower() in ("/setup", "setup"):
            return await self._setup(agent, user)

        if stripped.lower().startswith("/delete "):
            memory_id = stripped[len("/delete ") :].strip()
            return await self._delete_memory(agent, user, conversation, memory_id)

        return await super().try_handle(agent, user, conversation, message)

    async def _memorise(
        self, agent: Agent, user: User, conversation: Conversation, stripped: str
    ) -> WorkflowResult:
        text = stripped[len("/memorise") :].strip()

        if not text:
            return WorkflowResult(
                should_skip_llm=True,
                components=[
                    UiComponent(
                        rich_component=RichTextComponent(
                            content="# 📝 Save a Memory\n\n"
                            "Tell me what to remember.\n\n"
                            "Usage: `/memorise <text>`",
                            markdown=True,
                        ),
                        simple_component=None,
                    )
                ],
            )

        if not hasattr(agent, "agent_memory") or agent.agent_memory is None:
            return WorkflowResult(
                should_skip_llm=True,
                components=[
                    UiComponent(
                        rich_component=RichTextComponent(
                            content="# ⚠️ No Memory System\n\n"
                            "Agent memory is not configured. I can't save that.",
                            markdown=True,
                        ),
                        simple_component=None,
                    )
                ],
            )

        context = ToolContext(
            user=user,
            conversation_id=conversation.id,
            request_id=str(uuid.uuid4()),
            agent_memory=agent.agent_memory,
        )
        memory = await agent.agent_memory.save_text_memory(text, context)

        return WorkflowResult(
            should_skip_llm=True,
            components=[
                UiComponent(
                    rich_component=RichTextComponent(
                        content=f"# ✅ Memory Saved\n\n"
                        f"I'll remember: “{text}”\n\n"
                        f"**ID:** `{memory.memory_id}`\n\n"
                        f"View it any time under `/memories` or on your Account page.",
                        markdown=True,
                    ),
                    simple_component=None,
                )
            ],
        )

    async def _setup(self, agent: Agent, user: User) -> WorkflowResult:
        tools = await agent.tool_registry.get_schemas(user)
        tool_names = [tool.name for tool in tools]
        analysis = self._analyze_setup(tool_names)

        content = "# 🛠️ Setup\n\n"
        content += f"**{analysis['tool_count']}** tools available.\n\n"
        content += "| Capability | Status |\n|---|---|\n"
        content += f"| SQL Connection | {'✅ Available' if analysis['has_sql'] else '❌ Missing (required)'} |\n"
        content += f"| Memory (search) | {'✅' if analysis['has_search'] else '❌'} |\n"
        content += f"| Memory (save) | {'✅' if analysis['has_save'] else '❌'} |\n"
        content += f"| Visualization | {'✅' if analysis['has_viz'] else '➖ Text/tables only'} |\n"
        content += f"| Calculator | {'✅' if analysis['has_calculator'] else '➖ Not available'} |\n\n"

        if analysis["is_complete"]:
            content += "Everything is configured — nothing to do.\n"
        else:
            missing = []
            if not analysis["has_sql"]:
                missing.append("a SQL tool (`RunSqlTool`) — required before I can query data")
            if not analysis["has_memory"]:
                missing.append(
                    "memory tools (`SearchSavedCorrectToolUses`, `SaveQuestionToolArgs`)"
                )
            if not analysis["has_viz"]:
                missing.append("a visualization tool (`VisualizeDataTool`)")
            if not analysis["has_calculator"]:
                missing.append("a calculator tool")
            content += "**Missing:**\n" + "\n".join(f"- {m}" for m in missing) + "\n"

        return WorkflowResult(
            should_skip_llm=True,
            components=[
                UiComponent(
                    rich_component=RichTextComponent(content=content, markdown=True),
                    simple_component=None,
                )
            ],
        )
