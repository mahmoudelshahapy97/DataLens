"""`analyze_timeseries` -- the arithmetic a model otherwise does by eye.

Every case here is a series whose shape is known in advance, so the assertions
are about whether the tool describes it correctly rather than whether it
returns something. The two that matter most are the ones where reading the rows
would mislead: a short series that must *not* be called a trend, and an outlier
that is carrying the mean.

The runner is a stub rather than SQLite because the SQL is the caller's, not
this tool's -- there is no generated query to prove correct, and building a
real database per shape would only test pandas' CSV reader.
"""

from __future__ import annotations

import pandas as pd
import pytest

from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.timeseries import AnalyzeTimeseriesArgs, AnalyzeTimeseriesTool


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id="acme"),
        conversation_id="c1",
        request_id="r1",
        tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )


class _Runner:
    """Returns a fixed frame; records the SQL it was handed."""

    def __init__(self, frame: pd.DataFrame | Exception):
        self.frame = frame
        self.seen: list[str] = []

    async def run_sql(self, args, context):
        self.seen.append(args.sql)
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame


def _monthly(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "month": pd.date_range("2024-01-01", periods=len(values), freq="MS"),
            "revenue": values,
        }
    )


async def _run(frame, **overrides) -> tuple:
    runner = _Runner(frame)
    tool = AnalyzeTimeseriesTool(runner)
    args = AnalyzeTimeseriesArgs(
        **{
            "sql": "SELECT month, revenue FROM sales",
            "date_column": "month",
            "value_column": "revenue",
            **overrides,
        }
    )
    return await tool.execute(_context(), args), runner


class TestAnalyzeTimeseriesTool:
    def test_declares_sql_argument_field(self):
        """The query is model-written, so the safety policy must see it."""
        assert AnalyzeTimeseriesTool(_Runner(None)).sql_argument_fields == ("sql",)

    async def test_detects_consistent_growth(self):
        result, _ = await _run(_monthly([100, 110, 120, 130, 140, 150]))

        assert result.success
        assert "rising consistently" in result.result_for_llm
        assert "+50.0%" in result.result_for_llm

    async def test_detects_consistent_decline(self):
        result, _ = await _run(_monthly([150, 140, 130, 120, 110, 100]))
        assert "falling consistently" in result.result_for_llm

    async def test_refuses_to_call_a_short_series_a_trend(self):
        """Three points have a direction, but it is not a trend."""
        result, _ = await _run(_monthly([100, 50, 200]))

        assert "too few to call a trend" in result.result_for_llm
        assert "rising" not in result.result_for_llm

    async def test_noisy_series_is_not_called_consistent(self):
        result, _ = await _run(_monthly([100, 180, 90, 175, 95, 185, 85, 190]))
        assert "consistently" not in result.result_for_llm

    async def test_flags_an_outlier_carrying_the_mean(self):
        result, _ = await _run(_monthly([100, 102, 98, 101, 99, 5000, 100, 103]))

        text = result.result_for_llm
        assert "outlier" in text
        assert "5,000.00" in text
        # The point of flagging it is the decision it forces.
        assert "distort a mean" in text

    async def test_reports_largest_moves(self):
        result, _ = await _run(_monthly([100, 100, 300, 100, 100, 100]))

        text = result.result_for_llm
        assert "Largest period-on-period moves" in text
        assert "+200.00" in text

    async def test_finds_a_repeating_period(self):
        """Four clean cycles of length three."""
        result, _ = await _run(_monthly([10, 50, 90] * 4))

        text = result.result_for_llm
        assert "Possible seasonality" in text
        assert "every 3 periods" in text
        # Offered as a hint, never as a finding.
        assert "not a finding" in text

    async def test_short_series_gets_no_seasonality_claim(self):
        result, _ = await _run(_monthly([10, 50, 90, 10, 50]))
        assert "seasonality" not in result.result_for_llm

    async def test_missing_column_names_what_was_returned(self):
        result, _ = await _run(_monthly([1, 2, 3, 4]), value_column="profit")

        assert not result.success
        assert "'profit' is not in the result" in result.error
        assert "month, revenue" in result.error

    async def test_non_numeric_values_are_reported(self):
        frame = pd.DataFrame(
            {
                "month": pd.date_range("2024-01-01", periods=4, freq="MS"),
                "revenue": ["a", "b", "c", "d"],
            }
        )
        result, _ = await _run(frame)

        assert not result.success
        assert "could not be read as numbers" in result.error

    async def test_empty_result_is_not_an_error(self):
        result, _ = await _run(_monthly([]))

        assert result.success
        assert "no rows" in result.result_for_llm

    async def test_query_failure_is_reported(self):
        result, _ = await _run(RuntimeError("syntax error at or near FROM"))

        assert not result.success
        assert "syntax error" in result.error

    async def test_rows_with_unparseable_dates_are_dropped_not_fatal(self):
        frame = pd.DataFrame(
            {
                "month": ["2024-01-01", "not a date", "2024-03-01", "2024-04-01"],
                "revenue": [10, 20, 30, 40],
            }
        )
        result, _ = await _run(frame)

        assert result.success
        assert result.metadata["periods"] == 3
