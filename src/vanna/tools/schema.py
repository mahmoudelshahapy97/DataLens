"""Tools that let the agent read the schema catalog.

``RetrievalContextEnhancer`` already injects schema into the system prompt
before the model runs, which handles the common case with no round trip. But
injection alone is not sufficient:

* On a **large schema** the enhancer switches to relevance search and shows the
  top N tables. If the one the model needs is N+1, it has no recourse -- and a
  model with no way to ask will invent a table name rather than admit the gap.
* A **follow-up question** often pivots to a different part of the schema than
  the opening one, and the injected context was selected for the opening one.
* **Exploratory questions** ("what data do you have about customers?") are
  answered directly from the catalog, with no SQL at all.

So injection is the fast path and these tools are the escape hatch. Both read
the same tenant-scoped catalog, so they can never disagree.
"""

from __future__ import annotations

from typing import List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.schema_catalog import (
    SchemaCatalog,
    describe_schema,
    describe_table_names,
)
from vanna.components import (
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.tool import Tool, ToolContext, ToolResult


class SearchTablesArgs(BaseModel):
    """Arguments for search_tables."""

    query: str = Field(
        description="What you are looking for, in plain words "
        "(e.g. 'customer orders and payments')"
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum tables to return")


class GetTableSchemaArgs(BaseModel):
    """Arguments for get_table_schema."""

    tables: List[str] = Field(
        description="Table names to describe. Accepts bare or schema-qualified "
        "names (e.g. ['orders', 'public.customers'])."
    )


class SearchTablesTool(Tool[SearchTablesArgs]):
    """Finds tables relevant to a description, returning names and summaries.

    Returns an inventory rather than full column detail: the point is to let
    the model narrow down, then call ``get_table_schema`` for the two or three
    it actually needs. Returning every column of every candidate would defeat
    the purpose by blowing the same context budget the search was meant to save.
    """

    def __init__(
        self, catalog: SchemaCatalog, *, data_source_id: Optional[str] = None
    ) -> None:
        self.catalog = catalog
        self.data_source_id = data_source_id

    @property
    def name(self) -> str:
        return "search_tables"

    @property
    def description(self) -> str:
        return (
            "Find tables in the database relevant to a description. Use this "
            "when the schema you were given does not contain the table you "
            "need, or when the user asks what data is available. Returns table "
            "names and summaries -- call get_table_schema for column detail."
        )

    def get_args_schema(self) -> Type[SearchTablesArgs]:
        return SearchTablesArgs

    async def execute(
        self, context: ToolContext, args: SearchTablesArgs
    ) -> ToolResult:
        try:
            tables = await self.catalog.search_tables(
                context,
                args.query,
                limit=args.limit,
                data_source_id=self.data_source_id,
            )
        except Exception as e:
            message = f"Could not search the schema catalog: {e}"
            return _failure(message)

        if not tables:
            text = (
                f"No tables matching {args.query!r} were found in the catalog. "
                "The catalog may not be populated, or this data may not exist. "
                "Tell the user rather than guessing at a table name."
            )
            return _ok(text, text)

        listing = describe_table_names(tables)
        text = (
            f"{listing}\n\nCall get_table_schema with the names you need to see "
            "their columns."
        )
        return _ok(
            text,
            f"Found {len(tables)} relevant tables",
            metadata={"tables": [t.qualified_name for t in tables]},
        )


class GetTableSchemaTool(Tool[GetTableSchemaArgs]):
    """Returns full column detail for named tables.

    The description includes captured enum values for low-cardinality columns,
    which is the highest-value part of the payload: it removes the need to guess
    a filter literal, which is the most common cause of a query that runs
    perfectly and returns nothing.
    """

    def __init__(
        self, catalog: SchemaCatalog, *, data_source_id: Optional[str] = None
    ) -> None:
        self.catalog = catalog
        self.data_source_id = data_source_id

    @property
    def name(self) -> str:
        return "get_table_schema"

    @property
    def description(self) -> str:
        return (
            "Get the columns, types, keys, and known values for specific "
            "tables. Use this before writing SQL against a table whose schema "
            "you have not been shown."
        )

    def get_args_schema(self) -> Type[GetTableSchemaArgs]:
        return GetTableSchemaArgs

    async def execute(
        self, context: ToolContext, args: GetTableSchemaArgs
    ) -> ToolResult:
        found = []
        missing = []

        for name in args.tables:
            try:
                table = await self.catalog.get_table(
                    context, name, data_source_id=self.data_source_id
                )
            except Exception as e:
                return _failure(f"Could not read the schema catalog: {e}")
            if table is None:
                missing.append(name)
            else:
                found.append(table)

        if not found:
            # Naming the misses explicitly matters. A model told "not found"
            # can correct course; a model handed an empty response tends to
            # assume the table exists and proceed anyway.
            text = (
                f"None of these tables exist in the catalog: "
                f"{', '.join(missing)}. Use search_tables to find the correct "
                "name, or tell the user the data is not available."
            )
            return _ok(text, text, metadata={"missing": missing})

        relationships = []
        try:
            names = {t.qualified_name for t in found}
            all_rels = await self.catalog.get_relationships(
                context, data_source_id=self.data_source_id
            )
            # Include an edge when either end is in scope: a join path pointing
            # at a table the model has not fetched is exactly the hint it needs
            # to fetch that table next.
            relationships = [
                r for r in all_rels if r.from_table in names or r.to_table in names
            ]
        except Exception:
            pass  # relationships are a bonus, not a requirement

        text = describe_schema(found, relationships)
        if missing:
            text += (
                f"\n\nNot found in the catalog: {', '.join(missing)}. "
                "Do not reference these in SQL."
            )

        return _ok(
            text,
            f"Retrieved schema for {len(found)} table(s)",
            metadata={
                "found": [t.qualified_name for t in found],
                "missing": missing,
            },
        )


def create_schema_tools(
    catalog: SchemaCatalog, *, data_source_id: Optional[str] = None
) -> List[Tool]:
    """Build both schema tools for a catalog.

        registry.register_local_tool(t, []) for t in create_schema_tools(catalog)
    """
    return [
        SearchTablesTool(catalog, data_source_id=data_source_id),
        GetTableSchemaTool(catalog, data_source_id=data_source_id),
    ]


# ----------------------------------------------------------------------
# Result helpers
# ----------------------------------------------------------------------


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
