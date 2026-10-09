"""`evals/sql_accuracy.py` -- the pure parts (result comparison, the
reference-SQL sanity check, suite plumbing) and the datasets themselves.
Running the eval needs a live instance."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
import sqlglot

from evals.sql_accuracy import (
    DATASETS_DIR,
    Outcome,
    compare_results,
    has_swallowed_comment,
    load_suite,
    normalize_rows,
    parse_workspaces,
    summarize,
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

    def test_rounding_matches_sql_round_half_up(self):
        """Found live: an unrounded 49979.9050 against ROUND(...) = 49979.91."""
        gold = normalize_rows([("Ireland", Decimal("49979.91"))])
        pred = normalize_rows([("Ireland", Decimal("49979.9050"))])
        assert compare_results(gold, pred) == (True, True)
        assert normalize_rows([(2.675,)]) == [(2.68,)]  # float repr, not 2.67499...

    def test_zero_filled_groups_are_relaxed_only(self):
        """Found live: 'each store' answered with 498 stores at 0 rentals."""
        gold = normalize_rows([(1, 25761), (2, 26044)])
        pred = normalize_rows([(1, 25761), (2, 26044), (3, 0), (4, None)])
        assert compare_results(gold, pred) == (False, True)

    def test_extra_rows_with_real_values_still_fail(self):
        gold = normalize_rows([("a", 5)])
        pred = normalize_rows([("a", 5), ("b", 3)])
        assert compare_results(gold, pred) == (False, False)

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


# ----------------------------------------------------------------------
# The datasets themselves -- checked offline on every run, so a malformed
# question is caught before it costs an eval. Live checks (the gold SQL runs
# and returns rows) are `sql_accuracy.py --suite ... --check-gold`.
# ----------------------------------------------------------------------

SUITE = load_suite(DATASETS_DIR / "suite.json")
RECORDS = [
    (entry["path"].stem, entry["database"], i, record)
    for entry in SUITE
    for i, record in enumerate(json.loads(entry["path"].read_text(encoding="utf-8")))
]
DIFFICULTIES = {"easy", "medium", "hard"}


class TestDatasets:
    def test_suite_lists_every_dataset_file(self):
        listed = {entry["path"].name for entry in SUITE}
        on_disk = {p.name for p in DATASETS_DIR.glob("*.json") if p.name != "suite.json"}
        assert listed == on_disk

    @pytest.mark.parametrize("name,database,index,record", RECORDS,
                             ids=[f"{n}[{i}]" for n, _, i, _ in RECORDS])
    def test_record_is_well_formed(self, name, database, index, record):
        assert record["question"].strip().endswith(("?", "."))
        assert record["database"] == database
        assert record.get("difficulty") in DIFFICULTIES
        assert record.get("skills"), "skills drive the per-skill breakdown"
        assert not has_swallowed_comment(record["query"])
        # Every table is schema-qualified with the dataset's own schema, so the
        # gold SQL cannot silently read another database's table.
        tree = sqlglot.parse_one(record["query"], read="postgres")
        ctes = {c.alias_or_name for c in tree.find_all(sqlglot.exp.CTE)}
        for table in tree.find_all(sqlglot.exp.Table):
            if table.name not in ctes:
                assert table.db == database, f"{table.sql()} is not in schema {database}"

    def test_questions_are_unique(self):
        questions = [r["question"] for _, _, _, r in RECORDS]
        assert len(questions) == len(set(questions))

    def test_the_suite_is_mostly_hard(self):
        """The point of the newer sets: multi-hop alone no longer separates models."""
        hard = sum(r["difficulty"] == "hard" for _, _, _, r in RECORDS)
        assert hard / len(RECORDS) >= 0.4


class TestSuiteCli:
    def test_workspace_mapping_keeps_colons_in_the_data_source(self):
        mapping = parse_workspaces([
            "chinook=demo:postgresql://db_postgres/chinook",
            "pagila=acme",
        ])
        assert mapping["chinook"].tenant == "demo"
        assert mapping["chinook"].data_source == "postgresql://db_postgres/chinook"
        assert mapping["pagila"].data_source == ""

    def test_a_malformed_mapping_is_refused(self):
        with pytest.raises(ValueError):
            parse_workspaces(["chinook"])

    def test_summary_breaks_down_by_difficulty_and_skill(self):
        outcomes = [
            Outcome(index=0, question="a", difficulty="hard", skills=["fan_trap"], relaxed=True),
            Outcome(index=1, question="b", difficulty="hard", skills=["fan_trap", "window"]),
            Outcome(index=2, question="c", difficulty="easy", skills=["window"], relaxed=True),
        ]
        summary = summarize("t", outcomes)
        assert summary["by_difficulty"]["hard"] == {"n": 2, "relaxed": 1, "rate": 0.5}
        assert summary["by_skill"]["window"]["rate"] == 0.5
        assert summary["by_skill"]["fan_trap"]["n"] == 2


class TestClarificationCards:
    """`request_clarification` ends the turn with a card of complete questions."""

    BODY = {"chunks": [
        {"rich": {"type": "status_card", "title": "Tool completed"}, "simple": {"text": "ok"}},
        {"rich": {"type": "card", "data": {
            "title": "Which did you mean?",
            "actions": [
                {"label": "Customer country", "action": "Revenue for customers whose country is Brazil?"},
                {"label": "Billing country", "action": "Revenue for invoices billed to Brazil?"},
            ],
        }}, "simple": {"text": "Which did you mean?"}},
    ]}

    def test_options_are_found_wherever_the_card_nests(self):
        from evals.sql_accuracy import clarification_options

        assert clarification_options(self.BODY) == [
            "Revenue for customers whose country is Brazil?",
            "Revenue for invoices billed to Brazil?",
        ]

    def test_an_ordinary_answer_has_no_options(self):
        from evals.sql_accuracy import clarification_options

        assert clarification_options({"chunks": [{"rich": {"type": "dataframe"}}]}) == []
