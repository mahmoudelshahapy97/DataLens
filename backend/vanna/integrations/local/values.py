"""In-memory value dictionary store.

For tests, examples and single-process deployments. The property that must
survive a move to a real database is the one this makes cheap to check: a value
recorded by sampling is invisible until reviewed, and re-sampling never resets a
review somebody already made.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from ...capabilities.values import (
    ReviewStatus,
    SampledValue,
    ValueStore,
    ValueSynonym,
)

if TYPE_CHECKING:  # pragma: no cover
    from ...core.tool import ToolContext


class MemoryValueStore(ValueStore):
    """Sampled values and synonyms held in process memory, scoped by tenant."""

    def __init__(self, *, auto_approve: bool = False) -> None:
        self._lock = asyncio.Lock()
        # (tenant, data_source, table, column, value) -> SampledValue
        self._samples: Dict[Tuple[str, ...], SampledValue] = {}
        # (tenant, data_source, table, column, value) -> ValueSynonym
        self._synonyms: Dict[Tuple[str, ...], ValueSynonym] = {}
        # Convenience for demos and tests only. A deployment that sets this has
        # decided sampling alone may publish column contents to a model, which
        # is exactly the decision the review step exists to make explicit.
        self.auto_approve = auto_approve

    async def record_samples(
        self, context: "ToolContext", values: Sequence[SampledValue]
    ) -> int:
        tenant = _tenant(context)
        added = 0
        async with self._lock:
            for value in values:
                stamped = value.model_copy(update={"tenant_id": tenant})
                key = _sample_key(stamped)
                if key in self._samples:
                    # Already known. Leave the existing row alone -- resetting
                    # it would silently un-approve the dictionary on every scan.
                    continue
                if self.auto_approve:
                    stamped = stamped.model_copy(
                        update={"status": ReviewStatus.APPROVED}
                    )
                self._samples[key] = stamped
                added += 1
        return added

    async def list_samples(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        table: Optional[str] = None,
        column: Optional[str] = None,
        status: Optional[ReviewStatus] = None,
        limit: int = 500,
    ) -> List[SampledValue]:
        tenant = _tenant(context)
        async with self._lock:
            rows = [
                row.model_copy()
                for row in self._samples.values()
                if row.tenant_id == tenant
                and (data_source_id is None or row.data_source_id == data_source_id)
                and (table is None or row.table.casefold() == table.casefold())
                and (column is None or row.column.casefold() == column.casefold())
                and (status is None or row.status is status)
            ]
        rows.sort(key=lambda r: (r.table, r.column, r.value))
        return rows[:limit]

    async def set_status(
        self,
        context: "ToolContext",
        *,
        table: str,
        column: str,
        values: Sequence[str],
        status: ReviewStatus,
        actor: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> int:
        tenant = _tenant(context)
        wanted = set(values)
        changed = 0
        async with self._lock:
            for key, row in list(self._samples.items()):
                if (
                    row.tenant_id != tenant
                    or row.table.casefold() != table.casefold()
                    or row.column.casefold() != column.casefold()
                    or row.value not in wanted
                    or (data_source_id and row.data_source_id != data_source_id)
                ):
                    continue
                if row.status is status:
                    continue
                self._samples[key] = row.model_copy(
                    update={"status": status, "reviewed_by": actor}
                )
                changed += 1
        return changed

    async def set_synonym(
        self, context: "ToolContext", synonym: ValueSynonym
    ) -> None:
        stamped = synonym.model_copy(update={"tenant_id": _tenant(context)})
        async with self._lock:
            self._synonyms[_synonym_key(stamped)] = stamped

    async def list_synonyms(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        table: Optional[str] = None,
    ) -> List[ValueSynonym]:
        tenant = _tenant(context)
        async with self._lock:
            return [
                row.model_copy()
                for row in self._synonyms.values()
                if row.tenant_id == tenant
                and (data_source_id is None or row.data_source_id == data_source_id)
                and (table is None or row.table.casefold() == table.casefold())
            ]


def _tenant(context: "ToolContext") -> str:
    return getattr(context, "tenant_id", "default") or "default"


def _sample_key(row: SampledValue) -> Tuple[str, ...]:
    return (
        row.tenant_id,
        row.data_source_id,
        row.table.casefold(),
        row.column.casefold(),
        row.value,
    )


def _synonym_key(row: ValueSynonym) -> Tuple[str, ...]:
    return (
        row.tenant_id,
        row.data_source_id,
        row.table.casefold(),
        row.column.casefold(),
        row.value,
    )
