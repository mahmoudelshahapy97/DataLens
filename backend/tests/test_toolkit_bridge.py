"""`evals/toolkit_bridge.py` -- converting our suite to the toolkit's format and
vanna turns to toolkit predictions. Running it needs a live instance and the
toolkit installed; none of this does."""

from __future__ import annotations

import argparse
import json

from evals import sql_accuracy
from evals.sql_accuracy import DATASETS_DIR, Generation, Workspace, generate
from evals.toolkit_bridge import (
    _parse,
    agent_trace,
    benchmark_id,
    export,
    merge_records,
    message_for,
    needs_prediction,
    prediction_entry,
    registry_entry,
    suite_benchmarks,
    to_toolkit_record,
    toolkit_registry_entries,
)

SUITE = DATASETS_DIR / "suite.json"


def _row(sql: str, status: str = "valid", **extra):
    return {"sql": sql, "status": status, "error": None, "prompt_tokens": 100,
            "completion_tokens": 20, "retrieval_strategy": "full",
            "retrieved_table_names": ["chinook.track"], **extra}


class TestExport:
    RECORD = {"question": "How many tracks?", "query": "SELECT COUNT(*) FROM chinook.track",
              "database": "chinook", "difficulty": "easy", "skills": ["aggregate"], "hops": 0}

    def test_record_keeps_gold_as_a_list_and_skills_as_categories(self):
        record = to_toolkit_record(self.RECORD, "complex_chinook", 3)
        assert record["id"] == "complex_chinook-3"
        assert record["db_id"] == "chinook"
        assert record["sql"] == ["SELECT COUNT(*) FROM chinook.track"]
        assert record["meta"]["categories"] == ["aggregate", "difficulty_easy"]

    def test_suite_is_one_benchmark_per_database_with_every_question(self):
        grouped = suite_benchmarks(SUITE)
        files = sql_accuracy.load_suite(SUITE)
        total = sum(len(json.loads(e["path"].read_text(encoding="utf-8"))) for e in files)
        assert set(grouped) == {e["database"] for e in files}
        assert sum(len(r) for r in grouped.values()) == total
        ids = [r["id"] for records in grouped.values() for r in records]
        assert len(ids) == len(set(ids))
        assert all(r["db_id"] == db for db, records in grouped.items() for r in records)

    def test_registry_entry_runs_against_the_database_schema(self):
        entry = registry_entry("pagila")
        assert entry["name"] == benchmark_id("pagila") == "vanna_pagila"
        assert entry["db_engine"]["schema_name"] == "pagila"
        assert entry["db_engine"]["connection_string_env_var"] == "VANNA_EVAL_PG_PAGILA"

    def test_toolkit_benchmarks_get_absolute_inputs_and_local_predictions(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "benchmarks.json").write_text(json.dumps({"bird": {
            "data": "benchmarks/bird.json", "schema": "benchmarks/bird-schema.json",
            "predictions": "results/bird-predictions.json",
            "db_engine": {"db_type": "sqlite", "db_folder": "benchmarks/dbs"},
        }}), encoding="utf-8")
        entry = toolkit_registry_entries(tmp_path)["bird"]
        assert entry["data"] == str((data / "benchmarks/bird.json").resolve())
        assert entry["db_engine"]["db_folder"] == str((data / "benchmarks/dbs").resolve())
        assert entry["predictions"] == "results/bird-predictions.json"

    def test_export_writes_registry_and_benchmarks(self, tmp_path):
        args = _parse(["export", "--root", str(tmp_path), "--toolkit", ""])
        assert export(args) == 0
        registry = json.loads((tmp_path / "benchmarks.json").read_text(encoding="utf-8"))
        assert "vanna_chinook" in registry
        records = json.loads((tmp_path / registry["vanna_chinook"]["data"]).read_text("utf-8"))
        assert records and all(r["db_id"] == "chinook" for r in records)


class TestPredictionFile:
    def test_merge_keeps_answers_adds_new_questions_and_drops_removed_ones(self):
        benchmark = [{"id": "a", "question": "A?"}, {"id": "c", "question": "C?"}]
        existing = [{"id": "a", "question": "A?", "predictions": {"p": {"predicted_sql": "x"}}},
                    {"id": "b", "question": "B?", "predictions": {}}]
        merged = merge_records(benchmark, existing)
        assert [r["id"] for r in merged] == ["a", "c"]
        assert merged[0]["predictions"]["p"]["predicted_sql"] == "x"
        assert merged[1]["predictions"] == {}

    def test_bird_records_are_keyed_by_question_id(self):
        merged = merge_records([{"question_id": 7, "question": "Q?"}], [])
        assert merged[0]["question_id"] == 7

    def test_answered_records_are_skipped_unless_forced(self):
        record = {"predictions": {"vanna-a": {"predicted_sql": "x"}}}
        assert not needs_prediction(record, "vanna-a", force=False)
        assert needs_prediction(record, "vanna-a", force=True)
        assert needs_prediction(record, "vanna-b", force=False)

    def test_evidence_is_appended_only_when_asked(self):
        record = {"question": "Ratio of EUR to CZK?", "evidence": "ratio = EUR / CZK"}
        assert message_for(record, with_evidence=False) == "Ratio of EUR to CZK?"
        assert message_for(record, with_evidence=True).endswith("Hint: ratio = EUR / CZK")
        assert message_for({"question": "Q?", "evidence": ""}, True) == "Q?"


