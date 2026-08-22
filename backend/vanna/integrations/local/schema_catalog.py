"""File-backed schema catalog.

Suitable for single-process deployments, development, and small tenant counts.
Everything is held in memory and persisted to one JSON file. For multi-process
or high-tenant deployments, back the same interface with a relational store --
this is structured, queryable, relational data and does not belong in a vector
store.

Relevance search here is lexical (term overlap over table names, column names,
and descriptions) rather than embedding-based. That is a deliberate default:
it needs no model, no index, and no network call, and for the small-to-medium
schemas this implementation targets, the full-text path is used anyway.
Deployments with large schemas should implement ``search_tables`` against a
vector store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from vanna.capabilities.agent_memory import tenant_scope
from vanna.capabilities.schema_catalog import (
    RelationshipMetadata,
    SchemaCatalog,
    TableMetadata,
)
from vanna.capabilities.schema_catalog.search import (
    rank_tables,
    search_indexed,
    terms,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

logger = logging.getLogger(__name__)

# Ranking lives in the capabilities package: every catalog implementation needs the
# same fallback when no vector index is configured, and two copies of a relevance
# function drift into two different answers to the same question.
_terms = terms


class LocalSchemaCatalog(SchemaCatalog):
    """JSON-file-backed :class:`SchemaCatalog`.

    Args:
        path: JSON file to persist to. None keeps everything in memory only.
        autosave: Write to disk after each mutation. Disable for bulk loads and
            call :meth:`save` once at the end.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        autosave: bool = True,
        index: Optional[Any] = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.autosave = autosave
        # Optional ranked index. Without one, search_tables uses the term
        # overlap below -- which keeps this catalog dependency-free and makes
        # the index a strict improvement rather than a requirement.
        self.index = index
        # (tenant_id, data_source_id) -> qualified_name -> TableMetadata
        self._tables: Dict[Tuple[str, str], Dict[str, TableMetadata]] = {}
        self._relationships: Dict[Tuple[str, str], Dict[str, RelationshipMetadata]] = {}
        self._lock = asyncio.Lock()
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except Exception as e:
            logger.warning("Could not read catalog at %s: %s", self.path, e)
            return

        for entry in raw.get("tables", []):
            try:
                table = TableMetadata.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping malformed catalog table entry: %s", e)
                continue
            self._tables.setdefault(
                (table.tenant_id, table.data_source_id), {}
            )[table.qualified_name] = table

        for entry in raw.get("relationships", []):
            try:
                rel = RelationshipMetadata.model_validate(entry)
            except Exception as e:
                logger.warning("Skipping malformed catalog relationship: %s", e)
                continue
            self._relationships.setdefault(
                (rel.tenant_id, rel.data_source_id), {}
            )[rel.name] = rel

    def save(self) -> None:
        """Persist to disk. No-op when constructed without a path."""
        if not self.path:
            return
        payload = {
            "tables": [
                t.model_dump(mode="json")
                for bucket in self._tables.values()
                for t in bucket.values()
            ],
            "relationships": [
                r.model_dump(mode="json")
                for bucket in self._relationships.values()
                for r in bucket.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a temp file then replace, so an interrupted write cannot
        # leave a truncated catalog behind.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _maybe_save(self) -> None:
        if self.autosave:
            self.save()

    # ------------------------------------------------------------------
    # Scoping
    # ------------------------------------------------------------------

    @staticmethod
    def _key(context: "ToolContext", data_source_id: Optional[str]) -> Tuple[str, str]:
        """Bucket key. The tenant component is never caller-supplied."""
        return (tenant_scope(context), data_source_id or "default")

    def _buckets(
        self, context: "ToolContext", data_source_id: Optional[str]
    ) -> List[Tuple[str, str]]:
        """Keys to read from -- one data source, or all of the tenant's."""
        tenant = tenant_scope(context)
        if data_source_id is not None:
            return [(tenant, data_source_id)]
        return [k for k in self._tables if k[0] == tenant]

    # ------------------------------------------------------------------
    # SchemaCatalog interface
    # ------------------------------------------------------------------

    async def get_tables(
        self,
        context: "ToolContext",
        *,
        schema: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        tables: List[TableMetadata] = []
        for key in self._buckets(context, data_source_id):
            tables.extend(self._tables.get(key, {}).values())
        if schema is not None:
            tables = [t for t in tables if t.schema_name == schema]
        return sorted(tables, key=lambda t: t.qualified_name)

    async def get_table(
        self,
        context: "ToolContext",
        name: str,
        *,
        data_source_id: Optional[str] = None,
    ) -> Optional[TableMetadata]:
        lowered = name.lower()
        for table in await self.get_tables(context, data_source_id=data_source_id):
            if (
                table.qualified_name.lower() == lowered
                or table.table_name.lower() == lowered
            ):
                return table
        return None

    async def search_tables(
        self,
        context: "ToolContext",
        query: str,
        *,
        limit: int = 10,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        """Rank tables against *query*.

        Uses the configured index when there is one -- which also matches on the
        column *values* the scanner profiled, so "cancelled orders" finds the
        orders table through its ``status`` enum. Falls back to term overlap
        otherwise.
        """
        tables = await self.get_tables(context, data_source_id=data_source_id)
        if not tables:
            return []

        if self.index is not None:
            ranked = search_indexed(self.index, context, query, tables, limit)
            if ranked is not None:
                return ranked

        return rank_tables(query, tables, limit)

    async def upsert_tables(
        self, context: "ToolContext", tables: List[TableMetadata]
    ) -> None:
        async with self._lock:
            tenant = tenant_scope(context)
            for table in tables:
                # Stamp the caller's tenant rather than trusting the record:
                # a scanner or import could otherwise write into another
                # tenant's bucket.
                table.tenant_id = tenant
                key = (tenant, table.data_source_id)
                self._tables.setdefault(key, {})[table.qualified_name] = table
            self._maybe_save()

    async def get_relationships(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> List[RelationshipMetadata]:
        tenant = tenant_scope(context)
        keys = (
            [(tenant, data_source_id)]
            if data_source_id is not None
            else [k for k in self._relationships if k[0] == tenant]
        )
        out: List[RelationshipMetadata] = []
        for key in keys:
            out.extend(self._relationships.get(key, {}).values())
        return sorted(out, key=lambda r: r.name)

    async def upsert_relationships(
        self, context: "ToolContext", relationships: List[RelationshipMetadata]
    ) -> None:
        async with self._lock:
            tenant = tenant_scope(context)
            for rel in relationships:
                rel.tenant_id = tenant
                key = (tenant, rel.data_source_id)
                self._relationships.setdefault(key, {})[rel.name] = rel
            self._maybe_save()

    async def clear(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> int:
        async with self._lock:
            tenant = tenant_scope(context)
            removed = 0
            for store in (self._tables, self._relationships):
                keys = [
                    k
                    for k in store
                    if k[0] == tenant
                    and (data_source_id is None or k[1] == data_source_id)
                ]
                for key in keys:
                    removed += len(store[key])
                    del store[key]
            self._maybe_save()
            return removed
