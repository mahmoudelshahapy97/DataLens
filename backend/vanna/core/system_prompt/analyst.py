"""A system prompt that adapts its plan to the resources actually available.

Dataherald ships four prompt variants and picks one by which resources exist
(examples + instructions / instructions only / examples only / neither). The
insight worth taking is not the specific wording -- it is that **a plan
referencing a tool the agent does not have is worse than no plan**. It burns
tokens, and it invites the model to hallucinate a tool call that will fail.

So the plan here is *composed*, not selected: each step is emitted only when
the capability behind it is registered. An agent with no schema catalog gets no
"look up the schema" step.

The second departure from the reference is about where retrieval happens. The
existing ``DefaultSystemPromptBuilder`` spends roughly forty lines instructing
the model to call a search tool first ("you MUST", "Do NOT skip"). When
``RetrievalContextEnhancer`` is wired up, that context is already in the prompt
before the model reads a word, so those instructions are not just unnecessary
-- they cause a wasted round trip. This builder assumes retrieval is
deterministic and spends the space on SQL quality rules instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from .base import SystemPromptBuilder

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..tool.models import ToolSchema
    from ..user.models import User


#: SQL correctness rules, distilled from Dataherald's evaluator checklist.
#: Stated as instructions rather than as things to avoid -- "do X" is followed
#: more reliably than "don't do Y", which merely raises Y's salience.
SQL_QUALITY_RULES = [
    "Write dialect-correct SQL for the target database and nothing else.",
    "Use explicit date literals from the system_time tool. Never call NOW(), "
    "CURRENT_DATE, or similar -- results must be identical if the query is "
    "re-run tomorrow.",
    "Filter out NULLs with IS NOT NULL when they would distort a result.",
    "Use UNION ALL rather than UNION unless duplicates must genuinely be "
    "removed; UNION performs a costly extra sort.",
    "Remember BETWEEN is inclusive at both ends. Use < and > when the "
    "endpoints should be excluded.",
    "Match column types in comparisons; cast explicitly rather than relying "
    "on implicit conversion.",
    "Quote identifiers containing spaces, reserved words, or special "
    "characters.",
    "Compare string values case-insensitively (e.g. LOWER(col) = LOWER('x')) "
    "unless the column's exact values are known.",
    "Select the columns the question actually asks for -- not SELECT * -- and "
    "aggregate in SQL rather than returning raw rows for counting.",
    "Add a brief inline comment to each non-obvious clause, so the user can "
    "check your reasoning without knowing SQL.",
]

#: Rules for turning a result set into prose. The "say you don't know" clause
#: matters most: a model asked to summarise a result that does not answer the
#: question will otherwise produce a fluent, confident non-answer, which is
#: worse than an admission because the user cannot tell it apart from a real
#: one.
ANSWER_RULES = [
    "When you are answering from a query result, answer from that result only. "
    "Never supplement it with recalled facts about the domain.",
    "If the result does not answer the question, say so plainly and explain "
    "what would be needed. Do not fill the gap with a plausible-sounding "
    "answer.",
    "If a result was capped or truncated, say the answer is partial. Never "
    "state a total, maximum, or 'top N' from a truncated result.",
    "The raw table is already shown to the user -- interpret it, don't repeat "
    "it.",
    "State the units and the time range you actually used.",
]

#: Rules for the questions that are not data questions. Without these the prompt
#: is all query-plan and result-interpretation, and the model reads that as its
#: whole job -- so a greeting or "what can you do?" gets an unnecessary query, or
#: a refusal, or nothing. The agent has always been free to answer in prose; this
#: block is what tells the model so.
NON_DATA_RULES = [
    "Greetings and small talk: reply briefly and naturally, then offer to help "
    "with a question about the data. Do not run a query.",
    "'What can you do?', 'what data do you have?', 'which tables exist?': "
    "answer from the schema and tools described above. Name the subject areas "
    "you can query and give one or two example questions. Do not invent tables "
    "or columns you have not been shown.",
    "Follow-ups about a result you already returned ('what does that column "
    "mean?', 'why is that number low?'): answer from what is already in the "
    "conversation, and re-query only if the answer genuinely needs data you do "
    "not already have.",
    "Questions the database cannot answer (opinion, general knowledge, another "
    "system's data): say plainly that it is outside what this database covers, "
    "and suggest the closest question you can answer.",
    "Always reply with something. Never end a turn silently, and never reply "
    "only with a tool result.",
]


class AnalystSystemPromptBuilder(SystemPromptBuilder):
    """Builds a plan-driven system prompt scaled to the registered tools.

    Args:
        persona: Opening line describing the assistant.
        dialect: SQL dialect name, injected into the rules.
        base_prompt: Replaces the generated prompt entirely. An escape hatch
            for callers who want full control.
        extra_rules: Appended to the SQL quality rules.
        include_sql_rules / include_answer_rules: Toggle each block.
    """

    def __init__(
        self,
        *,
        persona: Optional[str] = None,
        dialect: Optional[str] = None,
        base_prompt: Optional[str] = None,
        extra_rules: Optional[List[str]] = None,
        include_sql_rules: bool = True,
        include_answer_rules: bool = True,
    ) -> None:
        self.persona = persona
        self.dialect = dialect
        self.base_prompt = base_prompt
        self.extra_rules = extra_rules or []
        self.include_sql_rules = include_sql_rules
        self.include_answer_rules = include_answer_rules

    async def build_system_prompt(
        self, user: "User", tools: List["ToolSchema"]
    ) -> Optional[str]:
        if self.base_prompt is not None:
            return self.base_prompt

        names = {t.name for t in tools}
        today = datetime.now().strftime("%Y-%m-%d")

        parts: List[str] = []

        parts.append(
            self.persona
            or (
                "You are DataLens, a data analyst assistant. Most questions you "
                "answer by querying the database and explaining what the results "
                "mean, but you also answer questions about yourself, your "
                "capabilities, the data you have access to, and results you have "
                "already returned in this conversation."
            )
        )
        parts.append(f"Today's date is {today}.")

        plan = self._build_plan(names)
        if plan:
            parts.append("\n## How to answer a data question\n")
            parts.extend(f"{i}. {step}" for i, step in enumerate(plan, start=1))

        if self.include_sql_rules and self._has_sql(names):
            dialect = f" ({self.dialect})" if self.dialect else ""
            parts.append(f"\n## SQL rules{dialect}\n")
            parts.extend(f"- {rule}" for rule in SQL_QUALITY_RULES)
            parts.extend(f"- {rule}" for rule in self.extra_rules)

        if self.include_answer_rules:
            parts.append(
                "\n## Answering a data question (when you have a query result)\n"
            )
            parts.extend(f"- {rule}" for rule in ANSWER_RULES)

        # Unconditional: an agent with no SQL tools at all still gets asked what
        # it can do, and still has to reply to a greeting.
        parts.append("\n## Questions that don't need a query\n")
        parts.extend(f"- {rule}" for rule in NON_DATA_RULES)

        if names:
            parts.append(f"\nAvailable tools: {', '.join(sorted(names))}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Plan composition
    # ------------------------------------------------------------------

    @staticmethod
    def _has_sql(names: set) -> bool:
        return any(n in names for n in ("run_sql", "execute_sql", "query"))

    def _build_plan(self, names: set) -> List[str]:
        """Emit only the steps whose tools are actually registered."""
        plan: List[str] = []

        if "get_relevant_tables" in names or "search_tables" in names:
            plan.append(
                "Identify the relevant tables. The schema you need is usually "
                "already provided above; look further only if it is not."
            )

        if "get_table_schema" in names:
            plan.append(
                "Fetch the full schema for any table you intend to use but "
                "have not been shown."
            )

        if "system_time" in names:
            plan.append(
                "If the question mentions any date or relative period "
                "('today', 'last quarter', 'YTD'), call system_time first and "
                "use the literal dates it returns."
            )

        if "check_column_values" in names:
            plan.append(
                "Before filtering on a string column, verify the value exists "
                "with check_column_values. Guessing at a literal is the most "
                "common cause of a query that runs correctly and returns "
                "nothing."
            )

        if "validate_sql" in names:
            plan.append(
                "Validate the query with validate_sql before running it, so "
                "mistakes cost nothing."
            )

        if self._has_sql(names):
            plan.append(
                "Execute the query. If it errors, read the error, fix the "
                "specific problem, and retry -- do not rewrite from scratch."
            )
            plan.append(
                "Interpret the result for the user in a sentence or two."
            )

        if "visualize_data" in names:
            plan.append(
                "Chart the result when a trend, comparison, or distribution "
                "would read better than a table."
            )

        return plan
