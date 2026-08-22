"""Schema catalog capability interface."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional

from .describe import SCHEMA_FULL_TEXT_THRESHOLD, describe_schema
from .models import RelationshipMetadata, SchemaContext, TableMetadata

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext


class SchemaCatalog(ABC):
    """Stores and retrieves structural knowledge about a database.

    Every method takes a ``ToolContext`` and **must** scope reads and writes to
    ``context.tenant_id``. This is a hard contract, not a suggestion: a catalog
    that ignores it leaks one tenant's table names and column descriptions into
    another tenant's prompt. Use
    :func:`vanna.capabilities.agent_memory.tenant_scope` to resolve the key.

    Implementations only need the abstract methods; :meth:`get_context` and
    :meth:`catalog_hash` are provided.
    """

    @abstractmethod
    async def get_tables(
        self,
        context: "ToolContext",
        *,
        schema: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        """Return all tables visible to the caller, tenant-scoped."""

    @abstractmethod
    async def get_table(
        self,
        context: "ToolContext",
        name: str,
        *,
        data_source_id: Optional[str] = None,
    ) -> Optional[TableMetadata]:
        """Return one table by name (``table`` or ``schema.table``), or None.

        Matching should follow SQL identifier semantics: case-insensitive for
        unquoted names.
        """

    @abstractmethod
    async def search_tables(
        self,
        context: "ToolContext",
        query: str,
        *,
        limit: int = 10,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        """Return tables relevant to *query*, most relevant first.

        Implementations may use embeddings, full-text search, or plain keyword
        overlap. Relevance quality matters most for large schemas, where
        :meth:`get_context` falls back to search rather than sending everything.
        """

    @abstractmethod
    async def upsert_tables(
        self, context: "ToolContext", tables: List[TableMetadata]
    ) -> None:
        """Insert or replace table metadata, stamping the caller's tenant."""

    @abstractmethod
    async def get_relationships(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> List[RelationshipMetadata]:
        """Return known join paths, tenant-scoped."""

    @abstractmethod
    async def upsert_relationships(
        self, context: "ToolContext", relationships: List[RelationshipMetadata]
    ) -> None:
        """Insert or replace relationship metadata."""

    @abstractmethod
    async def clear(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> int:
        """Remove this tenant's catalog entries. Returns the number removed."""

    # ------------------------------------------------------------------
    # Provided
    # ------------------------------------------------------------------

    async def get_context(
        self,
        context: "ToolContext",
        question: str,
        *,
        threshold: int = SCHEMA_FULL_TEXT_THRESHOLD,
        search_limit: int = 15,
        data_source_id: Optional[str] = None,
    ) -> SchemaContext:
        """Select the schema context for *question*, sized to the schema.

        Small schemas are sent whole; large ones fall back to relevance search.
        Sending everything is preferable whenever it fits, because retrieving
        tables in isolation strips the join paths between them -- precisely the
        information a multi-table query depends on. The threshold makes that a
        mechanical decision rather than a judgement call.

        Permission filtering happens *before* measurement, so a user who can
        see 30 tables of a 400-table warehouse still gets the high-accuracy
        full-text path.
        """
        tables = await self.get_tables(context, data_source_id=data_source_id)
        if not tables:
            return SchemaContext(strategy="empty", text="", total_tables=0)

        relationships = await self.get_relationships(
            context, data_source_id=data_source_id
        )

        full_text = describe_schema(tables, relationships)
        if len(full_text) <= threshold:
            return SchemaContext(
                strategy="full",
                text=full_text,
                table_names=[t.qualified_name for t in tables],
                char_count=len(full_text),
                total_tables=len(tables),
                included_tables=len(tables),
            )

        relevant = await self.search_tables(
            context, question, limit=search_limit, data_source_id=data_source_id
        )
        selected = {t.qualified_name for t in relevant}
        # Keep only the edges whose endpoints are both present -- a join path to
        # a table the model cannot see is noise that invites hallucination.
        scoped_rels = [
            r
            for r in relationships
            if r.from_table in selected and r.to_table in selected
        ]
        text = describe_schema(relevant, scoped_rels)
        return SchemaContext(
            strategy="search",
            text=text,
            table_names=sorted(selected),
            char_count=len(text),
            total_tables=len(tables),
            included_tables=len(relevant),
        )

    async def catalog_hash(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> str:
        """Content hash of this tenant's catalog, for cache invalidation.

        Derived from structure only -- table names, column names, types, and
        categories. Descriptions and timestamps are excluded so that editing a
        comment or re-running an unchanged scan does not invalidate every
        downstream cache. Invalidation becomes a property of the content rather
        than something a human has to remember to do.
        """
        tables = await self.get_tables(context, data_source_id=data_source_id)
        payload = [
            {
                "t": t.qualified_name,
                "c": sorted(
                    (c.name, c.data_type, tuple(c.categories or ())) for c in t.columns
                ),
            }
            for t in sorted(tables, key=lambda x: x.qualified_name)
        ]
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
