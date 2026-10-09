"""Describe how a series moved over time, rather than handing over the rows.

A model asked "is revenue growing?" gets back two hundred monthly rows and
answers by eye. That works for a series with one obvious direction and fails
for everything else: it reads a two-month dip as a trend, misses an outlier
that is carrying the whole average, and cannot tell a seasonal pattern from
growth. None of that is a reasoning failure -- the arithmetic simply was not
done.

So this does the arithmetic. It runs the query, sorts by the date column, and
returns statements about the series: the direction across the whole window,
the largest single moves, values far enough from the rest to distort an
average, and -- only when there are enough points for the question to mean
anything -- whether the series repeats on a fixed period.

Deliberately not forecasting. A projection invites the model to state a future
number as a fact, and nothing downstream would mark it as an estimate.

Numbers are reported with the qualifications that make them true: a "trend" over
five points is called what it is, and seasonality is offered as a hint rather
than a finding, because autocorrelation on a short series is mostly noise.
"""

from __future__ import annotations

from typing import List, Optional, Type

import pandas as pd
from pydantic import BaseModel, Field

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Below this, a series is too short to describe as trending at all -- the
#: direction is whatever the endpoints happen to be.
_MIN_POINTS_FOR_TREND = 4

#: Seasonality needs at least two full cycles to be distinguishable from a
#: trend, so the longest period considered is a quarter of the series.
_MIN_POINTS_FOR_SEASONALITY = 12

#: Modified z-score above which a point is called out. 3.5 is the conventional
#: threshold for the median-absolute-deviation variant, which is used here
#: because a mean-based z-score is itself distorted by the outlier it is
#: looking for.
_OUTLIER_THRESHOLD = 3.5

#: Largest moves reported. Enough to show a pattern, few enough to read.
_MAX_MOVES = 3


class AnalyzeTimeseriesArgs(BaseModel):
    """Arguments for analyze_timeseries."""

    sql: str = Field(
        description="Query returning one row per period, with a date column "
        "and a numeric column. Sort order does not matter."
    )
    date_column: str = Field(description="Column holding the period")
    value_column: str = Field(description="Numeric column to analyse")


