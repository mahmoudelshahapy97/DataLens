"""Check which columns of a table an admin curated as the ones that matter.

Sibling to ``check_column_values`` (``vanna/tools/column_values.py``), which
verifies a filter *value* before the model guesses one. This does the same
for column *names*: an admin can mark a subset of a table's columns as
"core" from the schema screen, and this tool lets the agent confirm which
columns those are, or check specific columns it is about to use against
that set, instead of guessing which columns matter most for a table with
many of them.
"""

from __future__ import annotations

from typing import List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.agent_memory import tenant_scope
from vanna.components import (
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.grants import normalize_identifier, normalize_table
from vanna.core.tool import Tool, ToolContext, ToolResult


class CheckCoreColumnsArgs(BaseModel):
    """Arguments for check_core_columns."""

    table: str = Field(
        description="Table name, optionally schema-qualified (e.g. 'orders' "
        "or 'public.orders')"
    )
    columns: Optional[List[str]] = Field(
        default=None,
        description="Columns you are about to use. Omit to just list the "
        "table's core columns.",
    )


class CheckCoreColumnsTool(Tool[CheckCoreColumnsArgs]):
    """Reports a table's curated core columns, or checks columns against them.

    Args:
        catalog: the control-plane catalog store. Needs ``get_table`` (to
            confirm the table exists) and ``get_core_columns_map`` (curation,
            not part of the general ``SchemaCatalog`` contract -- the same
            reason ``routes/catalog.py`` reaches for the concrete store
            rather than the ``SchemaCatalog`` interface).
        data_source_id: scopes lookups the same way the schema tools do.
    """

    #: No SQL in these arguments -- same reasoning as SearchTablesTool.
    sql_argument_fields: tuple = ()

    def __init__(self, catalog, *, data_source_id: Optional[str] = None) -> None:
        self.catalog = catalog
        self.data_source_id = data_source_id

    @property
    def name(self) -> str:
        return "check_core_columns"

    @property
    def description(self) -> str:
        return (
            "Check which columns of a table are marked 'core' by an admin -- "
            "the columns curated as the ones that matter most for this "
            "table. Call this before writing SQL against a table with many "
            "columns you have not confirmed core columns for, or to check "
            "whether specific columns you intend to use are among them."
        )

    def get_args_schema(self) -> Type[CheckCoreColumnsArgs]:
        return CheckCoreColumnsArgs

    async def execute(
        self, context: ToolContext, args: CheckCoreColumnsArgs
    ) -> ToolResult:
        table = await self.catalog.get_table(
            context, args.table, data_source_id=self.data_source_id
        )
        if table is None:
            text = (
                f"{args.table!r} was not found in the catalog. Use "
                "search_tables to find the correct name."
            )
            return _ok(text, text)

        tenant = tenant_scope(context)
        source = self.data_source_id or getattr(table, "data_source_id", None) or "default"
        table_key = normalize_table(table.qualified_name)

        try:
            core = (
                await self.catalog.get_core_columns_map(tenant, source, [table_key])
            ).get(table_key, [])
        except Exception as e:
            return _failure(f"Could not read core columns for {args.table}: {e}")

        if not core:
            text = f"No columns of {table.qualified_name} are marked core."
            return _ok(text, text, metadata={"table": table.qualified_name, "core": []})

        core_set = set(core)

        if args.columns is None:
            listed = "\n".join(f"  - {c}" for c in core)
            text = f"Core columns of {table.qualified_name}:\n{listed}"
            return _ok(
                text,
                f"{len(core)} core column(s) for {table.qualified_name}",
                metadata={"table": table.qualified_name, "core": core},
            )

        checked = [normalize_identifier(c) for c in args.columns]
        is_core = [c for c in checked if c in core_set]
        not_core = [c for c in checked if c not in core_set]

        lines = [f"Core columns of {table.qualified_name}: {', '.join(core)}."]
        if is_core:
            lines.append(f"Core: {', '.join(is_core)}.")
        if not_core:
            lines.append(
                f"Not core: {', '.join(not_core)} -- still valid columns if they "
                "exist, just not curated as core."
            )
        text = " ".join(lines)

        return _ok(
            text,
            f"Checked {len(checked)} column(s) against {len(core)} core column(s)",
            metadata={
                "table": table.qualified_name,
                "core": core,
                "is_core": is_core,
                "not_core": not_core,
            },
        )


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
