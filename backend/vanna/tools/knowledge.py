"""Reach into `ExampleStore` and `InstructionStore` after the opening question.

Both stores already feed the prompt via `RetrievalContextEnhancer`, but only
for the opening message of a conversation -- `InstructionStore.resolve` is
scope-driven and takes no query text (`capabilities/knowledge/base.py`), so a
`TABLE`-scoped rule for a table discovered on turn three of a conversation is
never resolved into context. This tool is the escape hatch for that gap, the
same way `search_tables`/`get_table_schema` are the escape hatch for schema
injection.

Both `Example` and `Instruction` are authoritative -- human-verified or
human-authored -- unlike `search_saved_correct_tool_uses`, which is advisory
and agent-written. The description says so explicitly, because three tools
in this codebase now start with "search past..." and the model has no other
way to tell them apart.
"""

from __future__ import annotations

from typing import List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.knowledge import ExampleStore, InstructionStore
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Truncated question length in the list view. Full text is not needed to let
#: the model decide whether to look closer -- it needs to decide whether to
#: look closer at all.
_QUESTION_PREVIEW = 120


class SearchKnowledgeArgs(BaseModel):
    """Arguments for search_knowledge."""

    question: str = Field(
        description="What you need verified examples or business rules for, "
        "in plain words."
    )
    tables: Optional[List[str]] = Field(
        default=None,
        description="Tables now in scope, if known (e.g. after get_table_schema). "
        "Table-scoped rules for these tables are resolved even if they were not "
        "shown at the start of the conversation.",
    )
    verified_only: bool = Field(
        default=True,
        description="Only return human-verified examples, not agent-captured "
        "candidates. Leave true unless verified examples returned nothing.",
    )


class SearchKnowledgeTool(Tool[SearchKnowledgeArgs]):
    """Looks up verified NL->SQL examples and business rules by scope.

    Args:
        example_store: Verified question/SQL pairs.
        instruction_store: Durable, scope-resolved business rules.
    """

    #: Takes plain-English text, never SQL.
    sql_argument_fields: tuple = ()

    def __init__(
        self, example_store: ExampleStore, instruction_store: InstructionStore
    ) -> None:
        self.example_store = example_store
        self.instruction_store = instruction_store

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "Search the workspace's AUTHORITATIVE, human-verified knowledge: "
            "verified example queries and business rules (instructions). This "
            "is not agent memory -- everything returned here was written or "
            "verified by a person. Use this mid-conversation, when the schema "
            "you were shown at the start does not cover a table you have since "
            "discovered, or when you need to check for a business rule about a "
            "specific table before writing SQL against it."
        )

    def get_args_schema(self) -> Type[SearchKnowledgeArgs]:
        return SearchKnowledgeArgs

    async def execute(
        self, context: ToolContext, args: SearchKnowledgeArgs
    ) -> ToolResult:
        sections: List[str] = []

        try:
            hits = await self.example_store.search(
                context, args.question, limit=5, verified_only=args.verified_only
            )
        except Exception as e:
            hits = []
            sections.append(f"Could not search verified examples: {e}")

        if hits:
            listed = "\n\n".join(
                f"Question: {hit.example.question}\nSQL:\n{hit.example.sql}"
                for hit in hits
            )
            sections.append(f"## Verified examples\n\n{listed}")
        else:
            sections.append(
                "## Verified examples\n\nNo verified examples are stored for "
                "this -- do not assume a precedent exists."
            )

        try:
            instructions = await self.instruction_store.resolve(
                context, tables=args.tables
            )
        except Exception as e:
            instructions = []
            sections.append(f"Could not resolve business rules: {e}")

        if instructions:
            listed = "\n".join(f"- {i.text}" for i in instructions)
            sections.append(f"## Business rules\n\n{listed}")
        else:
            sections.append(
                "## Business rules\n\nNo business rules apply here -- do not "
                "assume an unstated one exists."
            )

        text = "\n\n".join(sections)
        summary = f"{len(hits)} example(s), {len(instructions)} rule(s)"
        return ToolResult(
            success=True,
            result_for_llm=text,
            ui_component=UiComponent(
                rich_component=RichTextComponent(content=summary, markdown=False),
                simple_component=SimpleTextComponent(text=summary),
            ),
            metadata={
                "example_count": len(hits),
                "instruction_count": len(instructions),
            },
        )
