"""DataLens turn nodes: the steps whose policy is ours, not the library's.

``vanna/core/agent/graph`` defines what a node *is* and runs the graph. What a
node should judge, which model it should judge with, and when it is worth
spending a call on at all are product decisions, so they live here -- the same
split as ``chat_commands.py``, which subclasses the library's workflow handler
rather than editing it.

Two nodes: :class:`PlannerNode` before the model starts, :class:`CriticNode`
before its answer reaches the user.

**Why a node and not a tool.** Every other capability added recently is a tool,
because a tool costs nothing until the model reaches for it. Self-checking is
the case where that reasoning inverts: a model that has just produced a
confidently wrong answer is exactly the model that will not choose to
double-check it. The check has to be structural, which means it has to be a
node.

**What it costs.** One extra model call per data turn, and one extra full turn
when it rejects. That is not free and is not hidden: it runs only on turns that
actually executed a query and got rows, it is capped at
``max_critic_retries`` rejections, and it is off unless wired.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, AsyncGenerator, Optional

from vanna.core.agent.graph import TurnNode, TurnState
from vanna.core.llm.models import LlmMessage, LlmRequest
from vanna.components import (
    SimpleTextComponent,
    Task,
    TaskListComponent,
    UiComponent,
)
from vanna.core.storage import Message

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.llm.base import LlmService

logger = logging.getLogger(__name__)

#: Tools whose use means the turn produced data worth checking. A turn that
#: only searched the schema has no result for the critic to hold against the
#: question, so paying for a call would buy nothing.
_DATA_TOOLS = frozenset({"run_sql", "analyze_timeseries", "compare_periods"})

#: The critic answers with this exact word when the answer is sound. Asking for
#: one token rather than a JSON object keeps it cheap and keeps the failure
#: mode obvious: anything unparseable is treated as approval, because a broken
#: critic must not be able to block every answer in the product.
_OK = "OK"

_PROMPT = (
    "You are checking one answer produced by a data analyst, before the user "
    "sees it.\n\n"
    "Reply with exactly the word OK if the answer addresses the question "
    "asked. Otherwise reply with one sentence naming precisely what is "
    "missing or wrong.\n\n"
    "Reject only for these:\n"
    "- The answer reports a different measure, period or grouping than the "
    "question asked for.\n"
    "- The answer states a number the query result does not support.\n"
    "- The question asked for several things and the answer covers only some.\n\n"
    "Do not reject for style, brevity, formatting, or for not volunteering "
    "extra analysis. A short correct answer is correct."
)


class CriticNode(TurnNode):
    """Checks a data answer against the question before the user sees it.

    Sits on the edge between the model finishing and the answer being emitted,
    which is why ``Agent.answer_node`` exists: ``insert_after`` rewires static
    edges, and ``llm_turn`` names its successor directly.

    Args:
        llm_service: Used for the check. Pass a cheap model -- this is a
            yes/no judgement, not the analysis itself.
        answer_node: Where to forward once satisfied.
        max_retries: How many times it may send the turn back. One is the
            sensible default: a second rejection usually means the critic and
            the analyst disagree about the question, and looping on that burns
            the user's quota without converging.
    """

    name = "critic"

    def __init__(
        self,
        llm_service: "LlmService",
        *,
        answer_node: str = "answer",
        max_retries: int = 1,
    ) -> None:
        self.llm_service = llm_service
        self.answer_node = answer_node
        self.max_retries = max_retries

    # ------------------------------------------------------------------

    async def should_run(self, state: TurnState) -> bool:
        """Only on a turn that produced data, and only while retries remain.

        Entering is what costs a call, so every reason not to is checked here
        rather than inside :meth:`run`.
        """
        if self.max_retries <= 0:
            return False
        if state.notes.get("critic_retries", 0) >= self.max_retries:
            return False
        if not state.response or not state.response.content:
            # Nothing to check. The empty-response path has its own handling.
            return False
        return self._used_a_data_tool(state)

    @staticmethod
    def _used_a_data_tool(state: TurnState) -> bool:
        """Read from the transcript, so no built-in node had to be edited."""
        for message in state.conversation.messages:
            for call in message.tool_calls or []:
                if call.name in _DATA_TOOLS:
                    return True
        return False

    @staticmethod
    def _question(state: TurnState) -> Optional[str]:
        for message in reversed(state.conversation.messages):
            if message.role == "user" and message.content:
                return message.content
        return None

    async def run(self, state: TurnState) -> AsyncGenerator["UiComponent", None]:
        question = self._question(state)
        answer = (state.response.content or "") if state.response else ""

        verdict = await self._judge(state, question, answer)

        if verdict is None:
            # Approved, or the critic itself failed. Both forward: a critic
            # that cannot answer must not be able to hold up the product.
            state.goto = self.answer_node
            return

        attempts = state.notes.get("critic_retries", 0) + 1
        state.notes["critic_retries"] = attempts
        state.notes["critic_last"] = verdict
        logger.info("Critic sent a turn back (attempt %s): %s", attempts, verdict)

        # Appended as a user turn rather than a system one: providers treat a
        # mid-conversation system message inconsistently, and the analyst has
        # to act on this, which is what a user turn means.
        state.conversation.add_message(
            Message(
                role="user",
                content=(
                    "A reviewer checked your draft answer and found this "
                    f"problem: {verdict} Correct it and answer again. Query "
                    "again if you need to; do not apologise or mention this "
                    "review."
                ),
            )
        )

        # Cleared so llm_turn rebuilds against the history the critique was
        # just appended to, rather than replaying the request that produced
        # the answer being rejected.
        state.request = None
        state.goto = "llm_turn"
        return
        yield  # pragma: no cover - makes this an async generator

    async def _judge(
        self, state: TurnState, question: Optional[str], answer: str
    ) -> Optional[str]:
        """The critique, or None when the answer passes or the check fails."""
        if not question or not answer.strip():
            return None

        try:
            response = await self.llm_service.send_request(
                LlmRequest(
                    messages=[
                        LlmMessage(
                            role="user",
                            content=(
                                f"Question:\n{question}\n\nDraft answer:\n{answer}"
                            ),
                        )
                    ],
                    user=state.user,
                    system_prompt=_PROMPT,
                    temperature=0.0,
                    max_tokens=200,
                    stream=False,
                )
            )
        except Exception as e:
            # Deliberately swallowed. The critic is a safety net, and a torn
            # safety net is better than a blocked answer.
            logger.warning("Critic call failed, approving by default: %s", e)
            return None

        content = (response.content or "").strip()
        if not content or content.upper().startswith(_OK):
            return None
        return content


def build_critic_node(llm_service: "LlmService", *, max_retries: int = 1) -> TurnNode:
    """The critic, ready to be wired between `llm_turn` and `answer`."""
    return CriticNode(llm_service, max_retries=max_retries)


#: Words that suggest a question needs more than one query. Crude on purpose:
#: the alternative to a keyword list is a model call to decide whether to make
#: a model call, which costs the thing it is trying to save.
_MULTI_STEP_HINTS = (
    "compare",
    "versus",
    " vs ",
    "trend",
    "over time",
    "breakdown",
    "break down",
    "why",
    "correlat",
    "contribut",
    "drivers",
    "year on year",
    "month on month",
    "and also",
)

#: Below this many characters a question is almost never multi-step, whatever
#: words it happens to contain.
_MIN_QUESTION_LENGTH = 40

_PLAN_PROMPT = (
    "You are planning how a data analyst should answer one question, before "
    "any query is written.\n\n"
    "Reply with two to four steps, one per line, each starting with '- '. "
    "Each step is a thing to find out, not SQL. No preamble, no numbering, no "
    "closing remark.\n\n"
    "If the question needs only a single straightforward query, reply with "
    "exactly the word SIMPLE."
)

#: The planner says this when a question does not need it after all. The
#: keyword check cannot tell "compare these two numbers" from "compare these
#: two cohorts", so the model gets the last word.
_SIMPLE = "SIMPLE"


class PlannerNode(TurnNode):
    """Drafts an approach for a multi-step question, and shows it to the user.

    Runs before the first model call, so it is the entry node when wired.

    Costs one model call on every turn it fires, which is why the keyword gate
    in :meth:`should_run` is deliberately narrow: a planner that fires on
    "how many customers are there" is pure loss. Off by default.

    Args:
        llm_service: Used to draft. A cheap model is appropriate.
        next_node: Where to go once planned.
    """

    name = "planner"

    def __init__(
        self, llm_service: "LlmService", *, next_node: str = "llm_turn"
    ) -> None:
        self.llm_service = llm_service
        self.next_node = next_node

    async def should_run(self, state: TurnState) -> bool:
        if state.notes.get("planned"):
            return False  # the tool loop comes back through here; plan once
        question = _last_question(state)
        if not question or len(question) < _MIN_QUESTION_LENGTH:
            return False
        lowered = question.lower()
        return any(hint in lowered for hint in _MULTI_STEP_HINTS)

    async def run(self, state: TurnState) -> AsyncGenerator["UiComponent", None]:
        state.notes["planned"] = True
        question = _last_question(state) or ""

        steps = await self._draft(state, question)
        if steps:
            state.notes["plan"] = steps
            # Shown as tasks rather than prose so it reads as "here is what I
            # am about to do", and does not get mistaken for the answer.
            yield UiComponent(
                rich_component=TaskListComponent(
                    title="Approach",
                    tasks=[Task(title=step, status="pending") for step in steps],
                    show_progress=False,
                    show_timestamps=False,
                ),
                simple_component=SimpleTextComponent(
                    text="Approach:"
                    + chr(10)
                    + chr(10).join("  - " + step for step in steps)
                ),
            )
            state.conversation.add_message(
                Message(
                    role="user",
                    content=(
                        "Before answering, work to this approach:"
                        + chr(10)
                        + chr(10).join("- " + step for step in steps)
                        + chr(10)
                        + "Do not restate the plan to the user."
                    ),
                )
            )
            state.request = None  # the history just changed

        state.goto = self.next_node

    async def _draft(self, state: TurnState, question: str) -> list:
        try:
            response = await self.llm_service.send_request(
                LlmRequest(
                    messages=[LlmMessage(role="user", content=question)],
                    user=state.user,
                    system_prompt=_PLAN_PROMPT,
                    temperature=0.0,
                    max_tokens=300,
                    stream=False,
                )
            )
        except Exception as e:
            # Same reasoning as the critic: a planner that cannot answer must
            # not be able to stop the turn.
            logger.warning("Planner call failed, continuing unplanned: %s", e)
            return []

        content = (response.content or "").strip()
        if not content or content.upper().startswith(_SIMPLE):
            return []

        steps = [
            line.lstrip("- ").strip()
            for line in content.splitlines()
            if line.strip().startswith("-")
        ]
        # Two steps is the fewest that is a plan. One means the model agreed it
        # was simple without using the word.
        return steps[:4] if len(steps) >= 2 else []


def _last_question(state: TurnState) -> Optional[str]:
    for message in reversed(state.conversation.messages):
        if message.role == "user" and message.content:
            return message.content
    return None


def build_planner_node(llm_service: "LlmService") -> TurnNode:
    """The planner, ready to be wired as the graph's entry node."""
    return PlannerNode(llm_service)
