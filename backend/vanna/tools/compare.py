"""Compare one measure across two windows, without the self-join.

"How did Q3 compare to Q2" is a question models get wrong in a specific,
repeatable way. Written as a single query it needs either a self-join on a
shifted date range or a pair of correlated subqueries, and both are easy to
write in a form that runs and is wrong: a join that drops categories present in
one window but not the other, a filter applied after the aggregate, an
inclusive ``BETWEEN`` on both ends double-counting a boundary row.

The fix is not a better prompt. It is to stop asking for one query. This runs
the same aggregate twice, once per window, and does the alignment in Python --
an outer join, so a category missing from either side is reported as missing
rather than silently dropped. That last point is the whole reason this exists:
a product that sold nothing this quarter is exactly what the question is about,
and it is the row a self-join deletes.

The caller supplies SQL containing ``{start}`` and ``{end}`` placeholders. They
are substituted with the dates given as arguments, never with anything the
model wrote inline, so the two runs are guaranteed to differ only by window.
"""

from __future__ import annotations

from typing import Any, List, Optional, Type

import pandas as pd
from pydantic import BaseModel, Field

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Rows shown in the comparison before truncating. Announced when hit.
_MAX_ROWS_SHOWN = 30


class ComparePeriodsArgs(BaseModel):
    """Arguments for compare_periods."""

    sql: str = Field(
        description="Aggregate query with {start} and {end} placeholders in its "
        "date filter, e.g. "
        "SELECT category, SUM(total) AS revenue FROM sales "
        "WHERE sold_on >= '{start}' AND sold_on < '{end}' GROUP BY category. "
        "Use >= start and < end so a boundary row is not counted twice."
    )
    value_column: str = Field(description="The aggregated column to compare")
    label_column: Optional[str] = Field(
        default=None,
        description="Column identifying each group, e.g. category. Omit when "
        "the query returns a single total row.",
    )
    baseline_start: str = Field(description="First date of the earlier window")
    baseline_end: str = Field(description="Exclusive end of the earlier window")
    comparison_start: str = Field(description="First date of the later window")
    comparison_end: str = Field(description="Exclusive end of the later window")


