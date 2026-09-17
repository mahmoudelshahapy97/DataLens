"""Read the approved value dictionary for free, instead of probing the warehouse.

``check_column_values`` (``vanna/tools/column_values.py``) answers "what does
this column actually contain" by querying the database -- correct, but a
round trip, and it cannot see anything not currently in the table. This tool
answers the same question from ``ValueStore``, the curated dictionary a
workspace has already reviewed. Its highest-value payload is coded-value
labels (``'C' = 'Cancelled'``) that no string similarity the database-probing
tool runs would ever find.

Two-step output, matching ``search_tables``/``get_table_schema``: list mode is
an inventory of which columns have a dictionary, and a ``table``+``column``
drill-down returns one column's values and labels in full.

Two disclosure rules, both non-negotiable:

* **Only approved values.** ``ValueStore.dictionary_for`` assembles from
  ``ReviewStatus.APPROVED`` samples only -- this tool must never call
  ``list_samples`` without a status filter, which would surface ``PENDING``
  values nobody has agreed may reach a prompt.
* **Grant-aware.** A column's grant can be revoked after it was sampled, so
  this consults ``column_uses`` (via ``getattr``, since a plain catalog lacks
  it) before showing a dictionary. ``None`` means enforcement is off for this
  caller, not that anything is denied.
"""

from __future__ import annotations

from typing import Any, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.values import ReviewStatus, ValueStore
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Values shown per column drill-down before truncating. Announced when hit,
#: never silently dropped -- a model told nothing is missing will assert that
#: what is missing does not exist.
_MAX_VALUES_SHOWN = 40


class ListKnownValuesArgs(BaseModel):
    """Arguments for list_known_values."""

    table: Optional[str] = Field(
        default=None, description="Table to inspect. Omit for a full inventory."
    )
    column: Optional[str] = Field(
        default=None,
        description="Column to inspect, together with table. Omit for the "
        "inventory of that table's columns with a value dictionary.",
    )


class ListKnownValuesTool(Tool[ListKnownValuesArgs]):
    """Reads the workspace's curated, approved value dictionary.

    Args:
        value_store: The curated dictionary. Register this tool only when
            one exists (``Platform.values`` is optional) -- an unconditional
            registration would ``AttributeError`` inside ``execute``.
        catalog: Consulted (via `getattr`) to withhold a revoked column's
            values even though they were sampled and approved before the
            revocation.
    """

    #: Takes plain-English identifiers, never SQL.
    sql_argument_fields: tuple = ()

    def __init__(self, value_store: ValueStore, catalog: Any) -> None:
        self.value_store = value_store
        self.catalog = catalog

    @property
    def name(self) -> str:
        return "list_known_values"

    @property
    def description(self) -> str:
        return (
            "Look up the workspace's curated, human-approved value dictionary "
            "for a column -- including readable labels for coded values (e.g. "
            "'C' = 'Cancelled') that no fuzzy match would find. Call with no "
            "arguments for an inventory of which columns have one; call with "
            "table and column for that column's values."
        )

    def get_args_schema(self) -> Type[ListKnownValuesArgs]:
        return ListKnownValuesArgs

    async def execute(
        self, context: ToolContext, args: ListKnownValuesArgs
    ) -> ToolResult:
        if args.table and args.column:
            return await self._column(context, args.table, args.column)
        return await self._inventory(context, args.table)

    async def _column_readable(
        self, context: ToolContext, table: str, column: str
    ) -> bool:
        column_uses = getattr(self.catalog, "column_uses", None)
        if column_uses is None:
            return True
        try:
            uses = await column_uses(context)
        except Exception:
            return True  # cannot confirm a revocation; do not withhold on a lookup error
        if uses is None:
            return True  # unenforced for this caller
        columns = uses.get(table.lower()) or uses.get(table)
        return bool(columns and (column.lower() in columns or column in columns))

    async def _inventory(
        self, context: ToolContext, table: Optional[str]
    ) -> ToolResult:
        try:
            # status=APPROVED, never omitted -- list_samples with no status
            # filter would include PENDING values, which nobody has agreed may
            # reach a prompt.
            samples = await self.value_store.list_samples(
                context, table=table, status=ReviewStatus.APPROVED, limit=2000
            )
        except Exception as e:
            return _failure(f"Could not read the value dictionary: {e}")

        columns: dict = {}
        for s in samples:
            columns.setdefault((s.table, s.column), 0)
            columns[(s.table, s.column)] += 1

        if not columns:
            text = (
                "No approved value dictionary exists"
                + (f" for {table}" if table else "")
                + ". check_column_values can still probe the database directly."
            )
            return _ok(text, text)

        rows = [f"- {t}.{c} ({n} value(s))" for (t, c), n in sorted(columns.items())]
        text = "\n".join(rows)
        text += "\n\nCall again with table and column to see one column's values."
        return _ok(
            text,
            f"{len(columns)} column(s) with a value dictionary",
            metadata={"columns": [f"{t}.{c}" for t, c in columns]},
        )

    async def _column(
        self, context: ToolContext, table: str, column: str
    ) -> ToolResult:
        if not await self._column_readable(context, table, column):
            text = (
                f"You no longer have access to {table}.{column}, so its value "
                "dictionary cannot be shown."
            )
            return _ok(text, text)

        try:
            dictionary = await self.value_store.dictionary_for(
                context, table=table, column=column
            )
        except Exception as e:
            return _failure(f"Could not read the value dictionary: {e}")

        if dictionary.is_empty:
            text = (
                f"No approved values are recorded for {table}.{column}. Do not "
                "assume a value exists just because it seems plausible."
            )
            return _ok(text, text)

        values = list(dictionary.values)
        shown = values[:_MAX_VALUES_SHOWN]
        lines = []
        for v in shown:
            label = dictionary.labels.get(v)
            synonyms = dictionary.synonyms.get(v)
            extra = []
            if label:
                extra.append(f"label={label!r}")
            if synonyms:
                extra.append(f"also called {', '.join(synonyms)}")
            suffix = f" ({'; '.join(extra)})" if extra else ""
            lines.append(f"  - {v!r}{suffix}")

        text = f"Approved values for {table}.{column}:\n" + "\n".join(lines)
        if len(values) > len(shown):
            text += f"\n\n{len(values) - len(shown)} of {len(values)} values not shown."

        return _ok(
            text,
            f"{len(values)} approved value(s) for {table}.{column}",
            metadata={"table": table, "column": column, "count": len(values)},
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
