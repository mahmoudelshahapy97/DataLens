"""The tool a model calls to change data.

Two tools rather than one, and deliberately so. ``propose_write`` accepts a
typed plan and returns an approval card; ``confirm_write`` takes a decision on a
plan by id. Splitting them is what makes the confirmation real: a single tool
with a ``confirm`` flag lets one inference both propose a change and declare it
approved, which is the failure this design exists to prevent. Two tools mean the
approval has to come from somewhere the model does not control.

Note also what ``propose_write`` does *not* accept: a SQL string. Its argument
schema is the :class:`WritePlan` itself, so the model's only way to express a
change is the typed one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from vanna.components import (
    ComponentType,
    NotificationComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.tool import Tool, ToolContext, ToolResult
from vanna.core.write import WritePlan, WriteRefusal, describe_write_policy
from vanna.core.write.approval import WriteStatus
from vanna.core.write.service import WriteService

logger = logging.getLogger(__name__)


class ProposeWriteTool(Tool[WritePlan]):
    """Propose a change to the data, for a person to approve."""

    def __init__(
        self,
        service: WriteService,
        *,
        custom_tool_name: Optional[str] = None,
    ) -> None:
        self.service = service
        self._custom_name = custom_tool_name

    @property
    def name(self) -> str:
        return self._custom_name or "propose_write"

    @property
    def description(self) -> str:
        return (
            "Propose a change to the data -- adding, updating or deleting rows. "
            "Nothing is changed when you call this: it returns the exact "
            "statement and how many rows it would affect, for the user to "
            "approve.\n\n"
            "Describe the change as structured steps, never as SQL. Each step "
            "names one table, the columns to set, and -- for an update or "
            "delete -- which rows, by primary key only.\n\n"
            "Rules that will otherwise refuse your plan:\n"
            "- An update or delete MUST address rows by primary key. If you do "
            "not know the key, run a SELECT first to find it, or ask.\n"
            "- Never assign a generated or computed column.\n"
            "- An insert must supply every column that has no default and "
            "cannot be null.\n"
            "- Values are data, not expressions: you cannot write "
            "'quantity + 1'. Read the current value first, then set the result.\n"
            "- expected_row_count is a promise checked inside the transaction. "
            "If it is wrong the whole change is rolled back, so state what you "
            "actually believe rather than what you hope.\n"
            "- To create a row and then refer to it, use two steps and take the "
            "child's foreign key from the parent step with a reference.\n\n"
            "Do not call this to answer a question. Only call it when the user "
            "has asked for something to change."
        )

    def get_args_schema(self) -> Type[WritePlan]:
        return WritePlan

    async def execute(self, context: ToolContext, args: WritePlan) -> ToolResult:
        try:
            pending, validated = await self.service.propose(context, args)
        except WriteRefusal as refusal:
            return self._refused(context, refusal)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Could not propose a write")
            message = f"The change could not be prepared: {exc}"
            return ToolResult(
                success=False,
                result_for_llm=message,
                ui_component=_notify("error", message),
                error=str(exc),
                metadata={"error_type": "write_error"},
            )

        awaiting = pending.status is WriteStatus.AWAITING_REVIEW
        instruction = (
            "Show the user what this would do, in their own words, and tell "
            "them another administrator has to approve it. Do not call "
            "propose_write again for the same change."
            if awaiting
            else "Show the user what this would do, in their own words, and ask "
            "them to confirm. Nothing has been changed. Do not call "
            "propose_write again for the same change."
        )

        return ToolResult(
            success=True,
            result_for_llm=(
                f"{pending.describe()}\n\n"
                f"Reference: {pending.id}\n\n{instruction}"
            ),
            ui_component=UiComponent(
                rich_component=NotificationComponent(
                    type=ComponentType.NOTIFICATION,
                    level="warning" if pending.is_destructive else "info",
                    message=pending.describe(),
                ),
                simple_component=SimpleTextComponent(text=pending.describe()),
            ),
            metadata={
                "pending_write_id": pending.id,
                "requires_confirmation": True,
                "awaiting_second_approval": awaiting,
                "operation": pending.operation,
                "tables": pending.tables,
                "expected_row_count": pending.expected_row_count,
                "is_destructive": pending.is_destructive,
                # The parameterized preview. Never the bound values.
                "statement_preview": pending.statement_preview,
                "plan_hash": pending.plan_hash,
            },
        )

    def _refused(self, context: ToolContext, refusal: WriteRefusal) -> ToolResult:
        """A refused write is a completed turn, not a failure.

        ``success=True`` on purpose. Returning False here would send the agent
        into error recovery, which would retry the same refusal -- and would
        render in the console as something broken rather than as an answer. The
        change did not happen, the user gets told why, and that is a finished
        conversation.
        """
        guidance = (
            "Tell the user this in your own words. "
            + (
                "Then, if a corrected plan would be allowed, propose that instead."
                if refusal.repairable
                else "Do not retry: this will be refused again."
            )
        )
        return ToolResult(
            success=True,
            result_for_llm=f"The change was NOT made. {refusal.message}\n\n{guidance}",
            ui_component=_notify("warning", refusal.message),
            metadata={
                "refused": True,
                "code": refusal.code.value,
                "repairable": refusal.repairable,
            },
        )


class ConfirmWriteArgs(BaseModel):
    """Arguments for confirm_write."""

    pending_write_id: str = Field(
        description="The reference returned by propose_write."
    )
    approved: bool = Field(
        description="True only if the user has explicitly said yes to this "
        "specific change. Never infer approval from enthusiasm about the "
        "original question."
    )


class ConfirmWriteTool(Tool[ConfirmWriteArgs]):
    """Carry out a change the user has approved."""

    def __init__(self, service: WriteService) -> None:
        self.service = service

    @property
    def name(self) -> str:
        return "confirm_write"

    @property
    def description(self) -> str:
        return (
            "Carry out a change previously returned by propose_write, once the "
            "user has approved it. Call with approved=false if they declined.\n\n"
            "Only call this after the user has said yes to this specific "
            "change, in this conversation. The change is re-checked against "
            "current permissions before it runs, so an approval that has gone "
            "stale will be refused rather than applied."
        )

    def get_args_schema(self) -> Type[ConfirmWriteArgs]:
        return ConfirmWriteArgs

    async def execute(
        self, context: ToolContext, args: ConfirmWriteArgs
    ) -> ToolResult:
        try:
            decided, result = await self.service.decide_and_execute(
                context, args.pending_write_id, approve=args.approved
            )
        except WriteRefusal as refusal:
            return ToolResult(
                success=True,
                result_for_llm=(
                    f"The change was NOT made. {refusal.message}\n\n"
                    "Tell the user this in your own words."
                ),
                ui_component=_notify("warning", refusal.message),
                metadata={"refused": True, "code": refusal.code.value},
            )

        if not args.approved:
            message = "The change was declined. Nothing was modified."
            return ToolResult(
                success=True,
                result_for_llm=message,
                ui_component=_notify("info", message),
                metadata={"pending_write_id": decided.id, "status": "rejected"},
            )

        rows = result.rows_affected if result else 0
        noun = "row" if rows == 1 else "rows"
        message = f"Done. {rows} {noun} changed in {', '.join(decided.tables)}."
        return ToolResult(
            success=True,
            result_for_llm=message,
            ui_component=_notify("success", message),
            metadata={
                "pending_write_id": decided.id,
                "status": "executed",
                "rows_affected": rows,
                "tables": decided.tables,
            },
        )


def create_write_tools(service: WriteService) -> list:
    """Both halves of the write flow.

    Register with an ``access_groups`` list so the tools never appear in the
    schema shown to callers who could not use them::

        for tool in create_write_tools(service):
            registry.register_local_tool(tool, ["admin"])
    """
    return [ProposeWriteTool(service), ConfirmWriteTool(service)]


def write_capability_note(policy) -> str:
    """A line for the system prompt describing what may be changed."""
    return describe_write_policy(policy)


def _notify(level: str, message: str) -> UiComponent:
    return UiComponent(
        rich_component=NotificationComponent(
            type=ComponentType.NOTIFICATION, level=level, message=message
        ),
        simple_component=SimpleTextComponent(text=message),
    )