class TestPredictionEntry:
    def test_the_last_successful_statement_is_the_prediction(self):
        generation = Generation("c1", {"chunks": []}, rows=[
            _row("SELECT 1"), _row("SELECT bad", "invalid", error="syntax"), _row("SELECT 2"),
        ])
        entry = prediction_entry("Q?", generation, 2.5, "vanna-x")
        assert entry["predicted_sql"] == "SELECT 2"
        assert "inference_error" not in entry
        assert entry["inference_time_ms"] == 2500
        assert entry["token_usage"] == {"prompt_tokens": 100, "completion_tokens": 20,
                                        "total_tokens": 120}
        assert entry["vanna"]["statuses"] == ["valid", "invalid", "valid"]
        assert entry["vanna"]["retrieved_tables"] == ["chinook.track"]

    def test_no_sql_is_an_inference_error_carrying_what_the_agent_said(self):
        body = {"chunks": [{"simple": {"text": "Did you mean billing or customer country?"}}]}
        entry = prediction_entry("Q?", Generation("c1", body, clarified=True), 1.0, "p")
        assert "predicted_sql" not in entry
        assert entry["inference_error"].startswith("agent ran no SQL: Did you mean")
        assert entry["vanna"]["clarified"] is True

    def test_trace_shows_question_clarification_statements_and_answer(self):
        generation = Generation("c1", {}, notes=["clarification answered with: X?"],
                                rows=[_row("SELECT bad", "invalid", error="no such column")])
        trace = agent_trace("Q?", generation, "Here you go")
        assert trace[0] == {"step": "question", "messages": [{"role": "user", "content": "Q?"}]}
        assert [t["step"] for t in trace[1:]] == ["clarification", "run_sql (invalid)", "answer"]
        assert "no such column" in trace[2]["response"]


class TestToolkitWorkarounds:
    def test_numpy_scalars_are_written_as_plain_numbers(self):
        from evals.toolkit_bridge import _NumpySafeJson

        class Int64:  # what pandas hands the toolkit's summary
            def item(self):
                return 7

        assert json.loads(_NumpySafeJson().dumps({"num_correct_llm": Int64()})) == {
            "num_correct_llm": 7
        }
        assert _NumpySafeJson().loads("[1]") == [1]

    def test_reports_render_without_a_display(self, tmp_path, monkeypatch):
        from evals.toolkit_bridge import _toolkit_env

        monkeypatch.delenv("MPLBACKEND", raising=False)
        monkeypatch.delenv("TEXT2SQL_DATA_ROOT", raising=False)
        monkeypatch.delenv("TEXT2SQL_EVAL_TOOLKIT_DATA_ROOT", raising=False)
        _toolkit_env(argparse.Namespace(root=str(tmp_path), target_host="", suite=str(SUITE)))
        import os

        assert os.environ["MPLBACKEND"] == "Agg"
        assert os.environ["TEXT2SQL_DATA_ROOT"] == str(tmp_path.resolve())


class TestGenerate:
    """The ask/read-back step `run_one` and the bridge share."""

    CARD = {"chunks": [{"rich": {"actions": [{"label": "a", "action": "Option A?"}]}}]}

    def _args(self, answer: bool) -> argparse.Namespace:
        return argparse.Namespace(answer_clarifications=answer)

    def test_a_clarification_is_answered_in_the_same_conversation(self, monkeypatch):
        asked = []

        def fake_ask(args, workspace, question, conversation_id=None):
            asked.append((question, conversation_id))
            return "conv-1", self.CARD if len(asked) == 1 else {"chunks": []}

        monkeypatch.setattr(sql_accuracy, "_ask", fake_ask)
        monkeypatch.setattr(sql_accuracy, "_generations", lambda db, t, c: [_row("SELECT 1")])
        generation = generate(self._args(True), Workspace("demo"), "Q?", None)
        assert asked == [("Q?", None), ("Option A?", "conv-1")]
        assert generation.clarified and generation.final["sql"] == "SELECT 1"

    def test_without_answering_the_turn_has_no_sql(self, monkeypatch):
        monkeypatch.setattr(sql_accuracy, "_ask", lambda *a, **k: ("conv-1", self.CARD))
        monkeypatch.setattr(sql_accuracy, "_generations", lambda db, t, c: [])
        generation = generate(self._args(False), Workspace("demo"), "Q?", None)
        assert generation.clarified and generation.final is None
        assert generation.failure == "agent ran no SQL"
