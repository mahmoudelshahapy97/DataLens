"""Hand an ambiguous question back, instead of guessing which one was meant.

"Show me top customers" does not say top by what, over what period, or how
many. The agent currently picks one reading and answers it confidently, and the
user has no way to tell a choice was made at all -- the answer to the wrong
question looks exactly like the answer to the right one.

Asking in prose does not fix it, because the model has to re-derive the options
on the next turn from its own sentence. So the options are buttons on a card:
each carries the fully-specified question as its action, and clicking it sends
that question as the next message. The disambiguation happens once, in the turn
that noticed the ambiguity.

This is deliberately a tool rather than a stage in the turn. A clarification
*stage* would have to judge every question's ambiguity before answering any of
them -- an extra model call on every turn, including the overwhelming majority
that are perfectly clear. As a tool it costs nothing until the model reaches
for it.

It sets ``END_TURN``, so the agent stops rather than calling the model again.
Without that the model receives "I asked the user X" and, having been told
never to end a turn silently, writes an answer to its own question.
"""

from __future__ import annotations

from typing import List, Type

from pydantic import BaseModel, Field

from vanna.components import CardComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import END_TURN, Tool, ToolContext, ToolResult

#: Options offered. Two is the fewest that constitutes a choice; beyond four
#: the user is reading a menu rather than recognising what they meant.
_MIN_OPTIONS = 2
_MAX_OPTIONS = 4

#: Button labels are truncated to this. The button still carries the whole
#: question as its action -- only the caption is shortened.
_MAX_LABEL = 60


class RequestClarificationArgs(BaseModel):
    """Arguments for request_clarification."""

    question: str = Field(
        description="What is ambiguous, in one short sentence. State the "
        "ambiguity itself, e.g. \"'Top' could mean by revenue or by order "
        'count." Do not apologise or restate the whole question.'
    )
    options: List[str] = Field(
        description="Two to four complete, self-contained questions the user "
        "might have meant. Each is sent verbatim as the next message when "
        "clicked, so each must stand alone without this conversation -- write "
        "'Which 10 customers spent the most in 2024?', never 'by revenue'.",
        min_length=_MIN_OPTIONS,
        max_length=_MAX_OPTIONS,
    )


class RequestClarificationTool(Tool[RequestClarificationArgs]):
    """Puts a multiple-choice question to the user and ends the turn."""

    #: Takes prose, never SQL.
    sql_argument_fields: tuple = ()

    @property
    def name(self) -> str:
        return "request_clarification"

    @property
    def description(self) -> str:
        return (
            "Ask the user which of two to four readings of their question they "
            "meant, as clickable options. Use this when a question is genuinely "
            "ambiguous in a way that changes the answer -- an unstated measure "
            "('top' by what?), an unstated period, or a word matching several "
            "columns. Prefer answering with a stated assumption when one "
            "reading is clearly most likely; use this when two readings are "
            "equally plausible and would give materially different answers. "
            "Never use it to ask permission or to confirm something you can "
            "already do."
        )

    def get_args_schema(self) -> Type[RequestClarificationArgs]:
        return RequestClarificationArgs

    async def execute(
        self, context: ToolContext, args: RequestClarificationArgs
    ) -> ToolResult:
        seen: List[str] = []
        for option in args.options:
            cleaned = " ".join(option.split())
            # Deduplicated because two identical buttons are not a choice, and
            # the schema's length check counts them as two.
            if cleaned and cleaned not in seen:
                seen.append(cleaned)

        if len(seen) < _MIN_OPTIONS:
            return ToolResult(
                success=False,
                result_for_llm=(
                    f"request_clarification needs at least {_MIN_OPTIONS} "
                    "distinct, non-empty options. Answer with a stated "
                    "assumption instead."
                ),
                ui_component=None,
                error="Not enough distinct options to offer.",
            )

        question = args.question.strip()

        return ToolResult(
            success=True,
            # Written for the *next* turn: the agent breaks on END_TURN, so the
            # model only ever reads this as history, after the user has chosen.
            result_for_llm=(
                f"Asked the user to choose between: {'; '.join(seen)}. "
                "The turn ended here and their reply will arrive as the next "
                "message. Do not answer on their behalf."
            ),
            ui_component=UiComponent(
                rich_component=CardComponent(
                    title="Which did you mean?",
                    content=question,
                    icon="❓",
                    actions=[
                        {
                            "label": _label(option),
                            "action": option,
                            "variant": "secondary",
                        }
                        for option in seen
                    ],
                ),
                # Clients rendering only the simple payload still get the
                # question and the options, just without the buttons.
                simple_component=SimpleTextComponent(
                    text=question + "\n" + "\n".join(f"  - {option}" for option in seen)
                ),
            ),
            metadata={END_TURN: True, "options": seen},
        )


def _label(option: str) -> str:
    """Buttons carry the whole question; the caption is what fits on one."""
    return (
        option
        if len(option) <= _MAX_LABEL
        else option[: _MAX_LABEL - 3].rstrip() + "..."
    )
