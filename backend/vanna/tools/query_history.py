"""Let the agent read back its own generation history.

`GenerationStore` is already written on every question by
`RecordingRunSqlTool`, but nothing reads it back into a conversation --
so "the one from yesterday" or "why did that query fail last time" have no
answer today. This tool is that answer, and it is neither advisory
(`search_saved_correct_tool_uses`) nor authoritative
(`search_knowledge`): it is just a record of what happened, and the
description says so.

Three defences, all load-bearing:

* **Tenant.** `GenerationStore` implementations scope reads to
  ``context.tenant_id``.
* **User.** `GenerationStore.list_recent` is tenant-scoped but *not*
  user-scoped (`vanna_app/stores.py`), so this tool filters
  ``record.user_id == context.user.id`` itself, unconditionally, with no
  scope argument -- a viewer must never read an admin's questions and SQL. A
  workspace-wide history is a *separate* tool, registered ``["admin"]``, if
  ever wanted.
* **Grant drift.** SQL can reference a table whose grant was revoked *after*
  the query ran, so every record's referenced tables are checked against the
  caller's current catalog (already a ``GrantFilteredCatalog`` when
  enforcement is on) before it is shown at all. Suppressed records are
  counted, never silently dropped.

Two-step output, matching ``search_tables``/``get_table_schema``: list mode
returns no SQL at all (a 200-row dump with SQL is worse than no tool), and a
``generation_id`` drill-down returns the full SQL for one entry.
"""

from __future__ import annotations

from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.knowledge import extract_tables
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.generation import GenerationStore
from vanna.core.tool import Tool, ToolContext, ToolResult

_QUESTION_PREVIEW = 100


class SearchQueryHistoryArgs(BaseModel):
    """Arguments for search_query_history."""

    question: Optional[str] = Field(
        default=None,
        description="Filter to generations whose question contains this text "
        "(case-insensitive substring). Omit to list your most recent generations.",
    )
    generation_id: Optional[str] = Field(
        default=None,
        description="Return full detail, including the SQL, for one generation "
        "id from a previous list result. Omit for a list.",
    )
    limit: int = Field(default=10, ge=1, le=50)


class SearchQueryHistoryTool(Tool[SearchQueryHistoryArgs]):
    """Reads back the caller's own past generations: what ran, and how it went.

    Args:
        generation_store: Records of every generation, written elsewhere.
        catalog: Consulted (via `getattr`, since a plain `SchemaCatalog` lacks
            it) to suppress records whose tables the caller can no longer read.
    """

    #: Takes plain-English filters and an opaque id, never SQL.
    sql_argument_fields: tuple = ()

    def __init__(self, generation_store: GenerationStore, catalog: Any) -> None:
        self.generation_store = generation_store
        self.catalog = catalog

    @property
    def name(self) -> str:
        return "search_query_history"

    @property
    def description(self) -> str:
        return (
            "Look up YOUR OWN past questions and what happened when they ran -- "
            "this is neither verified knowledge nor agent memory, just a plain "
            "record of history (what ran, whether it succeeded, how many rows "
            "came back). Use this for 'the one from yesterday' or 'why did that "
            "fail'. Returns a list without SQL first; pass generation_id to see "
            "the full SQL for one entry."
        )

    def get_args_schema(self) -> Type[SearchQueryHistoryArgs]:
        return SearchQueryHistoryArgs

    async def execute(
        self, context: ToolContext, args: SearchQueryHistoryArgs
    ) -> ToolResult:
        if args.generation_id:
            return await self._detail(context, args.generation_id)
        return await self._list(context, args)

    # ------------------------------------------------------------------

    async def _visible_and_own(self, context: ToolContext, limit: int):
        try:
            records = await self.generation_store.list_recent(context, limit=limit)
        except Exception as e:
            return None, 0, str(e)

        mine = [r for r in records if r.user_id == context.user.id]

        visible = []
        suppressed = 0
        for record in mine:
            ok = await self._table_visible(context, record.sql)
            if ok:
                visible.append(record)
            else:
                suppressed += 1
        return visible, suppressed, None

    async def _table_visible(self, context: ToolContext, sql: str) -> bool:
        """False only when a referenced table is confirmed unreadable now."""
        if not sql:
            return True
        get_table = getattr(self.catalog, "get_table", None)
        if get_table is None:
            return True
        for table in extract_tables(sql):
            try:
                found = await get_table(context, table)
            except Exception:
                continue  # cannot confirm a revocation; do not suppress on a lookup error
            if found is None:
                return False
        return True

    async def _list(
        self, context: ToolContext, args: SearchQueryHistoryArgs
    ) -> ToolResult:
        # Over-fetch past the requested limit before filtering by question text,
        # so a text filter does not silently shrink the effective page size.
        visible, suppressed, error = await self._visible_and_own(
            context, max(args.limit * 5, 50)
        )
        if error is not None:
            return _failure(f"Could not read query history: {error}")

        if args.question:
            needle = args.question.casefold()
            visible = [r for r in visible if needle in r.question.casefold()]

        visible = visible[: args.limit]

        if not visible:
            text = "No past generations found for this filter and caller."
            if suppressed:
                text += (
                    f" ({suppressed} older entr{'y' if suppressed == 1 else 'ies'} "
                    "not shown because you no longer have access to a table involved.)"
                )
            return _ok(text, text)

        rows = []
        for r in visible:
            preview = r.question[:_QUESTION_PREVIEW]
            if len(r.question) > _QUESTION_PREVIEW:
                preview += "..."
            rows.append(
                f"- id={r.id} | {r.created_at.isoformat()} | {r.status.value} | "
                f"rows={r.row_count if r.row_count is not None else '?'} | {preview!r}"
            )

        text = "\n".join(rows)
        text += "\n\nCall again with generation_id to see the full SQL for one entry."
        if suppressed:
            text += (
                f"\n\n{suppressed} older entr{'y' if suppressed == 1 else 'ies'} not "
                "shown because you no longer have access to a table involved."
            )

        return _ok(
            text,
            f"{len(visible)} generation(s)",
            metadata={"ids": [r.id for r in visible], "suppressed": suppressed},
        )

    async def _detail(self, context: ToolContext, generation_id: str) -> ToolResult:
        try:
            record = await self.generation_store.get(context, generation_id)
        except Exception as e:
            return _failure(f"Could not read query history: {e}")

        if record is None or record.user_id != context.user.id:
            # Same message for "not found" and "not yours" -- a 403-shaped
            # response on someone else's id would confirm the id exists.
            text = f"No generation with id {generation_id!r} found."
            return _ok(text, text)

        if not await self._table_visible(context, record.sql):
            text = (
                "That generation referenced a table you no longer have access "
                "to, so its detail cannot be shown."
            )
            return _ok(text, text)

        text = (
            f"Question: {record.question}\n"
            f"Status: {record.status.value}\n"
            f"Rows: {record.row_count if record.row_count is not None else '?'}\n"
            f"SQL:\n{record.sql}"
        )
        if record.error:
            text += f"\nError: {record.error}"

        return _ok(text, f"Generation {generation_id}", metadata={"id": record.id})


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
