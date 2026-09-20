"""Find the join path between tables that are not directly related.

``get_table_schema`` (``vanna/tools/schema.py``) already returns every
relationship with one end in the tables it was asked for, so a *direct* edge is
never news by the time the model is writing SQL. What it cannot show is the
path through a table the model never asked about: ``track`` and ``artist`` have
no edge between them, and the model has to guess that ``album`` sits in the
middle. Guessing produces a join on two columns that happen to share a name, a
query that runs, and a number that is wrong -- the failure this tool exists to
remove.

So this tool answers only the question the schema tools cannot: **what is the
shortest chain of joins connecting these tables**, including the intermediate
tables nobody named. Its description says so explicitly, because the model
otherwise has no way to tell it apart from ``get_table_schema``.

Edges come from two sources, merged: the curated ``RelationshipMetadata`` graph
and the foreign keys recorded on the columns themselves. Both already exist in
the catalog; nothing here scans the database.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple, Type

from pydantic import BaseModel, Field

from vanna.capabilities.schema_catalog import SchemaCatalog
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Hops allowed before a path is judged too tenuous to suggest. Four already
#: means three tables the user never mentioned; beyond that a "join path" is
#: more likely to be two unrelated subject areas sharing a lookup table than a
#: route anyone wants joined.
_MAX_HOPS = 4

#: Bridge candidates listed when tables cannot be connected at all. Enough to
#: be a lead, few enough that the model does not treat the list as a schema.
_MAX_BRIDGE_HINTS = 6


class SuggestJoinsArgs(BaseModel):
    """Arguments for suggest_joins."""

    tables: List[str] = Field(
        description="Two or more tables that must appear in one query. "
        "Names may be bare or schema-qualified.",
        min_length=2,
    )


class SuggestJoinsTool(Tool[SuggestJoinsArgs]):
    """Finds multi-hop join paths from the catalog's relationship graph.

    Args:
        catalog: Source of both relationships and foreign keys.
        data_source_id: Scopes lookups the same way the schema tools do.
    """

    #: Takes table names, never SQL.
    sql_argument_fields: tuple = ()

    def __init__(
        self, catalog: SchemaCatalog, *, data_source_id: Optional[str] = None
    ) -> None:
        self.catalog = catalog
        self.data_source_id = data_source_id

    @property
    def name(self) -> str:
        return "suggest_joins"

    @property
    def description(self) -> str:
        return (
            "Find how to join two or more tables that have no direct "
            "relationship, including the intermediate tables needed to connect "
            "them. Returns ready-to-use ON clauses. Use this before writing "
            "any query joining tables you have not joined before in this "
            "conversation -- guessing a join on same-named columns is the most "
            "common cause of a query that runs and returns a wrong number. "
            "get_table_schema already shows direct relationships; this finds "
            "the path when there is no direct one."
        )

    def get_args_schema(self) -> Type[SuggestJoinsArgs]:
        return SuggestJoinsArgs

    async def execute(self, context: ToolContext, args: SuggestJoinsArgs) -> ToolResult:
        try:
            tables = await self.catalog.get_tables(
                context, data_source_id=self.data_source_id
            )
        except Exception as e:
            return _failure(f"Could not read the schema catalog: {e}")

        # Canonical name per table, plus a lookup that accepts the bare name,
        # the qualified name, and either in any case -- the model reliably
        # supplies a different form from the one the catalog stores.
        canonical: Dict[str, str] = {}
        for table in tables:
            qualified = table.qualified_name
            canonical[qualified.lower()] = qualified
            canonical.setdefault(table.table_name.lower(), qualified)

        wanted: List[str] = []
        unknown: List[str] = []
        for name in args.tables:
            resolved = canonical.get(name.lower())
            if resolved is None:
                unknown.append(name)
            elif resolved not in wanted:
                wanted.append(resolved)

        if unknown:
            # Naming the misses rather than silently dropping them: a model
            # handed a path between the tables it *did* get right will assume
            # the missing one was covered.
            text = (
                f"Not in the catalog: {', '.join(unknown)}. Use search_tables "
                "to find the correct names, then call suggest_joins again."
            )
            return _ok(text, text, metadata={"unknown": unknown})

        if len(wanted) < 2:
            text = "Give two or more distinct tables. A single table needs no join."
            return _ok(text, text)

        edges = await self._build_edges(context, tables, canonical)
        if not edges:
            text = (
                "The catalog records no relationships or foreign keys for this "
                "data source, so no join path can be derived. Fall back to "
                "get_table_schema and join on the primary/foreign key columns "
                "you can see."
            )
            return _ok(text, text)

        return self._describe_paths(wanted, edges)

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    async def _build_edges(
        self,
        context: ToolContext,
        tables: List[Any],
        canonical: Dict[str, str],
    ) -> Dict[str, Set[Tuple[str, str, str]]]:
        """Undirected adjacency: table -> {(neighbour, own column, their column)}.

        Undirected because a join is symmetric; the catalog stores a direction
        because cardinality has one, and reading only the stored direction is
        how a path from the "many" side to the "one" side goes missing.
        """
        edges: Dict[str, Set[Tuple[str, str, str]]] = {}

        def add(a: str, a_col: str, b: str, b_col: str) -> None:
            if a == b:
                return  # self-joins are real, but never part of a path between two tables
            edges.setdefault(a, set()).add((b, a_col, b_col))
            edges.setdefault(b, set()).add((a, b_col, a_col))

        try:
            relationships = await self.catalog.get_relationships(
                context, data_source_id=self.data_source_id
            )
        except Exception:
            relationships = []  # foreign keys alone still give a usable graph

        for rel in relationships:
            source = canonical.get(rel.from_table.lower())
            target = canonical.get(rel.to_table.lower())
            if source and target:
                add(source, rel.from_column, target, rel.to_column)

        for table in tables:
            for column in getattr(table, "columns", None) or []:
                fk = getattr(column, "foreign_key", None)
                if fk is None:
                    continue
                target = canonical.get(fk.references_table.lower())
                if target:
                    add(
                        table.qualified_name,
                        fk.column or column.name,
                        target,
                        fk.references_column,
                    )

        return edges

    # ------------------------------------------------------------------
    # Path finding
    # ------------------------------------------------------------------

    def _describe_paths(
        self, wanted: List[str], edges: Dict[str, Set[Tuple[str, str, str]]]
    ) -> ToolResult:
        anchor = wanted[0]
        sections: List[str] = []
        joined: Set[str] = {anchor}
        unreachable: List[str] = []

        for target in wanted[1:]:
            path = _shortest_path(anchor, target, edges)
            if path is None:
                unreachable.append(target)
                continue
            joined.update(step[0] for step in path)
            sections.append(_render_path(anchor, target, path))

        text_parts: List[str] = []
        if sections:
            text_parts.append("\n\n".join(sections))
            bridges = sorted(joined - set(wanted))
            if bridges:
                text_parts.append(
                    "Tables needed only to connect the ones you asked for: "
                    + ", ".join(bridges)
                    + ". Include them in FROM/JOIN but do not select from them "
                    "unless the question asks for their columns."
                )

        if unreachable:
            hints = sorted({n for (n, _, _) in edges.get(anchor, set())})
            hints = hints[:_MAX_BRIDGE_HINTS]
            hint_text = (
                f" {anchor} joins directly to: {', '.join(hints)}." if hints else ""
            )
            text_parts.append(
                f"No join path of {_MAX_HOPS} hops or fewer connects {anchor} "
                f"to: {', '.join(unreachable)}.{hint_text} These may belong to "
                "unrelated subject areas -- say so rather than inventing a join."
            )

        text = "\n\n".join(text_parts)
        summary = (
            f"Join path for {len(wanted)} table(s)"
            if sections
            else "No join path found"
        )
        return _ok(
            text,
            summary,
            metadata={
                "tables": wanted,
                "bridges": sorted(joined - set(wanted)),
                "unreachable": unreachable,
            },
        )


def _shortest_path(
    start: str, goal: str, edges: Dict[str, Set[Tuple[str, str, str]]]
) -> Optional[List[Tuple[str, str, str]]]:
    """Breadth-first, so the first path found is the one with fewest joins.

    Returns the steps taken, each ``(table reached, left column, right column)``,
    or None when the goal is further than :data:`_MAX_HOPS`.
    """
    if start == goal:
        return []

    queue: deque = deque([(start, [])])
    seen: Set[str] = {start}

    while queue:
        current, path = queue.popleft()
        if len(path) >= _MAX_HOPS:
            continue
        # Sorted so the same schema always yields the same suggestion; an
        # unordered set makes the tool's output vary between identical calls.
        for neighbour, own_column, their_column in sorted(edges.get(current, set())):
            if neighbour in seen:
                continue
            step = path + [(neighbour, own_column, their_column)]
            if neighbour == goal:
                return step
            seen.add(neighbour)
            queue.append((neighbour, step))

    return None


def _render_path(anchor: str, target: str, path: List[Tuple[str, str, str]]) -> str:
    hops = len(path)
    header = f"{anchor} -> {target} ({hops} join{'s' if hops != 1 else ''})"
    lines = [header]
    left = anchor
    for reached, left_column, right_column in path:
        lines.append(
            f"  JOIN {reached} ON {left}.{left_column} = {reached}.{right_column}"
        )
        left = reached
    return "\n".join(lines)


def _ok(text: str, summary: str, metadata: Optional[dict] = None) -> ToolResult:
    return ToolResult(
        success=True,
        result_for_llm=text,
        ui_component=UiComponent(
            rich_component=RichTextComponent(content=summary, markdown=False),
            simple_component=SimpleTextComponent(text=summary),
        ),
        metadata=metadata or {},
    )


def _failure(message: str) -> ToolResult:
    return ToolResult(
        success=False,
        result_for_llm=message,
        ui_component=UiComponent(
            rich_component=RichTextComponent(content=message, markdown=False),
            simple_component=SimpleTextComponent(text=message),
        ),
        error=message,
    )
