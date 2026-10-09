"""Score vanna with IBM's text2sql-eval-toolkit.

``sql_accuracy.py`` scores our own suite with our own comparison. The toolkit
(``../text2sql-eval-toolkit``) adds what that cannot: external benchmarks (BIRD
mini-dev on Postgres), more metrics -- logic-only execution accuracy (the
prediction with the reference SELECT list swapped in), BIRD's official match,
SQL parsability, an LLM judge with a written explanation per failure -- plus
failure reports and a dashboard that diffs two runs question by question.

The toolkit has no adapter for a system it does not drive itself. It reads a
predictions file per benchmark, each record carrying
``predictions[<pipeline id>] = {"predicted_sql": ..., ...}``, then executes and
scores it. This module writes vanna's SQL into that file -- asked of a running
instance exactly as ``sql_accuracy.py`` asks, read back from
``vanna_app.generations`` -- and hands over to the toolkit.

Run it in a venv that has the toolkit installed (Python >= 3.11)::

    pip install -e ../text2sql-eval-toolkit

    # 1. Registry + benchmarks for our suite (one per database), plus the
    #    toolkit's own (BIRD, Spider, ...) so one registry serves both.
    python backend/evals/toolkit_bridge.py export \\
        --target-host postgresql://postgres:postgres123@<wsl-ip>:5432 \\
        --toolkit ../text2sql-eval-toolkit

    # 2. Our suite, every database a workspace answers: predict, execute, score.
    python backend/evals/toolkit_bridge.py suite --pipeline-id vanna-baseline \\
        --target-host postgresql://postgres:postgres123@<wsl-ip>:5432 \\
        --token "$VANNA_API_TOKEN" --app-db "$VANNA_APP_DATABASE_URL" \\
        --workspace chinook=demo:postgresql://db_postgres/chinook ... \\
        --judge backend/evals/toolkit_judge.yaml

    # 3. One benchmark -- e.g. BIRD, asked in a workspace bound to its database.
    python backend/evals/toolkit_bridge.py predict bird_mini_dev_postgres_test_50 \\
        --pipeline-id vanna-baseline --workspace bird:postgresql://db_postgres/bird \\
        --token "$VANNA_API_TOKEN" --app-db "$VANNA_APP_DATABASE_URL"
    POSTGRES_CONNECTION_STRING=... python backend/evals/toolkit_bridge.py \\
        score bird_mini_dev_postgres_test_50 --judge backend/evals/toolkit_judge.yaml

Everything generated lives in ``.evals/toolkit/`` at the repository root
(git-ignored) -- outside ``backend/``, whose files the config importer catalogs
and the image build copies. It is both ``TEXT2SQL_DATA_ROOT`` (the registry and benchmarks) and
``TEXT2SQL_EVAL_TOOLKIT_DATA_ROOT`` (predictions and reports). Prediction is
resumable: a record already answered under the pipeline id is skipped, so an
interrupted run continues where it stopped. Use a new ``--pipeline-id`` per
change being measured; the dashboard compares pipelines side by side::

    TEXT2SQL_DATA_ROOT=.evals/toolkit text2sql-eval-dashboard --mode full
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.sql_accuracy import (  # noqa: E402
    DATASETS_DIR,
    Generation,
    Workspace,
    _answer_text,
    _app_db,
    generate,
    load_suite,
    parse_workspaces,
    question_preview,
)

TOOLKIT_DIR = Path(__file__).resolve().parents[2] / ".evals" / "toolkit"
DEFAULT_TOOLKIT = Path(__file__).resolve().parents[3] / "text2sql-eval-toolkit"

#: Our benchmarks are registered as ``vanna_<database>``.
PREFIX = "vanna_"


def benchmark_id(database: str) -> str:
    return f"{PREFIX}{database}"


def connection_env_var(database: str) -> str:
    return f"VANNA_EVAL_PG_{database.upper()}"


# ----------------------------------------------------------------------
# Export: our datasets in the toolkit's format (pure, unit-tested)
# ----------------------------------------------------------------------


def to_toolkit_record(record: Dict[str, Any], dataset: str, index: int) -> Dict[str, Any]:
    """One of our questions as a toolkit benchmark record.

    The id names the source file and position, so a report line leads back to
    the dataset entry. Skills become the toolkit's categories, which its
    per-category summary breaks accuracy down by, as ours does by skill.
    """
    difficulty = record.get("difficulty") or "unrated"
    return {
        "id": f"{dataset}-{index}",
        "db_id": record["database"],
        "question": record["question"],
        "sql": [record["query"]],
        "difficulty": difficulty,
        "meta": {
            "dataset": dataset,
            "hops": record.get("hops"),
            "categories": [*record.get("skills", []), f"difficulty_{difficulty}"],
        },
    }


def suite_benchmarks(suite_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Every suite question as toolkit records, grouped by database.

    One benchmark per database rather than per file: the toolkit runs a
    Postgres benchmark against a single connection and ``search_path``, and
    ignores the record's ``db_id``.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in load_suite(suite_path):
        records = json.loads(entry["path"].read_text(encoding="utf-8"))
        name = entry["path"].stem
        grouped.setdefault(entry["database"], []).extend(
            to_toolkit_record(r, name, i) for i, r in enumerate(records)
        )
    return grouped


def registry_entry(database: str) -> Dict[str, Any]:
    bid = benchmark_id(database)
    return {
        "name": bid,
        "description": f"vanna sql_accuracy suite, database {database}",
        "data": f"benchmarks/{bid}.json",
        "schema": f"benchmarks/{bid}-schema.json",
        "predictions": f"results/{bid}-predictions.json",
        "db_engine": {
            "db_type": "postgres",
            "connection_string_env_var": connection_env_var(database),
            "schema_name": database,
            "db_folder": "",
        },
    }


def toolkit_registry_entries(toolkit: Path) -> Dict[str, Dict[str, Any]]:
    """The toolkit's own benchmarks, with input paths made absolute.

    ``TEXT2SQL_DATA_ROOT`` replaces the packaged registry rather than adding to
    it, so BIRD and friends are copied in. Their predictions are written here,
    beside ours, not into the toolkit checkout.
    """
    entries: Dict[str, Dict[str, Any]] = {}
    data = toolkit / "data"
    for name in ("benchmarks.json", "test-benchmarks.json"):
        path = data / name
        if not path.is_file():
            continue
        for bid, entry in json.loads(path.read_text(encoding="utf-8")).items():
            entry = dict(entry)
            for key in ("data", "schema"):
                if entry.get(key) and not Path(entry[key]).is_absolute():
                    entry[key] = str((data / entry[key]).resolve())
            db_engine = dict(entry.get("db_engine") or {})
            folder = db_engine.get("db_folder")
            if folder and not Path(folder).is_absolute():
                db_engine["db_folder"] = str((data / folder).resolve())
            entry["db_engine"] = db_engine
            entry["predictions"] = f"results/{bid}-predictions.json"
            entries[bid] = entry
    return entries


def extract_schema(engine: Any, database: str) -> Dict[str, Any]:
    """The database's tables in the toolkit's schema format, keyed by ``db_id``.

    Only the toolkit's own pipelines read it -- the zero-shot baseline that
    vanna is compared against. vanna reads its schema from the live database.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    tables: Dict[str, Any] = {}
    for table in inspector.get_table_names(schema=database):
        pk = set(inspector.get_pk_constraint(table, schema=database).get("constrained_columns") or [])
        fks: Dict[str, List[Dict[str, str]]] = {}
        for fk in inspector.get_foreign_keys(table, schema=database):
            for local, remote in zip(fk["constrained_columns"], fk["referred_columns"]):
                fks.setdefault(local, []).append(
                    {"target_table": fk["referred_table"], "target_column": remote}
                )
        tables[table] = {
            "name": table,
            "columns": [
                {
                    "name": c["name"],
                    "type": str(c["type"]),
                    "primary_key": c["name"] in pk,
                    "foreign_keys": fks.get(c["name"], []),
                    "description": c.get("comment") or "",
                }
                for c in inspector.get_columns(table, schema=database)
            ],
        }
    return {database: {"name": database, "tables": tables}}


