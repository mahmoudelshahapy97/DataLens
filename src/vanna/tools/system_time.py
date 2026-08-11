"""A tool that reports the current time, so queries stop using `NOW()`.

Letting the model write ``WHERE created_at > CURRENT_DATE - 30`` looks
harmless and quietly breaks three things:

* **Reproducibility.** The same question asked twice returns different rows.
  A saved example, a cached result, and a regression test all become
  meaningless, because the query's meaning drifts with the wall clock.
* **Verifiability.** A user who checks yesterday's number against today's run
  sees a discrepancy with no explanation.
* **Correctness across timezones.** The database's clock, the server's clock,
  and the user's clock are frequently three different things, and
  ``CURRENT_DATE`` silently picks the database's.

Resolving the date *before* generation and pinning a literal into the SQL fixes
all three: the query says what it means, and it means the same thing tomorrow.

Pair this with ``denied_functions`` in the SQL policy to make it stick::

    SqlPolicy(denied_functions=frozenset(TIME_FUNCTION_NAMES))
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Type

from pydantic import BaseModel, Field

from vanna.components import (
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Non-deterministic time functions worth denying once this tool is registered.
#: Blocking them without providing an alternative just makes date questions
#: fail, so register the tool first.
TIME_FUNCTION_NAMES = (
    "now",
    "current_date",
    "current_time",
    "current_timestamp",
    "curdate",
    "curtime",
    "getdate",
    "sysdate",
    "localtime",
    "localtimestamp",
    "today",
    "unix_timestamp",
)


class SystemTimeArgs(BaseModel):
    """Arguments for the system_time tool."""

    timezone_name: Optional[str] = Field(
        default=None,
        description=(
            "IANA timezone name (e.g. 'America/New_York', 'Europe/London'). "
            "Defaults to the server's configured timezone."
        ),
    )


class SystemTimeTool(Tool[SystemTimeArgs]):
    """Reports the current date and time as literals for use in SQL.

    Args:
        default_timezone: IANA name used when the caller does not specify one.
            Set this to the organisation's reporting timezone -- "last month"
            means something different in Sydney than in Los Angeles.
        fiscal_year_start_month: Month the fiscal year begins (1-12). When set
            to something other than January, the tool reports the current
            fiscal quarter alongside the calendar one, so "this quarter" does
            not silently mean the wrong thing.
    """

    def __init__(
        self,
        *,
        default_timezone: Optional[str] = None,
        fiscal_year_start_month: int = 1,
    ) -> None:
        self.default_timezone = default_timezone
        self.fiscal_year_start_month = fiscal_year_start_month

    @property
    def name(self) -> str:
        return "system_time"

    @property
    def description(self) -> str:
        return (
            "Get the current date and time. Call this before writing any query "
            "involving dates or relative time ('today', 'last month', 'this "
            "quarter', 'year to date'), then write explicit date literals into "
            "the SQL. Do not use NOW(), CURRENT_DATE, or similar functions -- "
            "they make results change between runs."
        )

    def get_args_schema(self) -> Type[SystemTimeArgs]:
        return SystemTimeArgs

    async def execute(
        self, context: ToolContext, args: SystemTimeArgs
    ) -> ToolResult:
        tz_name = args.timezone_name or self.default_timezone
        now, resolved_tz, tz_error = self._resolve_now(tz_name)

        lines = [
            f"Current timestamp: {now.isoformat()}",
            f"Timezone: {resolved_tz}",
            f"Today: {now.date().isoformat()}",
            f"Yesterday: {(now.date() - timedelta(days=1)).isoformat()}",
        ]

        # Precompute the boundaries the model would otherwise derive by hand --
        # off-by-one errors on month and quarter starts are a common and
        # completely silent source of wrong numbers.
        lines.extend(self._period_boundaries(now))

        if self.fiscal_year_start_month != 1:
            lines.append(self._fiscal_period(now))

        lines.append(
            "\nUse these as explicit literals in your SQL "
            "(e.g. WHERE order_date >= DATE '2026-08-01'). "
            "Do not call NOW(), CURRENT_DATE, or equivalents."
        )
        if tz_error:
            lines.append(f"\nNote: {tz_error}")

        text = "\n".join(lines)
        return ToolResult(
            success=True,
            result_for_llm=text,
            ui_component=UiComponent(
                rich_component=RichTextComponent(content=text, markdown=False),
                simple_component=SimpleTextComponent(text=text),
            ),
            metadata={"timestamp": now.isoformat(), "timezone": resolved_tz},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_now(self, tz_name: Optional[str]):
        """Return ``(now, tz_label, error_or_None)``.

        An unknown timezone falls back to UTC with a note rather than raising:
        a slightly wrong timezone still answers the question, while a hard
        failure answers nothing.
        """
        if not tz_name:
            return datetime.now().astimezone(), "server local", None
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
            return datetime.now(tz), tz_name, None
        except Exception:
            return (
                datetime.now(timezone.utc),
                "UTC",
                f"Timezone {tz_name!r} was not recognised; UTC was used instead.",
            )

    @staticmethod
    def _period_boundaries(now: datetime) -> list:
        today = now.date()
        month_start = today.replace(day=1)

        prev_month_end = month_start - timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)

        quarter_index = (today.month - 1) // 3
        quarter_start = today.replace(month=quarter_index * 3 + 1, day=1)

        year_start = today.replace(month=1, day=1)

        # Monday-based week, matching ISO 8601.
        week_start = today - timedelta(days=today.weekday())

        return [
            f"Start of this week (Monday): {week_start.isoformat()}",
            f"Start of this month: {month_start.isoformat()}",
            f"Last month: {prev_month_start.isoformat()} "
            f"to {prev_month_end.isoformat()} (inclusive)",
            f"Start of this quarter: {quarter_start.isoformat()}",
            f"Start of this year: {year_start.isoformat()}",
        ]

    def _fiscal_period(self, now: datetime) -> str:
        today = now.date()
        start_month = self.fiscal_year_start_month
        # Months elapsed since the fiscal year began, wrapping at December.
        months_in = (today.month - start_month) % 12
        fiscal_quarter = months_in // 3 + 1
        fiscal_year = today.year if today.month >= start_month else today.year - 1
        fq_start_month = ((start_month - 1 + (fiscal_quarter - 1) * 3) % 12) + 1
        fq_year = fiscal_year if fq_start_month >= start_month else fiscal_year + 1
        fq_start = today.replace(year=fq_year, month=fq_start_month, day=1)
        return (
            f"Fiscal year {fiscal_year} (starts month {start_month}); "
            f"currently FQ{fiscal_quarter}, which began {fq_start.isoformat()}"
        )