class ComparePeriodsTool(Tool[ComparePeriodsArgs]):
    """Runs one aggregate over two windows and aligns the results.

    Args:
        sql_runner: Executes both queries. Its read-only enforcement, row cap
            and timeout apply to each.
    """

    #: The template is model-written SQL and must be policy-checked. It is
    #: checked before substitution, which is the conservative order: the
    #: placeholders only ever become date literals, so a template that passes
    #: cannot be widened by the values put into it.
    sql_argument_fields: tuple = ("sql",)

    def __init__(self, sql_runner: SqlRunner) -> None:
        self.sql_runner = sql_runner

    @property
    def name(self) -> str:
        return "compare_periods"

    @property
    def description(self) -> str:
        return (
            "Compare a measure between two date ranges -- quarter on quarter, "
            "year on year, before and after. Write the aggregate once with "
            "{start} and {end} placeholders; it is run for each window and the "
            "results are aligned for you, including groups present in only one "
            "window. Use this instead of writing a self-join or correlated "
            "subquery, which commonly drops exactly the rows the question is "
            "about."
        )

    def get_args_schema(self) -> Type[ComparePeriodsArgs]:
        return ComparePeriodsArgs

    async def execute(
        self, context: ToolContext, args: ComparePeriodsArgs
    ) -> ToolResult:
        if "{start}" not in args.sql or "{end}" not in args.sql:
            return _failure(
                "The query must contain {start} and {end} placeholders in its "
                "date filter, so the same aggregate can be run for both windows."
            )

        try:
            baseline = await self._window(
                context, args.sql, args.baseline_start, args.baseline_end
            )
            comparison = await self._window(
                context, args.sql, args.comparison_start, args.comparison_end
            )
        except Exception as e:
            return _failure(f"The query could not be run: {e}")

        for frame in (baseline, comparison):
            if args.value_column not in frame.columns:
                available = ", ".join(map(str, frame.columns)) or "none"
                return _failure(
                    f"value_column {args.value_column!r} is not in the result. "
                    f"Columns returned: {available}."
                )
            if args.label_column and args.label_column not in frame.columns:
                available = ", ".join(map(str, frame.columns)) or "none"
                return _failure(
                    f"label_column {args.label_column!r} is not in the result. "
                    f"Columns returned: {available}."
                )

        header = (
            f"Baseline {args.baseline_start} to {args.baseline_end} "
            f"vs comparison {args.comparison_start} to {args.comparison_end}."
        )

        if args.label_column is None:
            return _ok(
                header + "\n\n" + self._totals(baseline, comparison, args.value_column),
                "Compared two periods",
            )

        return self._by_group(header, baseline, comparison, args)

    # ------------------------------------------------------------------

    async def _window(
        self, context: ToolContext, template: str, start: str, end: str
    ) -> pd.DataFrame:
        sql = template.replace("{start}", start).replace("{end}", end)
        return await self.sql_runner.run_sql(RunSqlToolArgs(sql=sql), context)

    @staticmethod
    def _totals(
        baseline: pd.DataFrame, comparison: pd.DataFrame, value_column: str
    ) -> str:
        before = pd.to_numeric(baseline[value_column], errors="coerce").sum()
        after = pd.to_numeric(comparison[value_column], errors="coerce").sum()
        return (
            f"{value_column}:\n"
            f"  Baseline: {before:,.2f}\n"
            f"  Comparison: {after:,.2f}\n"
            f"  Change: {_delta(before, after)}"
        )

    def _by_group(
        self,
        header: str,
        baseline: pd.DataFrame,
        comparison: pd.DataFrame,
        args: ComparePeriodsArgs,
    ) -> ToolResult:
        label, value = args.label_column, args.value_column

        def indexed(frame: pd.DataFrame) -> pd.Series:
            numbers = pd.to_numeric(frame[value], errors="coerce")
            return pd.Series(numbers.values, index=frame[label].astype(str))

        before = indexed(baseline)
        after = indexed(comparison)

        # Outer, deliberately. An inner join here would delete the groups that
        # appeared or disappeared between the windows, which are usually the
        # most interesting rows in the answer.
        merged = pd.concat({"baseline": before, "comparison": after}, axis=1)
        merged["change"] = merged["comparison"].fillna(0) - merged["baseline"].fillna(0)
        merged = merged.reindex(
            merged["change"].abs().sort_values(ascending=False).index
        )

        appeared = [str(k) for k in merged.index[merged["baseline"].isna()]]
        gone = [str(k) for k in merged.index[merged["comparison"].isna()]]

        lines: List[str] = [f"{label:<28} {'baseline':>14} {'comparison':>14}  change"]
        for key, row in merged.head(_MAX_ROWS_SHOWN).iterrows():
            lines.append(
                f"{str(key)[:28]:<28} "
                f"{_number(row['baseline']):>14} "
                f"{_number(row['comparison']):>14}  "
                f"{_delta(row['baseline'], row['comparison'])}"
            )

        parts = [header, "\n".join(lines)]
        if len(merged) > _MAX_ROWS_SHOWN:
            parts.append(
                f"{len(merged) - _MAX_ROWS_SHOWN} further group(s) not shown, "
                "ordered by size of change."
            )
        if appeared:
            parts.append(
                "Present only in the comparison window: " + ", ".join(appeared)
            )
        if gone:
            parts.append(
                "Present only in the baseline window, and absent from the "
                "comparison: " + ", ".join(gone)
            )

        return _ok(
            "\n\n".join(parts),
            f"Compared {len(merged)} group(s) across two periods",
            metadata={
                "groups": len(merged),
                "appeared": appeared,
                "disappeared": gone,
            },
        )


def _number(value: Any) -> str:
    return "-" if pd.isna(value) else f"{float(value):,.2f}"


def _delta(before: Any, after: Any) -> str:
    """Absolute and relative change, with the cases that have no percentage."""
    missing_before, missing_after = pd.isna(before), pd.isna(after)
    if missing_before and missing_after:
        return "no data in either window"
    if missing_before:
        return f"new ({float(after):,.2f})"
    if missing_after:
        return f"gone (was {float(before):,.2f})"

    before, after = float(before), float(after)
    change = after - before
    if before == 0:
        # A percentage against a zero baseline is either undefined or infinite;
        # reporting it as "+100%" would be a fabrication.
        return f"{change:+,.2f} (no percentage: baseline is zero)"
    return f"{change:+,.2f} ({change / abs(before) * 100:+.1f}%)"


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