def export(args: argparse.Namespace) -> int:
    root = Path(args.root)
    (root / "benchmarks").mkdir(parents=True, exist_ok=True)
    registry: Dict[str, Any] = {}
    toolkit = Path(args.toolkit) if args.toolkit else None
    if toolkit:
        registry.update(toolkit_registry_entries(toolkit))

    for database, records in suite_benchmarks(Path(args.suite)).items():
        entry = registry_entry(database)
        registry[entry["name"]] = entry
        _write_json(root / entry["data"], records)
        schema: Dict[str, Any] = {}
        if args.target_host:
            from sqlalchemy import create_engine

            url = _sqlalchemy_url(f"{_host(args)}/{database}")
            schema = extract_schema(create_engine(url), database)
        _write_json(root / entry["schema"], schema)
        print(f"{entry['name']}: {len(records)} questions"
              + ("" if schema else " (no schema: pass --target-host for the baseline)"))

    _write_json(root / "benchmarks.json", registry)
    print(f"Registry: {root / 'benchmarks.json'} ({len(registry)} benchmarks)")
    return 0


# ----------------------------------------------------------------------
# Predict: vanna's SQL into the predictions file
# ----------------------------------------------------------------------


def record_id(record: Dict[str, Any]) -> str:
    """The toolkit's id lookup order (``utils.get_question_id``)."""
    for key in ("id", "question_id", "qid", "_id"):
        if record.get(key) is not None:
            return str(record[key])
    raise KeyError(f"record has no id: {sorted(record)}")


