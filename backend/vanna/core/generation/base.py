"""Generation store interface and a file-backed implementation."""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from .models import Feedback, GenerationStats, GenerationStatus, SqlGeneration

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..tool.models import ToolContext

logger = logging.getLogger(__name__)


class GenerationStore(ABC):
    """Records what the agent produced, scoped by tenant.

    Writes are on the request path, so implementations must be fast and must
    never raise into the caller -- a quality-analytics store that can take down
    a user's query has inverted its own cost/benefit.
    """

    @abstractmethod
    async def record(
        self, context: "ToolContext", generation: SqlGeneration
    ) -> SqlGeneration:
        """Store a generation, stamped with the caller's tenant."""

    @abstractmethod
    async def get(
        self, context: "ToolContext", generation_id: str
    ) -> Optional[SqlGeneration]:
        """Fetch one generation, or None."""

    @abstractmethod
    async def find_by_request(
        self, context: "ToolContext", request_id: str
    ) -> List[SqlGeneration]:
        """Every generation for one request. Used to attach feedback."""

    @abstractmethod
    async def set_feedback(
        self,
        context: "ToolContext",
        request_id: str,
        feedback: Feedback,
        comment: Optional[str] = None,
    ) -> int:
        """Attach a rating to a request's generations. Returns the count updated."""

    @abstractmethod
    async def list_recent(
        self,
        context: "ToolContext",
        *,
        limit: int = 100,
        status: Optional[GenerationStatus] = None,
        since: Optional[datetime] = None,
    ) -> List[SqlGeneration]:
        """Recent generations, newest first."""

    @abstractmethod
    async def stats(
        self, context: "ToolContext", *, since: Optional[datetime] = None
    ) -> GenerationStats:
        """Aggregate quality metrics."""

    async def promotable(
        self, context: "ToolContext", *, limit: int = 50
    ) -> List[SqlGeneration]:
        """Generations worth promoting into the verified example store."""
        recent = await self.list_recent(context, limit=limit * 10)
        return [g for g in recent if g.is_promotable][:limit]


class LocalGenerationStore(GenerationStore):
    """JSONL-backed generation store.

    Append-only JSON Lines rather than a single JSON document, because writes
    happen on every query: appending one line is cheap and cannot corrupt
    earlier records, whereas rewriting a growing array on each write gets
    slower forever and loses everything if interrupted.

    Fine up to a few hundred thousand records. Past that, or for multi-process
    deployments, back the same interface with a real database -- this is
    relational data and belongs in a relational store.

    Args:
        path: JSONL file. None keeps records in memory only.
        max_in_memory: Cap on retained records, oldest evicted first. Bounds
            memory in a long-running process; the file keeps everything.
    """

    def __init__(
        self, path: Optional[str] = None, *, max_in_memory: int = 10_000
    ) -> None:
        self.path = Path(path) if path else None
        self.max_in_memory = max_in_memory
        self._records: List[SqlGeneration] = []
        self._lock = asyncio.Lock()
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as handle:  # type: ignore[union-attr]
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._records.append(SqlGeneration.model_validate_json(line))
                    except Exception as e:
                        # One malformed line must not make the whole history
                        # unreadable -- that is the point of line-delimited.
                        logger.warning("Skipping malformed generation record: %s", e)
        except OSError as e:
            logger.warning("Could not read generation log: %s", e)
        self._evict()

    def _append(self, generation: SqlGeneration) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(generation.model_dump_json() + "\n")
        except OSError as e:
            logger.warning("Could not append generation record: %s", e)

    def _rewrite(self) -> None:
        """Rewrite the whole file. Used after a feedback update."""
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                for record in self._records:
                    handle.write(record.model_dump_json() + "\n")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("Could not rewrite generation log: %s", e)

    def _evict(self) -> None:
        if len(self._records) > self.max_in_memory:
            self._records = self._records[-self.max_in_memory :]

    @staticmethod
    def _tenant(context: "ToolContext") -> str:
        from ...capabilities.agent_memory import tenant_scope

        return tenant_scope(context)

    def _visible(self, context: "ToolContext") -> List[SqlGeneration]:
        tenant = self._tenant(context)
        return [r for r in self._records if r.tenant_id == tenant]

    # ------------------------------------------------------------------

    async def record(
        self, context: "ToolContext", generation: SqlGeneration
    ) -> SqlGeneration:
        async with self._lock:
            generation.tenant_id = self._tenant(context)
            if not generation.user_id:
                generation.user_id = context.user.id
            if not generation.conversation_id:
                generation.conversation_id = context.conversation_id
            if not generation.request_id:
                generation.request_id = context.request_id
            self._records.append(generation)
            self._append(generation)
            self._evict()
            return generation

    async def get(
        self, context: "ToolContext", generation_id: str
    ) -> Optional[SqlGeneration]:
        for record in self._visible(context):
            if record.id == generation_id:
                return record
        return None

    async def find_by_request(
        self, context: "ToolContext", request_id: str
    ) -> List[SqlGeneration]:
        return [r for r in self._visible(context) if r.request_id == request_id]

    async def set_feedback(
        self,
        context: "ToolContext",
        request_id: str,
        feedback: Feedback,
        comment: Optional[str] = None,
    ) -> int:
        async with self._lock:
            tenant = self._tenant(context)
            updated = 0
            for record in self._records:
                if record.tenant_id == tenant and record.request_id == request_id:
                    record.feedback = feedback
                    record.feedback_comment = comment
                    updated += 1
            if updated:
                self._rewrite()
            return updated

    async def list_recent(
        self,
        context: "ToolContext",
        *,
        limit: int = 100,
        status: Optional[GenerationStatus] = None,
        since: Optional[datetime] = None,
    ) -> List[SqlGeneration]:
        records = self._visible(context)
        if status is not None:
            records = [r for r in records if r.status == status]
        if since is not None:
            records = [r for r in records if r.created_at >= since]
        return sorted(records, key=lambda r: r.created_at, reverse=True)[:limit]

    async def stats(
        self, context: "ToolContext", *, since: Optional[datetime] = None
    ) -> GenerationStats:
        records = self._visible(context)
        if since is not None:
            records = [r for r in records if r.created_at >= since]

        stats = GenerationStats(total=len(records))
        if not records:
            return stats

        durations: List[float] = []
        repairs = 0
        for record in records:
            if record.status == GenerationStatus.VALID:
                stats.valid += 1
            elif record.status == GenerationStatus.INVALID:
                stats.invalid += 1
            elif record.status == GenerationStatus.EMPTY:
                stats.empty += 1
            elif record.status == GenerationStatus.REJECTED_BY_POLICY:
                stats.rejected_by_policy += 1
            elif record.status == GenerationStatus.TIMEOUT:
                stats.timeout += 1

            if record.feedback == Feedback.POSITIVE:
                stats.positive_feedback += 1
            elif record.feedback == Feedback.NEGATIVE:
                stats.negative_feedback += 1

            if record.cost_usd:
                stats.total_cost_usd += record.cost_usd
            if record.execution_ms is not None:
                durations.append(record.execution_ms)
            if record.repair_attempts:
                repairs += 1

        if durations:
            stats.avg_execution_ms = sum(durations) / len(durations)
        stats.repair_rate = repairs / len(records)
        return stats


def recent_window(hours: int = 24) -> datetime:
    """Convenience: a UTC cutoff *hours* in the past."""
    return datetime.now(timezone.utc) - timedelta(hours=hours)
