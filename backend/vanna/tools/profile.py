"""Describe what is actually in a column, so the model stops reasoning from its name.

``check_column_values`` (``vanna/tools/column_values.py``) answers "does this
particular value exist". That is the right question when the model already has
a literal in mind, and the wrong one when it does not: a column called
``status`` might hold four values or four million, might be 90% NULL, and might
store its dates as text. Each of those changes the correct query, and none of
them is visible in the name.

The shape of the failure this removes: the model writes ``AVG(amount)`` over a
column where two thirds of the rows are NULL and reports the average as if it
covered everything, or it writes ``GROUP BY customer_id`` on a column with a
million distinct values and returns a million rows.

Cheapest source first, the same order ``list_known_values`` uses:

* **The catalog** -- ``low_cardinality``, ``categories``, ``sample_values`` and
  the table's ``row_count_estimate`` were captured at scan time and cost
  nothing to read.
* **The database** -- one bounded aggregate, and a second top-N query only when
  the column turns out to have few enough distinct values for that to be
  meaningful.

``COUNT(DISTINCT ...)`` is not free on a large table. It is bounded here the
same way ``check_column_values`` bounds its ``DISTINCT`` scan: by the runner's
own statement timeout, which is the only limit that applies to an aggregate,
since a row cap cannot shrink a query that returns one row.
"""

from __future__ import annotations

from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

# Deliberately imported rather than redefined. Table and column names reach both
# tools as model-generated strings and are interpolated into SQL, because a
# parameter marker is not valid in an identifier position. Two copies of a
# security-relevant whitelist are two things that can drift apart.
from .column_values import _IDENTIFIER_RE

#: Distinct values at or below which a frequency breakdown is worth a second
#: query. Above it the breakdown is noise, and the count alone is the answer.
_TOP_N_THRESHOLD = 25

#: Rows returned by the frequency breakdown.
_TOP_N = 10


class ProfileColumnArgs(BaseModel):
    """Arguments for profile_column."""

    table: str = Field(description="Table name, optionally schema-qualified")
    column: str = Field(description="Column to profile")