def record_question(record: Dict[str, Any]) -> str:
    for key in ("question", "utterance", "page_content"):
        if record.get(key):
            return str(record[key])
    raise KeyError(f"record has no question: {sorted(record)}")


def message_for(record: Dict[str, Any], with_evidence: bool) -> str:
    """What is sent to the chat: the question, plus BIRD's hint if asked for.

    BIRD's ``evidence`` defines the terms a question uses ("eligible free rate
    = ..."). Running with and without it separates "could not write the SQL"
    from "did not know what the words meant".
    """
    question = record_question(record)
    evidence = (record.get("evidence") or "").strip()
    return f"{question}\n\nHint: {evidence}" if with_evidence and evidence else question


def merge_records(
    benchmark: List[Dict[str, Any]], existing: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The predictions file for *benchmark*, keeping what *existing* answered.

    A question added to the dataset since the last run is appended; one removed
    is dropped, so the report never scores a question no longer asked.
    """
    by_id = {record_id(r): r for r in existing}
    merged = []
    for record in benchmark:
        kept = by_id.get(record_id(record))
        merged.append(kept if kept is not None else {**record, "predictions": {}})
    return merged


def needs_prediction(record: Dict[str, Any], pipeline_id: str, force: bool) -> bool:
    return force or pipeline_id not in (record.get("predictions") or {})


def agent_trace(message: str, generation: Generation, answer: str) -> List[Dict[str, Any]]:
    """The turn in the toolkit's trace shape -- the judge and error report read it."""
    trace: List[Dict[str, Any]] = [
        {"step": "question", "messages": [{"role": "user", "content": message}]}
    ]
    trace.extend({"step": "clarification", "response": note} for note in generation.notes)
    for row in generation.rows:
        response = row.get("sql") or ""
        if row.get("error"):
            response += f"\n-- error: {row['error']}"
        trace.append({"step": f"run_sql ({row.get('status')})", "response": response})
    if answer:
        trace.append({"step": "answer", "response": answer})
    return trace


def prediction_entry(
    message: str, generation: Generation, seconds: float, pipeline_id: str
) -> Dict[str, Any]:
    """A toolkit prediction for one vanna turn.

    No successful statement becomes an ``inference_error``: the toolkit scores
    it 0 on every metric and lists it under inference failures, which is what
    "asked a clarifying question instead" or "gave up" should cost.
    """
    rows = generation.rows
    answer = _answer_text(generation.body)
    last = rows[-1] if rows else {}
    prompt_tokens = last.get("prompt_tokens") or 0
    completion_tokens = last.get("completion_tokens") or 0
    entry: Dict[str, Any] = {
        "model_name": pipeline_id,
        "inference_time_ms": round(seconds * 1000),
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "agent_trace": agent_trace(message, generation, answer),
        # Not read by the toolkit; kept for triage (which tables retrieval
        # offered, whether the agent asked instead of answering).
        "vanna": {
            "conversation_id": generation.conversation_id,
            "clarified": generation.clarified,
            "attempts": len(rows),
            "statuses": [r.get("status") for r in rows],
            "retrieval_strategy": (generation.final or last).get("retrieval_strategy"),
            "retrieved_tables": (generation.final or last).get("retrieved_table_names") or [],
        },
    }
    if generation.final is not None:
        entry["predicted_sql"] = generation.final["sql"]
    else:
        entry["inference_error"] = generation.failure
        if answer:
            entry["inference_error"] += f": {answer}"
    return entry


def predict(args: argparse.Namespace, bid: str, workspace: Workspace) -> int:
    _toolkit_env(args)
    from text2sql_eval_toolkit.utils import get_benchmark_info

    info = get_benchmark_info(bid)
    benchmark = json.loads(Path(info["benchmark_json_path"]).read_text(encoding="utf-8"))
    path = Path(info["predictions_path"])
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    records = merge_records(benchmark, existing)
    asked = records[: args.limit] if args.limit else records
    todo = [r for r in asked if needs_prediction(r, args.pipeline_id, args.force)]
    print(f"\n===== {bid}: {len(todo)} of {len(asked)} to ask as {args.pipeline_id!r}",
          flush=True)

    app_db = _app_db(args)
    db_lock = threading.Lock()
    file_lock = threading.Lock()
    failures = 0

    def ask(record: Dict[str, Any]) -> Dict[str, Any]:
        message = message_for(record, args.with_evidence)
        started = time.monotonic()
        generation = generate(args, workspace, message, _Locked(app_db, db_lock))
        return prediction_entry(message, generation, time.monotonic() - started,
                                args.pipeline_id)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(ask, r): r for r in todo}
        for done, future in enumerate(as_completed(futures), 1):
            record = futures[future]
            try:
                entry = future.result()
            except Exception as exc:  # server down, timeout: retried next run
                failures += 1
                print(f"[{done:>3}/{len(todo)}] ERROR {type(exc).__name__}: {exc}"[:300],
                      flush=True)
                continue
            mark = "sql" if "predicted_sql" in entry else "no-sql"
            print(f"[{done:>3}/{len(todo)}] {mark:<6} {entry['inference_time_ms'] / 1000:>6.1f}s  "
                  f"{question_preview(record_question(record))}", flush=True)
            with file_lock:
                record.setdefault("predictions", {})[args.pipeline_id] = entry
                _write_json(path, records)
    _write_json(path, records)
    if failures:
        print(f"{failures} request(s) failed; run again to retry them.", file=sys.stderr)
    return 1 if failures else 0


