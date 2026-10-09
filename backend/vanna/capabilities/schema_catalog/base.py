"""Schema catalog capability interface."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from .describe import SCHEMA_FULL_TEXT_THRESHOLD, describe_schema
from .models import RelationshipMetadata, SchemaContext, TableMetadata

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

#: Most relevant tables the search path connects with a join tree. Search
#: returns ~15; connecting all of them would pull in bridges for tables that
#: only matched a stray word.
MAX_JOIN_TERMINALS = 6

#: Bridge tables the search path may add. Each costs a table definition's
#: worth of prompt; past five the tree is spanning subject areas, not joining.
MAX_BRIDGE_TABLES = 5


def _resolve(canonical: Dict[str, str], name: str) -> str:
    """Catalog spelling of *name*; relationships and tables may differ in case."""
    return canonical.get((name or "").lower(), name)


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
        seed_tables: Sequence[str] = (),
        max_bridges: int = MAX_BRIDGE_TABLES,
    ) -> SchemaContext:
        """Select the schema context for *question*, sized to the schema.

        Small schemas are sent whole; large ones fall back to relevance search.
        Sending everything is preferable whenever it fits, because retrieving
        tables in isolation strips the join paths between them -- precisely the
        information a multi-table query depends on. The threshold makes that a
        mechanical decision rather than a judgement call.

        On the search path that loss is then repaired from the join graph: the
        most relevant tables are connected by a Steiner tree, and the tables
        the tree runs through (``bridge_tables``, at most *max_bridges*) are
        added and marked join-only. ``track`` and ``artist`` both rank for "top
        artists by tracks sold"; ``album`` does not, and without it the model
        guesses a join.

        *seed_tables* are tables the question is known to involve -- linked to
        a business term or metric it named -- and are always included, ahead
        of search results. Names a caller cannot see are ignored.

        Permission filtering happens *before* measurement, so a user who can
        see 30 tables of a 400-table warehouse still gets the high-accuracy
        full-text path.
        """
        tables = await self.get_tables(context, data_source_id=data_source_id)
        if not tables:
            return SchemaContext(strategy="empty", text="", total_tables=0)

        relationships = [
            r
            for r in await self.get_relationships(context, data_source_id=data_source_id)
            if getattr(r, "is_usable", True)
        ]

        from vanna.capabilities.schema_graph import build_schema_graph, table_lookup

        canonical = table_lookup(tables)
        # Both ends visible, on either path. Relationships outlive the tables
        # they join -- a rescan retires a vanished table but not its edges --
        # so Pagila's 54 dropped partitions left 36 foreign keys in every
        # full-schema prompt, each naming a table the model could not see.
        relationships = [
            r
            for r in relationships
            if (r.from_table or "").lower() in canonical
            and (r.to_table or "").lower() in canonical
        ]
        hinted: List[str] = []
        for name in seed_tables or ():
            resolved = canonical.get(str(name).lower())
            if resolved and resolved not in hinted:
                hinted.append(resolved)

        full_text = describe_schema(tables, relationships)
        if len(full_text) <= threshold:
            return SchemaContext(
                strategy="full",
                text=full_text,
                table_names=[t.qualified_name for t in tables],
                char_count=len(full_text),
                total_tables=len(tables),
                included_tables=len(tables),
                hinted_tables=hinted,
                tables=list(tables),
            )

        by_name = {t.qualified_name: t for t in tables}
        relevant = await self.search_tables(
            context, question, limit=search_limit, data_source_id=data_source_id
        )
        order: List[str] = list(hinted)
        for table in relevant:
            if table.qualified_name not in order:
                order.append(table.qualified_name)
                by_name.setdefault(table.qualified_name, table)

        bridges: List[str] = []
        terminals = order[:MAX_JOIN_TERMINALS]
        if len(terminals) >= 2 and max_bridges > 0:
            graph = build_schema_graph(tables, relationships)
            terminals = [t for t in terminals if t in graph]
            if len(terminals) >= 2:
                tree = graph.steiner_tree(terminals)
                chosen = set(order)
                # Attachment order, so a cap keeps the joins nearest the most
                # relevant table rather than an alphabetical subset.
                bridges = [t for t in tree.tables if t not in chosen][:max_bridges]

        selected_names = order + bridges
        selected = [by_name[n] for n in selected_names if n in by_name]
        names = {t.qualified_name for t in selected}
        # Keep only the edges whose endpoints are both present -- a join path to
        # a table the model cannot see is noise that invites hallucination.
        scoped_rels = [
            r
            for r in relationships
            if _resolve(canonical, r.from_table) in names
            and _resolve(canonical, r.to_table) in names
        ]
        text = describe_schema(selected, scoped_rels, bridge_tables=bridges)
        return SchemaContext(
            strategy="search",
            text=text,
            table_names=sorted(names),
            char_count=len(text),
            total_tables=len(tables),
            included_tables=len(selected),
            bridge_tables=bridges,
            hinted_tables=hinted,
            tables=selected,
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
