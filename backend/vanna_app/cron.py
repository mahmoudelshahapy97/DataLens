"""When a five-field crontab expression next fires.

A dependency was the obvious answer and it is the wrong one here. APScheduler --
the usual choice -- keeps its schedule in process memory, so with the four uvicorn
workers this deployment runs, four schedulers each fire the same job and four
copies of the same report go out. Making it safe means giving it a shared
jobstore, which is a second source of truth for something the database is already
holding: `report_schedules.next_run_at`.

So the schedule lives in one column, one worker advances it under an advisory
lock, and this module answers the only question that needs answering -- given a
crontab and an instant, when is the next one.

**Scope, stated plainly.** The five standard fields, `*`, `*/n`, `a-b`, `a,b,c`,
and `a-b/n`. Not `@daily`, not `L`, not `W`, not `#`, not seconds, not years. An
expression using anything else is *refused at write time* rather than silently
reinterpreted: a schedule that quietly means something other than what its author
typed is worse than one that would not save.

**Day-of-month and day-of-week are OR, not AND** when both are restricted. That is
cron's actual rule and it surprises everybody, so it is spelled out in `_matches`
rather than left to be rediscovered.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set, Tuple

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - the runtime is 3.12
    ZoneInfo = None  # type: ignore[assignment]


class CronError(ValueError):
    """A crontab expression this module will not schedule.

    Carries the offending field so an admin screen can point at the box that is
    wrong rather than reporting that "the schedule is invalid".
    """

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


#: (name, minimum, maximum). Day-of-week is 0-6 with 0 = Sunday; 7 is accepted as
#: Sunday too, because half the world's crontabs are written that way.
_FIELDS: Tuple[Tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)

_PART = re.compile(r"^(?:(\*)|(\d+)(?:-(\d+))?)(?:/(\d+))?$")

#: A run more than this far out is not a schedule anybody wrote on purpose -- it is
#: an expression like "31 February", which matches nothing. Bounding the search
#: turns an infinite loop into an error message.
_HORIZON_DAYS = 366 * 5


def _expand(spec: str, name: str, low: int, high: int) -> Set[int]:
    """One field to the set of values it matches."""
    values: Set[int] = set()

    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{name}: empty value in {spec!r}", field=name)

        match = _PART.match(part)
        if not match:
            raise CronError(
                f"{name}: {part!r} is not a range this scheduler understands. "
                "Use *, a number, a-b, or either with /n.",
                field=name,
            )

        star, start_text, end_text, step_text = match.groups()
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"{name}: step must be 1 or more in {part!r}", field=name)

        if star:
            start, end = low, high
        else:
            start = int(start_text)
            # `5/10` means "from 5 to the top of the range, every 10" -- not
            # "5 only". A bare `5` with no step means exactly 5.
            end = int(end_text) if end_text else (high if step_text else start)

        if start < low or end > high or start > end:
            raise CronError(
                f"{name}: {part!r} is outside {low}-{high}", field=name
            )

        values.update(range(start, end + 1, step))

    return values


class CronSchedule:
    """A parsed crontab expression."""

    __slots__ = ("expression", "minutes", "hours", "days", "months", "weekdays",
                 "_day_restricted", "_weekday_restricted")

    def __init__(self, expression: str) -> None:
        parts = expression.split()
        if len(parts) != 5:
            raise CronError(
                f"expected 5 fields (minute hour day-of-month month day-of-week), "
                f"got {len(parts)}: {expression!r}"
            )

        self.expression = " ".join(parts)
        sets = [
            _expand(part, name, low, high)
            for part, (name, low, high) in zip(parts, _FIELDS)
        ]
        self.minutes, self.hours, self.days, self.months, weekdays = sets

        # 7 and 0 are both Sunday. Normalising here rather than at every comparison.
        self.weekdays = {0 if value == 7 else value for value in weekdays}

        # Cron's oddest rule: when *both* day-of-month and day-of-week name
        # specific values, a day matching *either* fires. When only one is
        # restricted, only that one is consulted. Recording which are restricted
        # at parse time keeps `_matches` readable.
        self._day_restricted = parts[2] != "*"
        self._weekday_restricted = parts[4] != "*"

    def _matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        if moment.hour not in self.hours or moment.minute not in self.minutes:
            return False

        day_ok = moment.day in self.days
        # Python: Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
        weekday_ok = ((moment.weekday() + 1) % 7) in self.weekdays

        if self._day_restricted and self._weekday_restricted:
            return day_ok or weekday_ok
        if self._day_restricted:
            return day_ok
        if self._weekday_restricted:
            return weekday_ok
        return True

    def next_after(self, after: datetime, *, tz: str = "UTC") -> datetime:
        """The first firing strictly after ``after``.

        ``after`` is in UTC and the answer is in UTC; ``tz`` is the zone the
        *expression* is written in. "09:00 daily" is a wall-clock statement, so
        matching has to happen in the operator's zone -- otherwise the report
        moves by an hour twice a year, which is exactly the kind of drift nobody
        attributes to a timezone.
        """
        zone = _zone(tz)

        # Cron has minute resolution. Truncate, then step: starting from a moment
        # with seconds on it would match the current minute again.
        cursor = after.astimezone(zone).replace(second=0, microsecond=0)
        cursor += timedelta(minutes=1)

        limit = cursor + timedelta(days=_HORIZON_DAYS)
        while cursor < limit:
            if self._matches(cursor):
                # A wall-clock time that does not exist (the spring-forward gap)
                # or exists twice (the autumn overlap) is resolved by astimezone,
                # which picks the first valid instant. A report that runs once at
                # a defensible moment beats one that runs twice or not at all.
                return cursor.astimezone(timezone.utc)

            # Skip whole days when the date cannot match. A `0 3 1 1 *` schedule
            # otherwise costs half a million minute-steps to find next January.
            if not self._date_could_match(cursor):
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            else:
                cursor += timedelta(minutes=1)

        raise CronError(
            f"{self.expression!r} has no firing within {_HORIZON_DAYS // 366} years. "
            "It probably names a date that does not occur, such as 31 February."
        )

    def _date_could_match(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        day_ok = moment.day in self.days
        weekday_ok = ((moment.weekday() + 1) % 7) in self.weekdays
        if self._day_restricted and self._weekday_restricted:
            return day_ok or weekday_ok
        if self._day_restricted:
            return day_ok
        if self._weekday_restricted:
            return weekday_ok
        return True

    def describe(self) -> str:
        """A short human reading, for a schedule list.

        Only the shapes people actually type get prose; anything else falls back
        to the expression itself. A wrong plain-English description is worse than
        none -- somebody will believe it instead of the crontab.
        """
        parts = self.expression.split()
        minute, hour, dom, month, dow = parts

        if parts == ["*", "*", "*", "*", "*"]:
            return "Every minute"
        if minute.startswith("*/") and (hour, dom, month, dow) == ("*", "*", "*", "*"):
            return f"Every {minute[2:]} minutes"
        if minute.isdigit() and hour.startswith("*/") and (dom, month, dow) == ("*", "*", "*"):
            return f"Every {hour[2:]} hours, at {int(minute):02d} past"
        if minute.isdigit() and hour == "*" and (dom, month, dow) == ("*", "*", "*"):
            return f"Hourly, at {int(minute):02d} past"
        if minute.isdigit() and hour.isdigit():
            clock = f"{int(hour):02d}:{int(minute):02d}"
            if (dom, month, dow) == ("*", "*", "*"):
                return f"Daily at {clock}"
            if dom == "*" and month == "*" and dow.isdigit():
                names = ("Sunday", "Monday", "Tuesday", "Wednesday",
                         "Thursday", "Friday", "Saturday")
                return f"Every {names[int(dow) % 7]} at {clock}"
            if dom.isdigit() and month == "*" and dow == "*":
                return f"Monthly on day {int(dom)} at {clock}"
        return self.expression


def _zone(name: str):
    """The named zone, or UTC.

    An unknown name falls back rather than raising: the schedule already exists by
    the time this runs, and refusing to compute its next run would take the whole
    scheduler down over one bad row. `validate` is where a bad zone is rejected,
    at write time, where the author can fix it.
    """
    if not name or name.upper() == "UTC" or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - any zoneinfo failure means "use UTC"
        return timezone.utc


def parse(expression: str) -> CronSchedule:
    return CronSchedule(expression)


def next_occurrence(expression: str, after: datetime, *, tz: str = "UTC") -> datetime:
    """Convenience wrapper: parse and step in one call."""
    return CronSchedule(expression).next_after(after, tz=tz)


def validate(expression: str, tz: str = "UTC") -> None:
    """Raise ``CronError`` if this schedule cannot be saved.

    Called on write. It parses *and* computes one firing, because an expression
    can be syntactically perfect and still match nothing -- `0 0 30 2 *` is the
    canonical example, and finding that out at write time is the difference
    between an error message and a report that never arrives.
    """
    schedule = CronSchedule(expression)
    if ZoneInfo is not None and tz and tz.upper() != "UTC":
        try:
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001
            raise CronError(f"unknown timezone {tz!r}", field="timezone") from exc
    schedule.next_after(datetime.now(timezone.utc), tz=tz)


def known_timezones() -> List[str]:
    """A short list for a picker, not the full IANA database.

    Six hundred entries in a dropdown is not a choice, it is a search problem. The
    API accepts any IANA name; this is only what the form offers.
    """
    return [
        "UTC",
        "Africa/Cairo",
        "America/Chicago",
        "America/Los_Angeles",
        "America/New_York",
        "America/Sao_Paulo",
        "Asia/Dubai",
        "Asia/Kolkata",
        "Asia/Riyadh",
        "Asia/Shanghai",
        "Asia/Singapore",
        "Asia/Tokyo",
        "Australia/Sydney",
        "Europe/Berlin",
        "Europe/London",
        "Europe/Madrid",
        "Europe/Paris",
    ]


#: Offered by the schedule editor. Every one is expressible in the grammar above,
#: which is the point of keeping the list here rather than in the frontend: a
#: preset the backend would refuse must not be presentable.
PRESETS: Iterable[Tuple[str, str]] = (
    ("0 * * * *", "Hourly"),
    ("0 8 * * *", "Daily at 08:00"),
    ("0 8 * * 1", "Weekly on Monday at 08:00"),
    ("0 8 1 * *", "Monthly on the 1st at 08:00"),
    ("0 8 * * 1-5", "Weekdays at 08:00"),
)


def first_run(expression: str, tz: str = "UTC", *, now: Optional[datetime] = None) -> datetime:
    """When a schedule saved *now* would first fire."""
    return next_occurrence(expression, now or datetime.now(timezone.utc), tz=tz)
