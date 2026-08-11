"""File-backed example and instruction stores.

Both persist to JSON and scope everything by tenant. Example retrieval is
lexical, for the same reasons as ``LocalSchemaCatalog``: no model, no index, no
network call. Deployments that need semantic recall should implement
``ExampleStore.search`` against a vector store -- the interface is designed for
that substitution, and nothing else has to change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from vanna.capabilities.agent_memory import tenant_scope
from vanna.capabilities.knowledge import (
    Example,
    ExampleHit,
    ExampleStatus,
    ExampleStore,
    Instruction,
    InstructionStore,
    extract_tables,
    validate_sql_syntax,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Words carrying no discriminating signal in an analytics question. Without
#: this, "show me the total" and "show me the average" look nearly identical.
_STOPWORDS = frozenset(
    {
        # articles / prepositions / conjunctions
        "a", "an", "the", "of", "for", "by", "in", "on", "to", "and", "or",
        "from", "with", "as", "at", "it", "its", "this", "that", "these",
        "those",
        # copulas and auxiliaries
        "is", "are", "was", "were", "be", "been", "do", "does", "did", "can",
        # interrogatives and request phrasing
        "what", "which", "who", "whom", "how", "many", "much", "show", "me",
        "give", "list", "get", "find", "all", "any", "some", "please", "you",
    }
)


def _terms(text: str) -> set:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return {t for t in _WORD_RE.findall(spaced.lower()) if t not in _STOPWORDS}


class _JsonStore:
    """Shared JSON persistence with atomic writes."""

    def __init__(self, path: Optional[str], autosave: bool = True) -> None:
        self.path = Path(path) if path else None
        self.autosave = autosave
        self._lock = asyncio.Lock()

    def _read_raw(self) -> list:
        if not self.path or not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read %s: %s", self.path, e)
            return []
        return data if isinstance(data, list) else []

    def _write_raw(self, records: list) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)


class LocalExampleStore(ExampleStore, _JsonStore):
    """JSON-backed :class:`ExampleStore` with write-time SQL validation."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        autosave: bool = True,
        dialect: Optional[str] = None,
    ) -> None:
        _JsonStore.__init__(self, path, autosave)
        self.dialect = dialect
        self._examples: Dict[str, Example] = {}
        for raw in self._read_raw():
            try:
                example = Example.model_validate(raw)
                self._examples[example.id] = example
            except Exception as e:
                logger.warning("Skipping malformed example: %s", e)

    def save(self) -> None:
        self._write_raw([e.model_dump(mode="json") for e in self._examples.values()])

    def _maybe_save(self) -> None:
        if self.autosave:
            self.save()

    async def add(
        self,
        context: "ToolContext",
        question: str,
        sql: str,
        *,
        status: ExampleStatus = ExampleStatus.CANDIDATE,
        data_source_id: str = "default",
        tags: Optional[List[str]] = None,
        dialect: Optional[str] = None,
    ) -> Example:
        """Validate then store. Raises ``ValueError`` on unparseable SQL.

        Rejecting at write time is the point: an example that does not parse
        can never be right, and letting it in means it will be retrieved and
        shown to the model as a pattern to imitate.
        """
        effective_dialect = dialect or self.dialect
        error = validate_sql_syntax(sql, effective_dialect)
        if error:
            raise ValueError(f"Refusing to store an invalid example. {error}")

        async with self._lock:
            example = Example(
                question=question,
                sql=sql,
                status=status,
                tenant_id=tenant_scope(context),
                data_source_id=data_source_id,
                tables=extract_tables(sql, effective_dialect),
                tags=tags or [],
                created_by=context.user.id,
            )
            if status == ExampleStatus.VERIFIED:
                example.verified_at = datetime.now(timezone.utc)
                example.verified_by = context.user.id
            self._examples[example.id] = example
            self._maybe_save()
            return example

    def _visible(
        self, context: "ToolContext", data_source_id: Optional[str] = None
    ) -> List[Example]:
        tenant = tenant_scope(context)
        return [
            e
            for e in self._examples.values()
            if e.tenant_id == tenant
            and (data_source_id is None or e.data_source_id == data_source_id)
        ]

    async def search(
        self,
        context: "ToolContext",
        question: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        verified_only: bool = False,
        data_source_id: Optional[str] = None,
    ) -> List[ExampleHit]:
        """Rank by Jaccard term overlap between questions.

        Rejected examples are never returned -- that is what rejection means.
        """
        candidates = [
            e
            for e in self._visible(context, data_source_id)
            if e.status != ExampleStatus.REJECTED
            and (not verified_only or e.status == ExampleStatus.VERIFIED)
        ]
        if not candidates:
            return []

        query_terms = _terms(question)
        if not query_terms:
            return []

        hits: List[ExampleHit] = []
        for example in candidates:
            example_terms = _terms(example.question)
            if not example_terms:
                continue
            overlap = len(query_terms & example_terms)
            if not overlap:
                continue
            score = overlap / len(query_terms | example_terms)
            # A verified example outranks a candidate of equal similarity:
            # human review is a stronger signal than lexical closeness.
            if example.status == ExampleStatus.VERIFIED:
                score *= 1.25
            if score >= min_score:
                hits.append(ExampleHit(example=example, score=round(score, 4)))

        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    async def list_all(
        self,
        context: "ToolContext",
        *,
        status: Optional[ExampleStatus] = None,
        data_source_id: Optional[str] = None,
    ) -> List[Example]:
        examples = self._visible(context, data_source_id)
        if status is not None:
            examples = [e for e in examples if e.status == status]
        return sorted(examples, key=lambda e: e.created_at, reverse=True)

    async def set_status(
        self,
        context: "ToolContext",
        example_id: str,
        status: ExampleStatus,
        *,
        actor: Optional[str] = None,
    ) -> bool:
        async with self._lock:
            example = self._examples.get(example_id)
            # Ownership check: an id from another tenant must not be mutable.
            if not example or example.tenant_id != tenant_scope(context):
                return False
            example.status = status
            if status == ExampleStatus.VERIFIED:
                example.verified_at = datetime.now(timezone.utc)
                example.verified_by = actor or context.user.id
            self._maybe_save()
            return True

    async def delete(self, context: "ToolContext", example_id: str) -> bool:
        async with self._lock:
            example = self._examples.get(example_id)
            if not example or example.tenant_id != tenant_scope(context):
                return False
            del self._examples[example_id]
            self._maybe_save()
            return True


