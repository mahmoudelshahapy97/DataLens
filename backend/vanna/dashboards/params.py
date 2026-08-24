"""Parameters: the one part of a tile a reader is allowed to change.

A parameter changes the SQL that runs, which puts this module on the injection path.
Everything below follows from that.

**Typed, never interpolated.** A parameter declares a type, and its value is rendered
by a function that only knows how to emit that type: a date becomes ``DATE '2026-08-01'``
and nothing else can come out of it. There is deliberately no free-text parameter,
because the safe rendering of arbitrary text into SQL is "don't".

**An enum is an allowlist, not a hint.** Its ``options`` are declared on the dashboard.
A supplied value that is not one of them is refused rather than quoted, so the quoting
is a second line of defence rather than the only one.

**Substitution happens before the policy sees the statement.** The SQL policy validates
statement text; if parameters were applied afterwards they would be applied to something
already approved, and the approval would mean nothing. Placeholders are filled first and
the policy then judges the real statement -- so a tile is checked exactly like a chat
question, which is the guarantee the whole dashboards design rests on.

**A missing declaration is an error, not an empty string.** A statement referencing
``{{ since }}`` with no ``since`` parameter fails verification when the dashboard is
saved. Substituting nothing would produce valid SQL with a different meaning, which is
the failure a dashboard must not have.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: ``{{ name }}``, with any inner spacing. Deliberately narrow: a name is lowercase
#: ASCII, digits and underscores, so a placeholder can never smuggle an expression.
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")

#: A parameter name, matched whole.
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

#: Relative windows a date_range accepts by name. Spelled out rather than parsed from
#: something like "last 7 days", because a parser here is a second grammar to get wrong
#: and these are the windows anybody actually asks for.
WINDOWS: Dict[str, int] = {
    "last_7_days": 7,
    "last_14_days": 14,
    "last_30_days": 30,
    "last_90_days": 90,
    "last_180_days": 180,
    "last_365_days": 365,
}

#: Anchored windows, which are not a fixed number of days.
ANCHORED = ("month_to_date", "quarter_to_date", "year_to_date")


class ParameterError(ValueError):
    """A supplied value that will not be rendered.

    Carries the parameter name so the caller can say which control was wrong rather
    than reporting that "the report failed".
    """

    def __init__(self, name: str, message: str) -> None:
        super().__init__(f"{name}: {message}")
        self.name = name


class ParameterType(str, Enum):
    DATE = "date"
    DATE_RANGE = "date_range"
    INTEGER = "integer"
    ENUM = "enum"


class Parameter(BaseModel):
    """One control on a report, and the rules its value must satisfy."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParameterType
    label: str = ""
    #: Used when the reader supplies nothing. A report with defaults for every
    #: parameter renders on first open, which is what makes it a report rather than
    #: a form.
    default: Optional[Any] = None
    #: `enum` only, and required for it: this is the allowlist.
    options: List[str] = Field(default_factory=list)
    #: `integer` only. Both inclusive.
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _usable_name(cls, value: str) -> str:
        if not _NAME.match(value or ""):
            raise ValueError(
                f"{value!r} is not a usable parameter name: lowercase letters, digits "
                "and underscores, starting with a letter or underscore"
            )
        return value

    def placeholders(self) -> List[str]:
        """The placeholder names this parameter fills.

        A ``date_range`` fills two -- ``{{ period_start }}`` and ``{{ period_end }}``
        for a parameter called ``period``. Two named placeholders rather than one that
        expands to a pair, because ``BETWEEN {{ period }}`` reads like it works and
        does not.
        """
        if self.type is ParameterType.DATE_RANGE:
            return [f"{self.name}_start", f"{self.name}_end"]
        return [self.name]

    def prompt(self) -> str:
        return self.label or self.name.replace("_", " ")


# ----------------------------------------------------------------------
# Rendering a value as a SQL literal
# ----------------------------------------------------------------------