class _Locked:
    """A psycopg2 connection whose ``cursor()`` blocks are serialized across threads."""

    def __init__(self, connection: Any, lock: threading.Lock) -> None:
        self._connection = connection
        self._lock = lock

    def cursor(self) -> Any:
        lock, cursor = self._lock, self._connection.cursor()

        class _Cursor:
            def __enter__(self) -> Any:
                lock.acquire()
                return cursor.__enter__()

            def __exit__(self, *exc: Any) -> None:
                try:
                    cursor.__exit__(*exc)
                finally:
                    lock.release()

        return _Cursor()


# ----------------------------------------------------------------------
# Score: the toolkit's execution and evaluation
# ----------------------------------------------------------------------


def score(args: argparse.Namespace, bid: str) -> int:
    _toolkit_env(args)
    from text2sql_eval_toolkit import run_evaluation, run_execution
    from text2sql_eval_toolkit.analysis.error_analysis import export_failed_examples_to_markdown
    from text2sql_eval_toolkit.analysis.report_tools import (
        export_summary_results_by_category_to_markdown,
    )
    from text2sql_eval_toolkit.evaluation import evaluation_tools
    from text2sql_eval_toolkit.utils import get_benchmark_info

    evaluation_tools.json = _NumpySafeJson()
    if bid.startswith(PREFIX):
        _keep_sql_as_written()
    info = get_benchmark_info(bid)
    if not Path(info["predictions_path"]).is_file():
        print(f"{bid}: no predictions yet -- run predict first.", file=sys.stderr)
        return 2
    run_execution(bid, num_threads=args.db_threads, force_rerun=args.force)
    _, summary = run_evaluation(
        bid,
        use_llm=bool(args.judge),
        llm_judge_config_path=args.judge or None,
        force_rerun=args.force,
    )
    eval_path = Path(info["eval_results_path"])
    records = json.loads(eval_path.read_text(encoding="utf-8"))
    errors_md = eval_path.with_name(eval_path.stem + "_errors.md")
    export_failed_examples_to_markdown(records, errors_md, max_examples=args.max_errors)
    export_summary_results_by_category_to_markdown(
        records, eval_path.with_name(eval_path.stem + "_summary.md")
    )
    print(summary.to_string() if hasattr(summary, "to_string") else summary)
    print(f"Failures: {errors_md}", flush=True)
    return 0