class LocalInstructionStore(InstructionStore, _JsonStore):
    """JSON-backed :class:`InstructionStore` resolving by scope."""

    def __init__(self, path: Optional[str] = None, *, autosave: bool = True) -> None:
        _JsonStore.__init__(self, path, autosave)
        self._instructions: Dict[str, Instruction] = {}
        for raw in self._read_raw():
            try:
                instruction = Instruction.model_validate(raw)
                self._instructions[instruction.id] = instruction
            except Exception as e:
                logger.warning("Skipping malformed instruction: %s", e)

    def save(self) -> None:
        self._write_raw(
            [i.model_dump(mode="json") for i in self._instructions.values()]
        )

    def _maybe_save(self) -> None:
        if self.autosave:
            self.save()

    async def add(
        self, context: "ToolContext", instruction: Instruction
    ) -> Instruction:
        if (
            instruction.scope.value != "global"
            and not instruction.scope_ref
        ):
            raise ValueError(
                f"Instructions scoped to '{instruction.scope.value}' require a "
                "scope_ref naming the data source, table, or group."
            )
        async with self._lock:
            instruction.tenant_id = tenant_scope(context)
            if instruction.created_by is None:
                instruction.created_by = context.user.id
            self._instructions[instruction.id] = instruction
            self._maybe_save()
            return instruction

    async def resolve(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        tables: Optional[List[str]] = None,
    ) -> List[Instruction]:
        tenant = tenant_scope(context)
        groups = list(getattr(context.user, "group_memberships", []) or [])
        applicable = [
            i
            for i in self._instructions.values()
            if i.tenant_id == tenant
            and i.applies_to(
                data_source_id=data_source_id, tables=tables, user_groups=groups
            )
        ]
        # Highest priority first, then oldest first so ordering is stable
        # across requests -- an unstable prompt prefix defeats prompt caching.
        applicable.sort(key=lambda i: (-i.priority, i.created_at))
        return applicable

    async def list_all(self, context: "ToolContext") -> List[Instruction]:
        tenant = tenant_scope(context)
        return sorted(
            (i for i in self._instructions.values() if i.tenant_id == tenant),
            key=lambda i: (-i.priority, i.created_at),
        )

    async def set_enabled(
        self, context: "ToolContext", instruction_id: str, enabled: bool
    ) -> bool:
        async with self._lock:
            instruction = self._instructions.get(instruction_id)
            if not instruction or instruction.tenant_id != tenant_scope(context):
                return False
            instruction.enabled = enabled
            self._maybe_save()
            return True

    async def delete(self, context: "ToolContext", instruction_id: str) -> bool:
        async with self._lock:
            instruction = self._instructions.get(instruction_id)
            if not instruction or instruction.tenant_id != tenant_scope(context):
                return False
            del self._instructions[instruction_id]
            self._maybe_save()
            return True