def _as_date(name: str, value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ParameterError(name, f"{text!r} is not a date in YYYY-MM-DD form")


def _date_literal(value: date) -> str:
    # ISO format only ever produces digits and hyphens, so there is nothing here a
    # quote could escape from.
    return f"DATE '{value.isoformat()}'"


def _integer_literal(name: str, value: Any, parameter: Parameter) -> str:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ParameterError(name, f"{value!r} is not a whole number")
    if parameter.minimum is not None and number < parameter.minimum:
        raise ParameterError(name, f"must be {parameter.minimum} or more")
    if parameter.maximum is not None and number > parameter.maximum:
        raise ParameterError(name, f"must be {parameter.maximum} or less")
    return str(number)


def _enum_literal(name: str, value: Any, parameter: Parameter) -> str:
    text = str(value)
    if text not in parameter.options:
        # The allowlist, and the whole reason an enum is safe. The message names the
        # options because the caller is a person choosing from a dropdown.
        raise ParameterError(
            name,
            f"{text!r} is not one of the allowed values ({', '.join(parameter.options)})",
        )
    # Escaped anyway. The value came from a list this dashboard declares, but a
    # declaration is data too, and defence that depends on data being trustworthy is
    # not defence.
    return "'" + text.replace("'", "''") + "'"


def _window(name: str, value: Any, *, as_of: date) -> tuple:
    """Resolve a date_range value to a pair of dates."""
    text = str(value).strip().lower()

    if text in WINDOWS:
        # Inclusive of today: "last 7 days" ending yesterday surprises everybody who
        # looks at it on a Monday morning.
        return as_of - timedelta(days=WINDOWS[text] - 1), as_of
    if text == "month_to_date":
        return as_of.replace(day=1), as_of
    if text == "quarter_to_date":
        first_month = 1 + 3 * ((as_of.month - 1) // 3)
        return as_of.replace(month=first_month, day=1), as_of
    if text == "year_to_date":
        return as_of.replace(month=1, day=1), as_of

    if ".." in text:
        start_text, _, end_text = text.partition("..")
        start, end = _as_date(name, start_text), _as_date(name, end_text)
        if start > end:
            raise ParameterError(name, "the range starts after it ends")
        return start, end

    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = _as_date(name, value[0]), _as_date(name, value[1])
        if start > end:
            raise ParameterError(name, "the range starts after it ends")
        return start, end

    raise ParameterError(
        name,
        f"{text!r} is not a range. Use YYYY-MM-DD..YYYY-MM-DD or one of "
        + ", ".join([*WINDOWS, *ANCHORED]),
    )


def render_value(parameter: Parameter, value: Any, *, as_of: Optional[date] = None) -> Dict[str, str]:
    """One parameter and its value, as the SQL literals it fills.

    Returns placeholder name -> literal, because a date_range fills two.
    """
    today = as_of or datetime.now(timezone.utc).date()
    name = parameter.name

    if parameter.type is ParameterType.DATE:
        return {name: _date_literal(_as_date(name, value))}
    if parameter.type is ParameterType.INTEGER:
        return {name: _integer_literal(name, value, parameter)}
    if parameter.type is ParameterType.ENUM:
        return {name: _enum_literal(name, value, parameter)}
    if parameter.type is ParameterType.DATE_RANGE:
        start, end = _window(name, value, as_of=today)
        return {f"{name}_start": _date_literal(start), f"{name}_end": _date_literal(end)}

    raise ParameterError(name, f"unsupported parameter type {parameter.type!r}")


# ----------------------------------------------------------------------
# Resolving a whole set, and filling a statement
# ----------------------------------------------------------------------


def resolve(
    parameters: Iterable[Parameter],
    supplied: Optional[Dict[str, Any]] = None,
    *,
    as_of: Optional[date] = None,
) -> Dict[str, str]:
    """Every declared parameter, rendered. Raises on anything that will not render.

    An unknown supplied key is an error rather than something to ignore. A typo in a
    parameter name would otherwise leave the report rendering its default while the
    reader believes they changed it -- a wrong answer that looks like a right one.
    """
    declared = {parameter.name: parameter for parameter in parameters}
    given = dict(supplied or {})

    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise ParameterError(
            unknown[0],
            "no such parameter on this report"
            + (f" (also: {', '.join(unknown[1:])})" if len(unknown) > 1 else ""),
        )

    rendered: Dict[str, str] = {}
    for name, parameter in declared.items():
        if name in given and given[name] not in (None, ""):
            value = given[name]
        elif parameter.default not in (None, ""):
            value = parameter.default
        else:
            raise ParameterError(name, "a value is required and there is no default")
        rendered.update(render_value(parameter, value, as_of=as_of))
    return rendered


def placeholders_in(sql: str) -> Set[str]:
    """Every ``{{ name }}`` in a statement."""
    return set(PLACEHOLDER.findall(sql or ""))


def substitute(sql: str, rendered: Dict[str, str]) -> str:
    """Fill a statement's placeholders.

    Raises on a placeholder with no value. ``verify_dashboard`` catches that when the
    document is saved, so reaching it here means a document stored by an older build --
    which is exactly the case the render-time check exists for.
    """
    missing = sorted(placeholders_in(sql) - set(rendered))
    if missing:
        raise ParameterError(
            missing[0],
            "this statement uses the placeholder but the report declares no such "
            "parameter",
        )

    def fill(match: "re.Match[str]") -> str:
        return rendered[match.group(1)]

    return PLACEHOLDER.sub(fill, sql)


def declared_placeholders(parameters: Iterable[Parameter]) -> Set[str]:
    """Every placeholder name the declarations can fill."""
    names: Set[str] = set()
    for parameter in parameters:
        names.update(parameter.placeholders())
    return names