def _keep_sql_as_written() -> None:
    """Execute predictions on our databases exactly as they were written.

    Before running a prediction on Postgres, toolkit 1.6.0 re-renders it
    through sqlglot to quote mixed-case identifiers (BIRD's Postgres port has
    them) and stores the result over ``predicted_sql``. The re-render is not
    faithful: ``100.0 * a / b`` comes back as ``CAST(... AS DOUBLE PRECISION)``,
    and Postgres has no ``ROUND(double precision, integer)`` -- so a query that
    ran in vanna fails in the toolkit. Our schemas are all lower case, so the
    quoting buys nothing here. BIRD keeps the toolkit's behaviour.
    """
    from text2sql_eval_toolkit.execution import execution_tools

    execution_tools.quote_mixed_case_columns = lambda sql: sql


class _NumpySafeJson:
    """``json`` for the toolkit's evaluation module, writing numpy scalars.

    With a judge configured, toolkit 1.6.0 puts numpy ``int64`` counts in the
    summary it writes, and ``json.dump`` refuses them -- after every verdict
    has been paid for. Everything but ``dump``/``dumps`` is the real module.
    """

    @staticmethod
    def _default(value: Any) -> Any:
        if hasattr(value, "item"):
            return value.item()
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    def dump(self, obj: Any, fp: Any, **kwargs: Any) -> None:
        kwargs.setdefault("default", self._default)
        json.dump(obj, fp, **kwargs)

    def dumps(self, obj: Any, **kwargs: Any) -> str:
        kwargs.setdefault("default", self._default)
        return json.dumps(obj, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(json, name)


def suite(args: argparse.Namespace) -> int:
    """Predict and score every suite database a ``--workspace`` answers."""
    try:
        workspaces = parse_workspaces(args.workspace)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    databases = {e["database"] for e in load_suite(Path(args.suite))}
    status = 0
    for database in sorted(databases):
        workspace = workspaces.get(database)
        if workspace is None:
            print(f"{benchmark_id(database)}: skipped, no --workspace for {database!r}")
            continue
        status = max(status, predict(args, benchmark_id(database), workspace))
        status = max(status, score(args, benchmark_id(database)))
    return status


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _toolkit_env(args: argparse.Namespace) -> None:
    """Point the toolkit at our registry and results, and our databases at
    ``--target-host``. Variables already set win, as the toolkit's own
    ``load_env`` does."""
    root = str(Path(args.root).resolve())
    # The toolkit draws its report charts from worker threads. Under WSLg (or
    # any desktop) matplotlib picks the Tk backend, and Tk torn down off the
    # main thread kills the process -- after the judge has been paid for.
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("TEXT2SQL_DATA_ROOT", root)
    os.environ.setdefault("TEXT2SQL_EVAL_TOOLKIT_DATA_ROOT", root)
    if getattr(args, "target_host", ""):
        for entry in load_suite(Path(args.suite)):
            os.environ.setdefault(connection_env_var(entry["database"]),
                                  f"{_host(args)}/{entry['database']}")


def _host(args: argparse.Namespace) -> str:
    return args.target_host.rstrip("/")


def _sqlalchemy_url(url: str) -> str:
    """*url* with a driver SQLAlchemy can load in the toolkit's venv.

    SQLAlchemy 2.1 maps a bare ``postgresql://`` to psycopg 3, but the toolkit
    installs only psycopg2 (asyncpg runs its queries). The bare URL is still
    what the toolkit's ``VANNA_EVAL_PG_*`` variables need.
    """
    import importlib.util

    if url.startswith("postgresql://") and importlib.util.find_spec("psycopg") is None:
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _parse(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=str(TOOLKIT_DIR),
                        help="Registry, benchmarks and results (default .evals/toolkit)")
    common.add_argument("--suite", default=str(DATASETS_DIR / "suite.json"))
    common.add_argument("--target-host", default="",
                        help="postgresql://user:pass@host:5432 -- each database name is appended")

    asking = argparse.ArgumentParser(add_help=False)
    asking.add_argument("--pipeline-id", required=True,
                        help="Names this run in the toolkit's reports, e.g. vanna-baseline")
    asking.add_argument("--server", default="http://localhost:3000")
    asking.add_argument("--token", required=True, help="API token (Bearer) for an analyst")
    asking.add_argument("--app-db", required=True, help="Control-plane Postgres URL")
    asking.add_argument("--answer-clarifications", action="store_true",
                        help="Reply to a clarification with its first option, as sql_accuracy does")
    asking.add_argument("--with-evidence", action="store_true",
                        help="Append a record's evidence (BIRD hints) to the question")
    asking.add_argument("--workers", type=int, default=3)
    asking.add_argument("--limit", type=int, default=0, help="Only the first N questions")
    asking.add_argument("--timeout", type=float, default=300.0)

    scoring = argparse.ArgumentParser(add_help=False)
    scoring.add_argument("--judge", default="",
                         help="LLM judge config YAML; omit to skip the judge")
    scoring.add_argument("--db-threads", type=int, default=8)
    scoring.add_argument("--max-errors", type=int, default=200,
                         help="Failures written to the *_errors.md report per pipeline")

    forcing = argparse.ArgumentParser(add_help=False)
    forcing.add_argument("--force", action="store_true",
                         help="Redo work already stored (asking, executing or judging)")

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", parents=[common],
                       help="Write the registry and our suite as toolkit benchmarks")
    e.add_argument("--toolkit", default=str(DEFAULT_TOOLKIT) if DEFAULT_TOOLKIT.is_dir() else "",
                   help="toolkit checkout whose benchmarks (BIRD, ...) join the registry")
    pr = sub.add_parser("predict", parents=[common, asking, forcing],
                        help="Ask vanna one benchmark's questions")
    pr.add_argument("benchmark")
    pr.add_argument("--workspace", required=True, metavar="TENANT[:DATA_SOURCE]")
    sc = sub.add_parser("score", parents=[common, scoring, forcing],
                        help="Execute and evaluate one benchmark's predictions")
    sc.add_argument("benchmark")
    su = sub.add_parser("suite", parents=[common, asking, scoring, forcing],
                        help="predict + score for every suite database with a workspace")
    su.add_argument("--workspace", action="append", default=[],
                    metavar="DATABASE=TENANT[:DATA_SOURCE]")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if os.name == "nt" and not sys.flags.utf8_mode and argv is None:
        # The toolkit writes its markdown reports (emoji headings) with the
        # locale encoding, which on Windows is cp1252 and cannot encode them.
        import subprocess

        return subprocess.call([sys.executable, "-X", "utf8", *sys.argv])
    args = _parse(argv)
    if args.command == "export":
        return export(args)
    if args.command == "predict":
        tenant, _, data_source = args.workspace.partition(":")
        return predict(args, args.benchmark, Workspace(tenant=tenant, data_source=data_source))
    if args.command == "score":
        return score(args, args.benchmark)
    return suite(args)


if __name__ == "__main__":
    sys.exit(main())
