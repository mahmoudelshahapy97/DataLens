"""Deterministic retrieval: build context before the model runs, not by asking it to.

Vanna's default system prompt currently spends roughly forty lines instructing
the model to call a search tool before doing anything else -- "you MUST first
call search_saved_correct_tool_uses", "Do NOT skip the search step, even if you
think you know how to answer".

Retrieval the model can decline is not retrieval. Compliance is probabilistic,
and each time it *does* comply it costs a full LLM round trip before any real
work begins. Dataherald and WrenAI both retrieve directly instead, and so does
this enhancer.

The mechanism already exists: ``LlmContextEnhancer.enhance_system_prompt`` is
called on every message. This implementation makes it do the work, assembling
four sources under a token budget:

1. **Instructions** -- resolved by scope, always applied.
2. **Verified examples** -- few-shot NL->SQL pairs.
3. **Schema** -- full text when it fits, relevance search when it does not.
4. **Memories** -- the existing ``AgentMemory``, as advisory background.

Every source is optional. Supply only what you have; missing sources are simply
omitted rather than rendered as empty headings.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Callable, List, Optional

from .base import LlmContextEnhancer
from .budget import AssemblyResult, BudgetPolicy, Section, assemble

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...capabilities.agent_memory import AgentMemory
    from ...capabilities.knowledge import ExampleStore, InstructionStore
    from ...capabilities.schema_catalog import SchemaCatalog
    from ..llm.models import LlmMessage
    from ..observability import ObservabilityProvider
    from ..user.models import User

logger = logging.getLogger(__name__)


class _NullAgentMemory:
    """Satisfies ``ToolContext.agent_memory`` when none is configured.

    Reads return empty and writes are dropped, so a deployment without agent
    memory behaves as though it simply has no memories -- rather than failing
    the whole retrieval pass on a type check.
    """

    async def save_tool_usage(self, *args, **kwargs) -> None:
        return None

    async def save_text_memory(self, *args, **kwargs):
        return None

    async def search_similar_usage(self, *args, **kwargs):
        return []

    async def search_text_memories(self, *args, **kwargs):
        return []

    async def get_recent_memories(self, *args, **kwargs):
        return []

    async def get_recent_text_memories(self, *args, **kwargs):
        return []

    async def delete_by_id(self, *args, **kwargs) -> bool:
        return False

    async def delete_text_memory(self, *args, **kwargs) -> bool:
        return False

    async def clear_memories(self, *args, **kwargs) -> int:
        return 0


def _register_null_memory() -> None:
    """Register the null object as an ``AgentMemory`` virtual subclass.

    ``ToolContext`` validates ``agent_memory`` with an isinstance check, so a
    structurally-compatible class is not enough -- it has to be recognised as
    the ABC. Registering avoids importing AgentMemory at module scope, which
    would create a circular import (capabilities imports core.tool, which is
    what this module is being imported to serve).
    """
    try:
        from ...capabilities.agent_memory import AgentMemory

        AgentMemory.register(_NullAgentMemory)
    except Exception:  # pragma: no cover - defensive
        pass


_register_null_memory()


class RetrievalContextEnhancer(LlmContextEnhancer):
    """Assembles retrieved context into the system prompt on every turn.

    Args:
        catalog: Schema catalog. Supplies table and column context.
        example_store: Verified NL->SQL pairs for few-shot prompting.
        instruction_store: Scope-resolved business rules.
        agent_memory: Existing memory, included as advisory background.
        budget: Token allocation across sections.
        data_source_id: Data source to scope retrieval to.
        max_examples / max_memories: Per-source result caps. Kept small on
            purpose -- over-retrieval causes anchoring, where the model contorts
            a near-miss example instead of writing fresh SQL.
        verified_examples_only: Use only human-reviewed examples.
        observability_provider: Emits per-section token metrics.
        count_tokens: Real tokenizer, if available.
    """

    def __init__(
        self,
        *,
        catalog: Optional["SchemaCatalog"] = None,
        example_store: Optional["ExampleStore"] = None,
        instruction_store: Optional["InstructionStore"] = None,
        agent_memory: Optional["AgentMemory"] = None,
        budget: Optional[BudgetPolicy] = None,
        data_source_id: str = "default",
        max_examples: int = 3,
        max_memories: int = 5,
        verified_examples_only: bool = False,
        observability_provider: Optional["ObservabilityProvider"] = None,
        count_tokens: Optional[Callable[[str], int]] = None,
    ) -> None:
        self.catalog = catalog
        self.example_store = example_store
        self.instruction_store = instruction_store
        self.agent_memory = agent_memory
        self.budget = budget or BudgetPolicy()
        self.data_source_id = data_source_id
        self.max_examples = max_examples
        self.max_memories = max_memories
        self.verified_examples_only = verified_examples_only
        self.observability_provider = observability_provider
        self.count_tokens = count_tokens

    # ------------------------------------------------------------------
    # LlmContextEnhancer interface
    # ------------------------------------------------------------------

    async def enhance_system_prompt(
        self, system_prompt: str, user_message: str, user: "User"
    ) -> str:
        """Append retrieved context to the system prompt.

        Retrieval failures never fail the request: a degraded prompt still
        produces an answer, while a raised exception produces nothing. Each
        source is isolated so one broken store cannot suppress the others.
        """
        result = await self.build_context(user_message, user)
        if result is None or not result.text:
            return system_prompt
        return f"{system_prompt}\n\n{result.text}"

    async def build_context(
        self, user_message: str, user: "User"
    ) -> Optional[AssemblyResult]:
        """Retrieve and budget the context, returning the assembly itself.

        Split out from :meth:`enhance_system_prompt` so callers that need to
        *show* what was assembled -- a prompt preview, a debugging endpoint --
        can see the per-section token counts and what the budget dropped,
        instead of re-deriving them from a finished string.

        Returns None when there was nothing to add.
        """
        context = self._make_context(user)

        sections: List[Section] = []

        instructions = await self._safe(
            "instructions", self._instruction_items, context
        )
        examples = await self._safe(
            "examples", self._example_items, context, user_message
        )
        schema_text, schema_tables = await self._safe(
            "schema", self._schema_section, context, user_message
        ) or ("", [])
        memories = await self._safe(
            "memories", self._memory_items, context, user_message
        )

        # Table-scoped instructions can only be resolved once the schema
        # section has decided which tables are in play, so re-resolve with that
        # knowledge. This is why the schema section is computed before the
        # instruction section is finalised, despite being displayed after it.
        if schema_tables and self.instruction_store is not None:
            refined = await self._safe(
                "instructions", self._instruction_items, context, schema_tables
            )
            if refined:
                instructions = refined

        if instructions:
            sections.append(
                Section(
                    name="instructions",
                    title="## Rules you must follow",
                    preamble=(
                        "These apply to every query. They are not optional and "
                        "override your defaults."
                    ),
                    items=instructions,
                    priority=100,  # never the first thing dropped
                    display_order=10,
                )
            )

        if schema_text:
            sections.append(
                Section(
                    name="schema",
                    title="## Database schema",
                    items=[schema_text],
                    priority=90,
                    display_order=20,
                )
            )

        if examples:
            sections.append(
                Section(
                    name="examples",
                    title="## Verified query examples",
                    preamble=(
                        "Correct SQL for similar questions against this "
                        "database. Follow their conventions."
                    ),
                    items=examples,
                    priority=80,
                    display_order=30,
                )
            )

        if memories:
            sections.append(
                Section(
                    name="memories",
                    title="## Background from previous sessions",
                    preamble="Context that may be relevant. Lower confidence "
                    "than the rules above.",
                    items=memories,
                    priority=50,  # first to go under pressure
                    display_order=40,
                )
            )

        if not sections:
            return None

        result = assemble(sections, self.budget, count_tokens=self.count_tokens)
        await self._record_metrics(result)
        return result

    async def enhance_user_messages(
        self, messages: list["LlmMessage"], user: "User"
    ) -> list["LlmMessage"]:
        """Unmodified. All context goes in the system prompt.

        Keeping it there means the retrieved block sits in a stable prefix,
        which is what lets provider-side prompt caching hit across turns.
        """
        return messages

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    async def _instruction_items(
        self, context, tables: Optional[List[str]] = None
    ) -> List[str]:
        if self.instruction_store is None:
            return []
        instructions = await self.instruction_store.resolve(
            context, data_source_id=self.data_source_id, tables=tables
        )
        return [f"- {i.text}" for i in instructions]

    async def _example_items(self, context, question: str) -> List[str]:
        if self.example_store is None:
            return []
        hits = await self.example_store.search(
            context,
            question,
            limit=self.max_examples,
            verified_only=self.verified_examples_only,
            data_source_id=self.data_source_id,
        )
        return [
            f"Question: {h.example.question}\nSQL:\n{h.example.sql}" for h in hits
        ]

    async def _schema_section(self, context, question: str):
        if self.catalog is None:
            return "", []
        schema_context = await self.catalog.get_context(
            context, question, data_source_id=self.data_source_id
        )
        if not schema_context.text:
            return "", []

        text = schema_context.text
        if schema_context.is_partial:
            # Tell the model the schema is partial. Otherwise a missing table
            # looks like a non-existent one, and it invents a name instead of
            # asking.
            text = (
                f"(Showing {schema_context.included_tables} of "
                f"{schema_context.total_tables} tables, selected for this "
                "question. If the table you need is absent, say so rather than "
                "guessing a name.)\n\n" + text
            )
        return text, schema_context.table_names

    async def _memory_items(self, context, question: str) -> List[str]:
        if self.agent_memory is None:
            return []
        results = await self.agent_memory.search_text_memories(
            question, context, limit=self.max_memories
        )
        return [f"- {r.memory.content}" for r in results]

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _make_context(self, user: "User"):
        """Build a ToolContext for capability calls.

        The enhancer runs before the agent's own ToolContext exists, so it
        constructs one. ``tenant_id`` comes from the user, which is what keeps
        retrieval tenant-scoped on this path too.

        ``ToolContext.agent_memory`` is a required, type-validated field, but
        this enhancer's own ``agent_memory`` is optional -- a deployment using
        only the catalog and example store has no reason to configure one.
        Passing None straight through raises a validation error that takes out
        *all* retrieval, including the sources that were configured. A null
        object keeps the type contract satisfied and confines the absence to
        the one source actually missing.
        """
        from ..tool import ToolContext

        return ToolContext(
            user=user,
            conversation_id="retrieval",
            request_id=str(uuid.uuid4()),
            tenant_id=getattr(user, "tenant_id", "default"),
            agent_memory=self.agent_memory or _NullAgentMemory(),
        )

    async def _safe(self, source: str, fn, *args):
        """Run a source, converting failure into an empty result."""
        try:
            return await fn(*args)
        except Exception as e:
            logger.warning(
                "Retrieval source %r failed; continuing without it: %s",
                source,
                e,
                exc_info=True,
            )
            return [] if source != "schema" else ("", [])

    async def _record_metrics(self, result) -> None:
        provider = self.observability_provider
        if provider is None:
            return
        from ..observability.tracing import record_metric

        for name, tokens in result.section_tokens.items():
            await record_metric(
                provider,
                "retrieval.section.tokens",
                float(tokens),
                "tokens",
                tags={"section": name},
            )
        for name, dropped in result.dropped_items.items():
            await record_metric(
                provider,
                "retrieval.section.dropped",
                float(dropped),
                "count",
                tags={"section": name},
            )