class ProfileColumnTool(Tool[ProfileColumnArgs]):
    """Reports a column's shape: nulls, distinct count, range, common values.

    Args:
        sql_runner: Executes the aggregates. Its read-only enforcement and
            statement timeout apply.
        catalog: Consulted first, so a column the scanner already profiled
            costs no database round trip at all.
        data_source_id: Scopes catalog lookups the way the schema tools do.
    """

    #: Takes identifiers, never SQL. The SQL is built here and the identifiers
    #: are whitelisted; nothing the model writes reaches the database verbatim.
    sql_argument_fields: tuple = ()

    def __init__(
        self,
        sql_runner: SqlRunner,
        *,
        catalog: Any = None,
        data_source_id: Optional[str] = None,
    ) -> None:
        self.sql_runner = sql_runner
        self.catalog = catalog
        self.data_source_id = data_source_id

    @property
    def name(self) -> str:
        return "profile_column"

    @property
    def description(self) -> str:
        return (
            "Describe what a column actually contains: how many rows are NULL, "
            "how many distinct values it has, its minimum and maximum, and its "
            "most common values. Use this before averaging, summing or grouping "
            "by a column whose contents you have not seen -- a column that is "
            "mostly NULL or has far more distinct values than you expect makes "
            "the obvious query the wrong one. To check whether one specific "
            "value exists, use check_column_values instead."
        )

    def get_args_schema(self) -> Type[ProfileColumnArgs]:
        return ProfileColumnArgs

    async def execute(
        self, context: ToolContext, args: ProfileColumnArgs
    ) -> ToolResult:
        table, column = args.table.strip(), args.column.strip()

        for label, identifier in (("table", table), ("column", column)):
            if not _IDENTIFIER_RE.match(identifier):
                return _failure(
                    f"{label.capitalize()} name {identifier!r} is not a valid identifier."
                )

        sections: List[str] = []
        catalog_facts = await self._from_catalog(context, table, column)
        if catalog_facts:
            sections.append(catalog_facts)

        try:
            stats = await self._aggregate(context, table, column)
        except Exception as e:
            # The catalog half is still worth returning: a scanned column may
            # already answer the question without the database being reachable.
            if sections:
                sections.append(
                    f"The live profile could not be read ({e}). The facts above "
                    "come from the catalog and may be out of date."
                )
                return _ok(
                    "\n\n".join(sections), f"Catalog profile for {table}.{column}"
                )
            return _failure(f"Could not profile {table}.{column}: {e}")

        sections.append(stats["text"])

        if 0 < stats["distinct"] <= _TOP_N_THRESHOLD:
            try:
                sections.append(await self._top_values(context, table, column))
            except Exception:
                pass  # the breakdown is a bonus; the aggregate already answered

        return _ok(
            "\n\n".join(sections),
            f"Profiled {table}.{column}",
            metadata={
                "table": table,
                "column": column,
                "rows": stats["rows"],
                "nulls": stats["nulls"],
                "distinct": stats["distinct"],
            },
        )

    # ------------------------------------------------------------------

    async def _from_catalog(
        self, context: ToolContext, table: str, column: str
    ) -> Optional[str]:
        """Facts captured at scan time. Free, and sometimes the whole answer."""
        if self.catalog is None:
            return None
        try:
            meta = await self.catalog.get_table(
                context, table, data_source_id=self.data_source_id
            )
        except Exception:
            return None
        if meta is None:
            return None

        found = next(
            (c for c in (meta.columns or []) if c.name.lower() == column.lower()), None
        )
        if found is None:
            return None

        lines = [f"{table}.{column} ({found.data_type})"]
        if found.description:
            lines.append(f"  Described as: {found.description}")
        lines.append(f"  Nullable: {'yes' if found.nullable else 'no'}")
        if found.is_primary_key:
            lines.append("  Primary key, so every value is distinct and non-NULL.")
        if meta.row_count_estimate is not None:
            lines.append(f"  Table holds roughly {meta.row_count_estimate:,} rows.")
        if found.low_cardinality and found.categories:
            lines.append(
                f"  Known categories ({len(found.categories)}): "
                + ", ".join(repr(c) for c in found.categories[:_TOP_N_THRESHOLD])
            )
        elif found.sample_values:
            lines.append(
                "  Sample values: "
                + ", ".join(repr(v) for v in found.sample_values[:5])
            )
        return "\n".join(lines)

    async def _aggregate(self, context: ToolContext, table: str, column: str) -> dict:
        """One portable aggregate. Every dialect here supports all five."""
        sql = (
            f"SELECT COUNT(*) AS total_rows, "
            f"COUNT({column}) AS non_null, "
            f"COUNT(DISTINCT {column}) AS distinct_values, "
            f"MIN({column}) AS min_value, "
            f"MAX({column}) AS max_value "
            f"FROM {table}"
        )
        frame = await self.sql_runner.run_sql(RunSqlToolArgs(sql=sql), context)
        row = frame.iloc[0]

        total = int(row["total_rows"])
        non_null = int(row["non_null"])
        distinct = int(row["distinct_values"])
        nulls = total - non_null

        lines = [f"Live profile of {table}.{column}:"]
        lines.append(f"  Rows: {total:,}")
        if total:
            share = nulls / total * 100
            lines.append(f"  NULL: {nulls:,} ({share:.1f}%)")
            # Said outright rather than left as a percentage to interpret. A
            # model that reads "38% NULL" still tends to write a bare AVG().
            if share >= 10:
                lines.append(
                    "  A large share of this column is NULL. Aggregates ignore "
                    "NULLs, so say what the figure covers, or filter with "
                    "IS NOT NULL deliberately."
                )
        lines.append(f"  Distinct values: {distinct:,}")
        if total and distinct == total and total > 1:
            lines.append(
                "  Every value is unique -- grouping by it returns one row each."
            )
        lines.append(f"  Range: {row['min_value']!r} to {row['max_value']!r}")

        return {
            "text": "\n".join(lines),
            "rows": total,
            "nulls": nulls,
            "distinct": distinct,
        }

    async def _top_values(self, context: ToolContext, table: str, column: str) -> str:
        sql = (
            f"SELECT {column} AS value, COUNT(*) AS occurrences "
            f"FROM {table} "
            f"WHERE {column} IS NOT NULL "
            f"GROUP BY {column} "
            f"ORDER BY occurrences DESC "
            f"LIMIT {_TOP_N}"
        )
        frame = await self.sql_runner.run_sql(RunSqlToolArgs(sql=sql), context)
        lines = ["Most common values:"]
        for _, row in frame.iterrows():
            lines.append(f"  {row['value']!r}: {int(row['occurrences']):,}")
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
