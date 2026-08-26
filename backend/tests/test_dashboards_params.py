"""Report parameters: what renders, what is refused, and why.

A parameter changes the SQL that runs, so most of this file is about the values that
must *not* render. The design claim being tested is narrow and worth stating: a value
is rendered by a function that only knows how to emit one type, so there is no path
from a supplied string to arbitrary SQL. These tests are what makes that a claim rather
than an intention.

No database and no browser. Substitution is pure, which is the reason it was built as a
separate module rather than inline in the renderer.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from vanna.dashboards import (
    Dashboard,
    Parameter,
    ParameterError,
    has_errors,
    verify_dashboard,
)
from vanna.dashboards.models import Tile
from vanna.dashboards.params import (
    declared_placeholders,
    placeholders_in,
    render_value,
    resolve,
    substitute,
)

#: A fixed "today" so the relative windows are assertable. Real callers use the
#: current date; passing it in is what makes a window testable at all.
AS_OF = date(2026, 8, 23)


def _table(sql: str) -> Tile:
    return Tile(kind="table", query={"source": "sql", "sql": sql})


# ----------------------------------------------------------------------
# Rendering a value
# ----------------------------------------------------------------------


class TestRendering:
    def test_a_date_becomes_a_typed_literal(self):
        parameter = Parameter(name="since", type="date")
        assert render_value(parameter, "2026-01-31") == {"since": "DATE '2026-01-31'"}

    def test_a_date_range_fills_two_placeholders(self):
        """`{{ period }}` alone would read like it works and would not."""
        parameter = Parameter(name="period", type="date_range")
        assert render_value(parameter, "2026-01-01..2026-03-31", as_of=AS_OF) == {
            "period_start": "DATE '2026-01-01'",
            "period_end": "DATE '2026-03-31'",
        }

    @pytest.mark.parametrize(
        "window,start",
        [
            ("last_7_days", "2026-08-17"),   # inclusive of today, so 7 days spans to the 17th
            ("last_30_days", "2026-07-25"),
            ("month_to_date", "2026-08-01"),
            ("quarter_to_date", "2026-07-01"),
            ("year_to_date", "2026-01-01"),
        ],
    )
    def test_the_named_windows(self, window: str, start: str):
        parameter = Parameter(name="period", type="date_range")
        rendered = render_value(parameter, window, as_of=AS_OF)
        assert rendered["period_start"] == f"DATE '{start}'"
        assert rendered["period_end"] == "DATE '2026-08-23'"

    def test_last_7_days_includes_today(self):
        """Off-by-one here is the kind of thing nobody notices until a Monday.

        A window ending yesterday makes every "last 7 days" figure disagree with the
        same figure computed anywhere else.
        """
        parameter = Parameter(name="period", type="date_range")
        rendered = render_value(parameter, "last_7_days", as_of=AS_OF)
        assert rendered["period_end"] == "DATE '2026-08-23'"
        assert rendered["period_start"] == "DATE '2026-08-17'"

    def test_an_integer_is_bounded(self):
        parameter = Parameter(name="top", type="integer", minimum=1, maximum=100)
        assert render_value(parameter, "25") == {"top": "25"}
        with pytest.raises(ParameterError):
            render_value(parameter, "0")
        with pytest.raises(ParameterError):
            render_value(parameter, "1000")

    def test_an_enum_only_accepts_a_declared_option(self):
        parameter = Parameter(name="country", type="enum", options=["USA", "Canada"])
        assert render_value(parameter, "USA") == {"country": "'USA'"}
        with pytest.raises(ParameterError) as caught:
            render_value(parameter, "France")
        assert "not one of the allowed values" in str(caught.value)


# ----------------------------------------------------------------------
# The values that must not render
# ----------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize(
        "hostile",
        [
            "2026-01-01'; DROP TABLE invoice; --",
            "2026-01-01 OR 1=1",
            "'; DELETE FROM chinook.invoice WHERE 1=1; --",
            "2026-01-01) UNION SELECT password FROM users --",
            "now()",
            "CURRENT_DATE",
        ],
    )
    def test_a_date_refuses_anything_that_is_not_a_date(self, hostile: str):
        """The type is the control.

        `_as_date` parses with `date.fromisoformat` and emits only what that returns,
        so there is no string from the caller that reaches the statement.
        """
        with pytest.raises(ParameterError):
            render_value(Parameter(name="since", type="date"), hostile)

    @pytest.mark.parametrize(
        "hostile",
        ["1; DROP TABLE invoice", "1 OR 1=1", "0x10", "1e9", ""],
    )
    def test_an_integer_refuses_anything_that_is_not_a_number(self, hostile: str):
        with pytest.raises(ParameterError):
            render_value(Parameter(name="top", type="integer"), hostile)

    def test_an_enum_refuses_a_value_outside_its_options_even_when_quoted(self):
        parameter = Parameter(name="country", type="enum", options=["USA"])
        with pytest.raises(ParameterError):
            render_value(parameter, "USA' OR '1'='1")

    def test_a_quote_in_a_declared_option_is_still_escaped(self):
        """Defence that assumes the declaration is trustworthy is not defence.

        The options list is data, and data can be wrong. An apostrophe in a legitimate
        value -- O'Reilly, Côte d'Ivoire -- must survive as a value rather than end
        the string literal.
        """
        parameter = Parameter(name="name", type="enum", options=["O'Reilly"])
        assert render_value(parameter, "O'Reilly") == {"name": "'O''Reilly'"}

    def test_an_undeclared_parameter_is_refused_rather_than_ignored(self):
        """A typo must not silently render the default.

        Ignoring the unknown key would leave the reader believing they changed the
        date range when they did not -- a wrong answer wearing a right one's clothes.
        """
        with pytest.raises(ParameterError) as caught:
            resolve([Parameter(name="since", type="date", default="2026-01-01")],
                    {"snice": "2026-05-05"})
        assert "no such parameter" in str(caught.value)

    def test_a_required_parameter_with_no_default_and_no_value_is_refused(self):
        with pytest.raises(ParameterError):
            resolve([Parameter(name="since", type="date")], {})

    def test_a_placeholder_with_no_parameter_is_refused_at_render_time_too(self):
        """Belt and braces: verify catches this at save time, this catches an old document."""
        with pytest.raises(ParameterError) as caught:
            substitute("SELECT 1 WHERE d > {{ since }}", {})
        assert "declares no such parameter" in str(caught.value)


# ----------------------------------------------------------------------
# Substitution
# ----------------------------------------------------------------------


class TestSubstitution:
    def test_it_fills_every_occurrence(self):
        sql = "SELECT 1 WHERE a > {{ since }} AND b > {{since}}"
        assert substitute(sql, {"since": "DATE '2026-01-01'"}) == (
            "SELECT 1 WHERE a > DATE '2026-01-01' AND b > DATE '2026-01-01'"
        )

    def test_it_finds_placeholders_whatever_the_spacing(self):
        assert placeholders_in("{{a}} {{ b }} {{  c  }}") == {"a", "b", "c"}

    def test_it_ignores_things_that_only_look_like_placeholders(self):
        """A name is lowercase ASCII, so nothing else can pose as one."""
        assert placeholders_in("{{ SELECT }} {{ a-b }} { x } {{}}") == set()

    def test_a_date_range_declares_both_halves(self):
        parameter = Parameter(name="period", type="date_range")
        assert declared_placeholders([parameter]) == {"period_start", "period_end"}

    def test_a_whole_set_resolves_together(self):
        rendered = resolve(
            [
                Parameter(name="period", type="date_range", default="last_7_days"),
                Parameter(name="top", type="integer", default=10),
                Parameter(name="country", type="enum", options=["USA"], default="USA"),
            ],
            {"top": 25},
            as_of=AS_OF,
        )
        assert rendered == {
            "period_start": "DATE '2026-08-17'",
            "period_end": "DATE '2026-08-23'",
            "top": "25",
            "country": "'USA'",
        }


# ----------------------------------------------------------------------
# Verification, at save time
# ----------------------------------------------------------------------


class TestVerification:
    def test_a_placeholder_with_no_declaration_is_an_error(self):
        dashboard = Dashboard(
            title="R", tiles=[_table("SELECT 1 WHERE d > {{ since }}")]
        )
        issues = verify_dashboard(dashboard)
        assert has_errors(issues)
        assert any("declares no parameter" in str(i) for i in issues)

    def test_a_declared_and_used_parameter_is_clean(self):
        dashboard = Dashboard(
            title="R",
            parameters=[Parameter(name="since", type="date", default="2026-01-01")],
            tiles=[_table("SELECT 1 WHERE d > {{ since }}")],
        )
        assert not has_errors(verify_dashboard(dashboard))

    def test_an_enum_without_options_is_an_error(self):
        dashboard = Dashboard(
            title="R",
            parameters=[Parameter(name="country", type="enum")],
            tiles=[_table("SELECT {{ country }}")],
        )
        assert has_errors(verify_dashboard(dashboard))

    def test_a_default_outside_its_own_options_is_an_error(self):
        dashboard = Dashboard(
            title="R",
            parameters=[
                Parameter(name="c", type="enum", options=["USA"], default="France")
            ],
            tiles=[_table("SELECT {{ c }}")],
        )
        assert has_errors(verify_dashboard(dashboard))

    def test_a_duplicate_parameter_is_an_error(self):
        dashboard = Dashboard(
            title="R",
            parameters=[
                Parameter(name="since", type="date", default="2026-01-01"),
                Parameter(name="since", type="date", default="2026-02-01"),
            ],
            tiles=[_table("SELECT 1 WHERE d > {{ since }}")],
        )
        assert has_errors(verify_dashboard(dashboard))

    def test_an_unused_parameter_warns_rather_than_fails(self):
        """Declared but unused is a control that appears to do nothing when moved.

        A warning, not an error: a report mid-edit legitimately passes through that
        state, and refusing to save it would be the tool arguing with its author.
        """
        dashboard = Dashboard(
            title="R",
            parameters=[Parameter(name="unused", type="date", default="2026-01-01")],
            tiles=[_table("SELECT 1")],
        )
        issues = verify_dashboard(dashboard)
        assert not has_errors(issues)
        assert any("no tile uses it" in str(i) for i in issues)

    def test_a_parameter_name_must_be_usable(self):
        """Rejected at construction, so an unusable name cannot reach a statement."""
        for bad in ("Since", "1since", "since-date", "since date", ""):
            with pytest.raises(ValidationError):
                Parameter(name=bad, type="date")
