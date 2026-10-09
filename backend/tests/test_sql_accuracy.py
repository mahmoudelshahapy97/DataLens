"""`evals/sql_accuracy.py` -- the pure parts: result comparison and the
reference-SQL sanity check. Running the eval itself needs a live instance."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from evals.sql_accuracy import (
    compare_results,
    has_swallowed_comment,
    normalize_rows,
)


class TestCompareResults:
    def test_identical_rows_in_any_order_are_exact(self):
        gold = normalize_rows([("USA", 523.06), ("Canada", 303.96)])
        pred = normalize_rows([("Canada", Decimal("303.96")), ("USA", Decimal("523.06"))])
        assert compare_results(gold, pred) == (True, True)

    def test_int_and_decimal_and_rounding_compare_equal(self):
        gold = normalize_rows([(213, 1.0 / 3)])
        pred = normalize_rows([(Decimal("213"), Decimal("0.33"))])
        assert compare_results(gold, pred) == (True, True)

    def test_extra_and_reordered_columns_are_relaxed_only(self):
        gold = normalize_rows([("Iron Maiden", 213)])
        pred = normalize_rows([(213, 90, "Iron Maiden")])  # count, artist_id, name
        assert compare_results(gold, pred) == (False, True)

    def test_values_must_stay_on_the_same_row(self):
        gold = normalize_rows([("a", 1), ("b", 2)])
        pred = normalize_rows([("a", 2), ("b", 1)])  # same columns, rows crossed
        assert compare_results(gold, pred) == (False, False)

    def test_different_row_count_fails(self):
        gold = normalize_rows([("a",)])
        pred = normalize_rows([("a",), ("a",)])
        assert compare_results(gold, pred) == (False, False)

    def test_missing_column_fails(self):
        gold = normalize_rows([("a", 1)])
        pred = normalize_rows([("a",)])
        assert compare_results(gold, pred) == (False, False)

    def test_dates_compare_as_text(self):
        gold = normalize_rows([(date(2025, 1, 1),)])
        pred = normalize_rows([("2025-01-01",)])
        assert compare_results(gold, pred) == (True, True)


class TestSwallowedComment:
    def test_one_line_with_comment_is_flagged(self):
        assert has_swallowed_comment("SELECT a, -- note b FROM t")

    def test_multiline_comment_is_fine(self):
        assert not has_swallowed_comment("SELECT a, -- note\n b FROM t")

    def test_dashes_inside_a_literal_are_fine(self):
        assert not has_swallowed_comment("SELECT * FROM t WHERE x = '--'")


class TestExporterKeepsStatementsIntact:
    def test_comments_are_stripped_before_collapsing(self):
        from tools.export_qa import _one_line

        sql = "SELECT a, -- the key\n  b\nFROM t WHERE c = '--keep' -- trailing"
        assert _one_line(sql) == "SELECT a, b FROM t WHERE c = '--keep'"
        assert not has_swallowed_comment(_one_line(sql))
