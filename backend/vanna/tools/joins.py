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
cheapest set of joins connecting these tables**, including the intermediate
tables nobody named. Its description says so explicitly, because the model
otherwise has no way to tell it apart from ``get_table_schema``.

The answer is one join *tree* over all requested tables (see
``vanna.capabilities.schema_graph``), not a separate path from the first table
to each of the others -- separate paths can route through different bridges
and describe a join with a cycle in it. The tree also says when it fans out
one-to-many in two directions from one table, which is where SUMs come back
multiplied.
"""

from __future__ import annotations

from typing import List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.schema_catalog import SchemaCatalog
from vanna.capabilities.schema_graph import (
    MAX_HOPS,
    SchemaGraph,
    SteinerResult,
    fan_traps,
    load_schema_graph,
    table_lookup,
)
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

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

        # Accepts the bare name, the qualified name, and either in any case --
        # the model reliably supplies a different form from the one the
        # catalog stores.
        canonical = table_lookup(tables)

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

        graph, _ = await load_schema_graph(
            self.catalog, context, data_source_id=self.data_source_id, tables=tables
        )
        if not graph:
            text = (
                "The catalog records no relationships or foreign keys for this "
                "data source, so no join path can be derived. Fall back to "
                "get_table_schema and join on the primary/foreign key columns "
                "you can see."
            )
            return _ok(text, text)

        return self._describe_tree(wanted, graph)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _describe_tree(self, wanted: List[str], graph: SchemaGraph) -> ToolResult:
        tree = graph.steiner_tree(wanted, max_hops=MAX_HOPS)
        anchor = tree.anchor
        bridges = tree.bridges(wanted)
        traps = fan_traps(tree.edges)

        text_parts: List[str] = []
        if tree.edges:
            text_parts.append(_render_tree(tree))
            if bridges:
                text_parts.append(
                    "Tables needed only to connect the ones you asked for: "
                    + ", ".join(bridges)
                    + ". Include them in FROM/JOIN but do not select from them "
                    "unless the question asks for their columns."
                )
            for hub, targets in traps:
                text_parts.append(
                    f"Row multiplication warning: {hub} joins one-to-many to "
                    f"both {' and '.join(targets)}. Each {hub} row is repeated "
                    "once per combination of their rows, so a SUM or COUNT over "
                    "this join comes back too large. Aggregate each branch "
                    "separately (a subquery or CTE per branch, grouped by the "
                    f"{hub} key) and join the aggregates."
                )

        if tree.unreachable:
            hints = graph.neighbours(anchor)[:_MAX_BRIDGE_HINTS]
            hint_text = (
                f" {anchor} joins directly to: {', '.join(hints)}." if hints else ""
            )
            text_parts.append(
                f"No join path of {MAX_HOPS} hops or fewer connects {anchor} "
                f"to: {', '.join(tree.unreachable)}.{hint_text} These may belong to "
                "unrelated subject areas -- say so rather than inventing a join."
            )

        text = "\n\n".join(text_parts)
        summary = (
            f"Join path for {len(wanted)} table(s)"
            if tree.edges
            else "No join path found"
        )
        return _ok(
            text,
            summary,
            metadata={
                "tables": wanted,
                "bridges": bridges,
                "unreachable": list(tree.unreachable),
                "fan_traps": [{"hub": hub, "tables": t} for hub, t in traps],
            },
        )


def _render_tree(tree: SteinerResult) -> str:
    hops = len(tree.edges)
    header = (
        f"Join tree for {', '.join(tree.tables)} "
        f"({hops} join{'s' if hops != 1 else ''})"
    )
    lines = [header, f"  FROM {tree.anchor}"]
    for edge in tree.edges:
        lines.append(
            f"  JOIN {edge.right} ON {edge.left}.{edge.left_column} = "
            f"{edge.right}.{edge.right_column}"
        )
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