class AnalyzeTimeseriesTool(Tool[AnalyzeTimeseriesArgs]):
    """Computes trend, notable moves, outliers and seasonality for a series.

    Args:
        sql_runner: Executes the query. Its read-only enforcement, row cap and
            timeout all apply, exactly as they do for run_sql.
    """

    #: Runs a model-written query, so the safety policy must check it -- the
    #: same field, and the same treatment, as run_sql and validate_sql.
    sql_argument_fields: tuple = ("sql",)

    def __init__(self, sql_runner: SqlRunner) -> None:
        self.sql_runner = sql_runner

    @property
    def name(self) -> str:
        return "analyze_timeseries"

    @property
    def description(self) -> str:
        return (
            "Run a query returning a value per time period and get back the "
            "trend, the largest moves, any outliers distorting the average, "
            "and whether the series is seasonal. Use this for any question "
            "about growth, decline, trends or 'what changed' -- reading the "
            "rows and judging by eye mistakes a short dip for a trend and "
            "misses the outlier carrying the average."
        )

    def get_args_schema(self) -> Type[AnalyzeTimeseriesArgs]:
        return AnalyzeTimeseriesArgs

    async def execute(
        self, context: ToolContext, args: AnalyzeTimeseriesArgs
    ) -> ToolResult:
        try:
            frame = await self.sql_runner.run_sql(RunSqlToolArgs(sql=args.sql), context)
        except Exception as e:
            return _failure(f"The query could not be run: {e}")

        for label, column in (
            ("date_column", args.date_column),
            ("value_column", args.value_column),
        ):
            if column not in frame.columns:
                available = ", ".join(map(str, frame.columns)) or "none"
                return _failure(
                    f"{label} {column!r} is not in the result. Columns "
                    f"returned: {available}."
                )

        series = self._clean(frame, args.date_column, args.value_column)
        if series is None:
            return _failure(
                f"{args.value_column!r} could not be read as numbers, or "
                f"{args.date_column!r} as dates. Cast them in the query."
            )
        if series.empty:
            return _ok(
                "The query returned no rows, so there is no series to analyse.",
                "Empty series",
            )

        sections = [
            self._summary(series, args.value_column),
            self._trend(series),
            self._moves(series),
        ]
        outliers = self._outliers(series)
        if outliers:
            sections.append(outliers)
        seasonality = self._seasonality(series)
        if seasonality:
            sections.append(seasonality)

        return _ok(
            "\n\n".join(s for s in sections if s),
            f"Analysed {len(series)} periods of {args.value_column}",
            metadata={"periods": len(series)},
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _clean(
        frame: pd.DataFrame, date_column: str, value_column: str
    ) -> Optional[pd.Series]:
        """A numeric series indexed by date, sorted, with unusable rows dropped."""
        if frame.empty:
            # Checked before the all-NaN test below, which is vacuously true on
            # an empty frame and would report "no rows" as a type error.
            return pd.Series(dtype="float64", index=pd.DatetimeIndex([]))

        try:
            dates = pd.to_datetime(frame[date_column], errors="coerce")
            values = pd.to_numeric(frame[value_column], errors="coerce")
        except Exception:
            return None
        if dates.isna().all() or values.isna().all():
            return None

        series = pd.Series(values.values, index=dates)
        return series[series.index.notna() & series.notna()].sort_index()

    @staticmethod
    def _summary(series: pd.Series, value_column: str) -> str:
        return (
            f"{value_column} over {len(series)} periods, "
            f"{series.index[0].date()} to {series.index[-1].date()}:\n"
            f"  Total: {series.sum():,.2f}\n"
            f"  Mean: {series.mean():,.2f}   Median: {series.median():,.2f}\n"
            f"  Lowest: {series.min():,.2f}   Highest: {series.max():,.2f}"
        )

    @staticmethod
    def _trend(series: pd.Series) -> str:
        if len(series) < _MIN_POINTS_FOR_TREND:
            return (
                f"Only {len(series)} periods, which is too few to call a trend. "
                "Report the individual values rather than a direction."
            )

        # Correlation with position, not a fitted slope: the question here is
        # how *consistently* the series moves in one direction, which a slope
        # cannot answer -- a steep slope and a noisy one look the same. Position
        # rather than timestamp assumes evenly spaced periods, which is what a
        # GROUP BY over a date part produces.
        positions = pd.Series(range(len(series)), dtype="float64")
        consistency = positions.corr(pd.Series(series.values, dtype="float64"))
        first, last = float(series.iloc[0]), float(series.iloc[-1])

        if first == 0:
            change = "undefined (the series starts at zero)"
        else:
            change = f"{(last - first) / abs(first) * 100:+.1f}%"

        if pd.isna(consistency):
            direction = "flat"
        elif consistency > 0.5:
            direction = "rising consistently"
        elif consistency > 0.1:
            direction = "rising, unevenly"
        elif consistency < -0.5:
            direction = "falling consistently"
        elif consistency < -0.1:
            direction = "falling, unevenly"
        else:
            direction = "without a clear direction"

        return (
            f"Trend: {direction}. First to last: {change} "
            f"({first:,.2f} to {last:,.2f})."
        )

    @staticmethod
    def _moves(series: pd.Series) -> str:
        if len(series) < 2:
            return ""
        deltas = series.diff().dropna()
        if deltas.empty:
            return ""
        ranked = deltas.reindex(deltas.abs().sort_values(ascending=False).index)
        lines = ["Largest period-on-period moves:"]
        for when, delta in ranked.head(_MAX_MOVES).items():
            previous = series.shift(1)[when]
            share = (
                f" ({delta / abs(previous) * 100:+.1f}%)"
                if previous not in (0, None) and not pd.isna(previous)
                else ""
            )
            lines.append(f"  {when.date()}: {delta:+,.2f}{share}")
        return "\n".join(lines)

    @staticmethod
    def _outliers(series: pd.Series) -> str:
        if len(series) < _MIN_POINTS_FOR_TREND:
            return ""
        median = series.median()
        deviation = (series - median).abs().median()
        if deviation == 0:
            return ""
        scores = 0.6745 * (series - median) / deviation
        flagged = series[scores.abs() > _OUTLIER_THRESHOLD]
        if flagged.empty:
            return ""

        lines = [
            f"{len(flagged)} outlier(s), far enough from the rest to distort a "
            "mean. Consider whether the question wants them included:"
        ]
        for when, value in flagged.items():
            lines.append(f"  {when.date()}: {value:,.2f} (median is {median:,.2f})")
        return "\n".join(lines)

    @staticmethod
    def _seasonality(series: pd.Series) -> str:
        if len(series) < _MIN_POINTS_FOR_SEASONALITY:
            return ""
        values = pd.Series(series.values, dtype="float64")
        best_lag, best_correlation = 0, 0.0
        # Up to a quarter of the series, so any period found has at least four
        # cycles behind it. Anything longer is indistinguishable from trend.
        for lag in range(2, len(values) // 4 + 1):
            correlation = values.autocorr(lag=lag)
            if pd.notna(correlation) and correlation > best_correlation:
                best_lag, best_correlation = lag, correlation
        if best_lag == 0 or best_correlation < 0.5:
            return ""
        return (
            f"Possible seasonality: values repeat roughly every {best_lag} "
            f"periods (correlation {best_correlation:.2f}). This is a hint from "
            "autocorrelation, not a finding -- compare like periods before "
            "attributing a change to seasonality."
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
