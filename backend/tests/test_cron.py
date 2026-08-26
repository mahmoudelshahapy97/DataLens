"""Crontab arithmetic.

A unit test in the strict sense -- no database, no application, no clock. Every
case below fixes ``now`` at a known Wednesday (2026-08-26 10:30 UTC) so the
weekday cases assert a real date rather than "seven days from whenever this ran".

The refusal cases matter as much as the arithmetic. This scheduler understands a
deliberately small grammar, and the alternative to refusing `@daily` or `L` is
*silently reinterpreting* them -- a schedule that means something other than what
its author typed, discovered when a report does not arrive.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from vanna_app import cron
from vanna_app.cron import CronError

UTC = timezone.utc

#: A Wednesday. The weekday cases depend on it, so it is named rather than inlined.
NOW = datetime(2026, 8, 26, 10, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("0 * * * *", datetime(2026, 8, 26, 11, 0, tzinfo=UTC)),
        ("*/5 * * * *", datetime(2026, 8, 26, 10, 35, tzinfo=UTC)),
        ("0 8 * * *", datetime(2026, 8, 27, 8, 0, tzinfo=UTC)),
        # Strictly after: the current minute must not match itself, or a
        # scheduler that ticks twice in one minute fires twice.
        ("30 10 * * *", datetime(2026, 8, 27, 10, 30, tzinfo=UTC)),
        # 2026-08-26 is a Wednesday, so the next Monday is the 31st.
        ("0 8 * * 1", datetime(2026, 8, 31, 8, 0, tzinfo=UTC)),
        ("0 8 1 * *", datetime(2026, 9, 1, 8, 0, tzinfo=UTC)),
        ("0 8 * * 1-5", datetime(2026, 8, 27, 8, 0, tzinfo=UTC)),
        ("0 9,17 * * *", datetime(2026, 8, 26, 17, 0, tzinfo=UTC)),
        # 7 and 0 are both Sunday; half the world's crontabs use 7.
        ("0 8 * * 7", datetime(2026, 8, 30, 8, 0, tzinfo=UTC)),
        ("0 0-23/6 * * *", datetime(2026, 8, 26, 12, 0, tzinfo=UTC)),
    ],
)
def test_next_occurrence(expression: str, expected: datetime) -> None:
    assert cron.next_occurrence(expression, NOW) == expected


def test_day_of_month_and_weekday_are_or_not_and() -> None:
    """Cron's oddest rule, asserted because it surprises everybody.

    When *both* day-of-month and day-of-week name specific values, a day matching
    **either** fires. ``0 8 1 * 1`` is "the 1st, or any Monday" -- not "the 1st,
    if it is a Monday". Reading it as AND would give 2027-02-01; the next Monday
    is five days away.
    """
    assert cron.next_occurrence("0 8 1 * 1", NOW) == datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


def test_expression_is_wall_clock_in_its_own_timezone() -> None:
    """"08:00 daily" is a statement about a wall clock, not about UTC.

    Cairo is UTC+3 in August, so the run lands at 05:00 UTC. Storing an offset
    instead of a zone would make this correct today and an hour wrong after the
    next transition -- drift nobody attributes to a timezone.
    """
    assert cron.next_occurrence("0 8 * * *", NOW, tz="Africa/Cairo") == datetime(
        2026, 8, 27, 5, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "expression",
    [
        "0 8 * *",       # four fields
        "0 8 * * * *",   # six
        "@daily",        # nickname
        "60 8 * * *",    # minute out of range
        "0 24 * * *",    # hour out of range
        "0 8 * 13 *",    # month out of range
        "0 8 L * *",     # last-day modifier
        "0 8 * * 5#2",   # nth-weekday modifier
        "0 8 * * MON",   # names
        "0 8 5-1 * *",   # inverted range
        "*/0 * * * *",   # zero step
    ],
)
def test_refuses_what_it_does_not_implement(expression: str) -> None:
    with pytest.raises(CronError):
        cron.parse(expression)


def test_refuses_a_date_that_never_occurs() -> None:
    """Syntactically perfect, matches nothing.

    ``0 0 30 2 *`` is the canonical trap. Catching it at write time is the
    difference between an error message and a report that silently never arrives.
    """
    with pytest.raises(CronError):
        cron.validate("0 0 30 2 *")


def test_refuses_an_unknown_timezone() -> None:
    with pytest.raises(CronError):
        cron.validate("0 8 * * *", "Mars/Olympus")


@pytest.mark.parametrize(
    "expression, description",
    [
        ("*/5 * * * *", "Every 5 minutes"),
        ("0 * * * *", "Hourly, at 00 past"),
        ("0 8 * * *", "Daily at 08:00"),
        ("0 8 * * 1", "Every Monday at 08:00"),
        ("0 8 1 * *", "Monthly on day 1 at 08:00"),
        # No prose for this shape, so it falls back to the expression rather than
        # inventing a description somebody would believe instead of the crontab.
        ("0 8 * * 1-5", "0 8 * * 1-5"),
    ],
)
def test_describe(expression: str, description: str) -> None:
    assert cron.parse(expression).describe() == description


def test_every_offered_preset_is_accepted() -> None:
    """A preset the backend would refuse must not be presentable.

    The list lives beside the parser for this reason; the schedule editor reads it
    rather than hard-coding its own.
    """
    for expression, _label in cron.PRESETS:
        cron.validate(expression)


def test_parse_is_stable_across_whitespace() -> None:
    assert cron.parse("0   8  *  *  *").expression == "0 8 * * *"
