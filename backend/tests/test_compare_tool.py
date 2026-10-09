"""`compare_periods` -- two windows, aligned without a self-join.

The case this tool exists for is the one an inner join deletes: a category that
sold in the baseline window and nothing in the comparison window. That row is
usually the answer to "what changed", so `test_group_absent_from_comparison_is_kept`
is the test that matters most here; the rest guard the arithmetic around it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.compare import ComparePeriodsArgs, ComparePeriodsTool


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id="acme"),
        conversation_id="c1",
        request_id="r1",
        tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )


class _Runner:
    """Returns a frame per call, in order, and records the SQL it received."""

    def __init__(self, *frames):
        self.frames = list(frames)
        self.seen: list[str] = []

    async def run_sql(self, args, context):
        self.seen.append(args.sql)
        nxt = self.frames.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


_TEMPLATE = (
    "SELECT category, SUM(total) AS revenue FROM sales "
    "WHERE sold_on >= '{start}' AND sold_on < '{end}' GROUP BY category"
)


async def _run(runner, **overrides):
    args = ComparePeriodsArgs(
        **{
            "sql": _TEMPLATE,
            "value_column": "revenue",
            "label_column": "category",
            "baseline_start": "2024-04-01",
            "baseline_end": "2024-07-01",
            "comparison_start": "2024-07-01",
            "comparison_end": "2024-10-01",
            **overrides,
        }
    )
    return await ComparePeriodsTool(runner).execute(_context(), args)


def _frame(pairs: dict) -> pd.DataFrame:
    return pd.DataFrame(
        {"category": list(pairs), "revenue": [float(v) for v in pairs.values()]}
    )


class TestComparePeriodsTool:
    def test_declares_sql_argument_field(self):
        assert ComparePeriodsTool(_Runner()).sql_argument_fields == ("sql",)

    async def test_substitutes_each_window_into_the_same_template(self):
        runner = _Runner(_frame({"a": 10}), _frame({"a": 20}))
        await _run(runner)

        assert len(runner.seen) == 2
        assert "sold_on >= '2024-04-01' AND sold_on < '2024-07-01'" in runner.seen[0]
        assert "sold_on >= '2024-07-01' AND sold_on < '2024-10-01'" in runner.seen[1]
        # The two runs must differ only by window.
        assert runner.seen[0].replace("2024-04-01", "X").replace(
            "2024-07-01", "Y"
        ) == runner.seen[1].replace("2024-07-01", "X").replace("2024-10-01", "Y")

    async def test_rejects_a_template_without_placeholders(self):
        result = await _run(_Runner(), sql="SELECT 1 AS revenue")

        assert not result.success
        assert "{start} and {end}" in result.error

    async def test_computes_change_and_percentage(self):
        result = await _run(_Runner(_frame({"books": 100}), _frame({"books": 150})))

        assert result.success
        assert "+50.00" in result.result_for_llm
        assert "+50.0%" in result.result_for_llm

    async def test_group_absent_from_comparison_is_kept(self):
        """The row an inner join would delete, and the point of the tool."""
        result = await _run(
            _Runner(_frame({"books": 100, "vinyl": 40}), _frame({"books": 120}))
        )

        text = result.result_for_llm
        assert "vinyl" in text
        assert result.metadata["disappeared"] == ["vinyl"]
        assert "gone (was 40.00)" in text

    async def test_group_new_in_comparison_is_kept(self):
        result = await _run(
            _Runner(_frame({"books": 100}), _frame({"books": 120, "tapes": 25}))
        )

        assert result.metadata["appeared"] == ["tapes"]
        assert "new (25.00)" in result.result_for_llm

    async def test_zero_baseline_reports_no_percentage(self):
        """+100% against a zero baseline would be a fabrication."""
        result = await _run(_Runner(_frame({"books": 0}), _frame({"books": 80})))

        assert "no percentage: baseline is zero" in result.result_for_llm

    async def test_orders_groups_by_size_of_change(self):
        result = await _run(
            _Runner(
                _frame({"small": 100, "big": 100}),
                _frame({"small": 105, "big": 400}),
            )
        )

        text = result.result_for_llm
        assert text.index("big") < text.index("small")

    async def test_totals_mode_without_a_label_column(self):
        result = await _run(
            _Runner(
                pd.DataFrame({"revenue": [500.0]}), pd.DataFrame({"revenue": [650.0]})
            ),
            label_column=None,
        )

        assert result.success
        assert "Baseline: 500.00" in result.result_for_llm
        assert "+30.0%" in result.result_for_llm

    async def test_missing_value_column_names_what_was_returned(self):
        result = await _run(
            _Runner(_frame({"a": 1}), _frame({"a": 2})), value_column="profit"
        )

        assert not result.success
        assert "'profit' is not in the result" in result.error
        assert "category, revenue" in result.error

    async def test_query_failure_is_reported(self):
        result = await _run(_Runner(RuntimeError("no such column: sold_on")))

        assert not result.success
        assert "no such column" in result.error
