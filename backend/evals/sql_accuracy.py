"""Execution accuracy: does the agent's SQL return the same rows as the reference?

The trajectory dataset (``datasets/sql_generation/basic.yaml``) checks which tools
the agent reaches for. This checks the thing users care about -- the numbers. Each
question is sent to a **running** instance exactly as the UI sends it, the SQL the
agent actually executed is read back from ``vanna_app.generations`` (the recording
``run_sql`` tool writes every statement there), and both that SQL and the
reference SQL are executed against the warehouse and their result sets compared.

Driving a live instance rather than building an agent in-process is deliberate:
what is measured is the whole stack -- retrieval, the enhancer chain, grants, the
critic -- not a reconstruction of it that can drift from what ships.

    python backend/evals/sql_accuracy.py \\
        --server http://localhost:3000 --token "$VANNA_API_TOKEN" \\
        --tenant chinook --data-source chinook \\
        --app-db "$VANNA_APP_DATABASE_URL" \\
        --target-db postgresql://user:pass@localhost:5432/chinook \\
        --dataset qa.json --label full

Datasets live in ``datasets/sql_accuracy/``, listed in ``suite.json``: multi-hop
sets for Chinook, Northwind (no declared foreign keys -- it measures inferred
relationships) and Pagila, harder ``complex_*`` sets for the same three, and
sets for ecommerce, healthcare, booking, employees and world. Each question
carries a ``difficulty`` and ``skills`` (anti_join, fan_trap, top_n_per_group,
temporal_current_row, ...); reports break accuracy down by both.

The whole suite, one database per dataset on a single server -- gold check
first, then the agent for whichever databases a workspace answers:

    python backend/evals/sql_accuracy.py --suite backend/evals/datasets/sql_accuracy/suite.json \\
        --target-host postgresql://user:pass@localhost:5432 --check-gold

    python backend/evals/sql_accuracy.py --suite backend/evals/datasets/sql_accuracy/suite.json \\
        --target-host postgresql://user:pass@localhost:5432 \\
        --token "$VANNA_API_TOKEN" --app-db "$VANNA_APP_DATABASE_URL" \\
        --workspace chinook=demo:postgresql://db_postgres/chinook \\
        --workspace pagila=acme:postgresql://db_postgres/pagila --label full

One dataset, gold check only -- this runs only the reference SQL, no agent, no
model calls:

    python backend/evals/sql_accuracy.py --check-gold \\
        --dataset backend/evals/datasets/sql_accuracy/multi_hop_pagila.json \\
        --target-db postgresql://user:pass@localhost:5432/pagila

Run it twice to get the number the schema-graph work is gated on: once normally
(``--label full``) and once against a server started with
``VANNA_SCHEMA_FULL_TEXT_THRESHOLD=0`` (``--label search``), which makes even
Chinook take the search path a large warehouse takes. The gap between the two is
what graph-expanded retrieval can win back.

Two scores per question:

* **exact** -- same columns in the same order, same rows (order ignored).
* **relaxed** -- every reference column is matched by some generated column and
  the rows agree on those columns. Tolerates an extra ``artist_id`` beside the
  ``name`` that was asked for, or columns in a different order, which a human
  grader would accept.

Row order is ignored in both. Floats compare at two decimal places.

**About the reference SQL.** ``qa.json`` is exported from chat history by
``tools/export_qa.py``: it is what the agent answered once, not hand-verified
ground truth. Scoring against it measures agreement with that run. Review the
reference queries before treating a score as accuracy.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Projections tried before relaxed matching gives up. Real results have a
#: handful of columns; this only bounds a pathological wide result.
_MAX_PROJECTIONS = 2_000

_CENT = Decimal("0.01")

Rows = List[Tuple[Any, ...]]


# ----------------------------------------------------------------------
# Result comparison (pure, unit-tested in tests/test_sql_accuracy.py)
# ----------------------------------------------------------------------


def normalize_value(value: Any) -> Any:
    """Make values from two different queries comparable.

    ``COUNT(*)`` returns an int where ``SUM(numeric)`` returns a Decimal, and a
    ``ROUND(.., 2)`` in one query but not the other must not count as a different
    answer -- so every number becomes a float at two decimals.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        # Half-up in decimal, as SQL's ROUND does. `round(float(x), 2)` sends
        # an exact 49979.905 to 49979.9 (the float is 49979.90499...), so an
        # unrounded correct answer failed against a reference that said ROUND.
        exact = value if isinstance(value, Decimal) else Decimal(repr(value))
        return float(exact.quantize(_CENT, rounding=ROUND_HALF_UP))
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value).strip()


def normalize_rows(rows: Sequence[Sequence[Any]]) -> Rows:
    return [tuple(normalize_value(v) for v in row) for row in rows]


def compare_results(gold: Rows, predicted: Rows) -> Tuple[bool, bool]:
    """Return ``(exact, relaxed)`` for two already-normalized result sets."""
    exact = Counter(gold) == Counter(predicted)
    if exact:
        return True, True
    if len(predicted) > len(gold):
        # "For each store" answered with every store, the 498 that have no
        # rentals listed at 0: a legitimate reading of "each", not a wrong
        # answer. Extra rows carrying a zero or empty value that the reference
        # does not have are set aside before the relaxed comparison (never the
        # exact one); everything left must still match.
        expected = set(gold)
        predicted = [r for r in predicted if r in expected or not _zero_filled(r)]
    if len(gold) != len(predicted):
        return False, False
    if not gold:
        return False, True  # both empty; only the column shape differed

    gold_width = len(gold[0])
    pred_width = len(predicted[0]) if predicted else 0
    if pred_width < gold_width:
        return False, False

    # Candidate generated columns for each reference column: same multiset of
    # values. Cheap to compute and prunes almost every combination.
    def column(rows: Rows, i: int) -> Counter:
        return Counter(row[i] for row in rows)

    pred_columns = [column(predicted, j) for j in range(pred_width)]
    candidates: List[List[int]] = []
    for i in range(gold_width):
        values = column(gold, i)
        matches = [j for j, c in enumerate(pred_columns) if c == values]
        if not matches:
            return False, False
        candidates.append(matches)

    target = Counter(gold)
    for tried, projection in enumerate(itertools.product(*candidates)):
        if tried >= _MAX_PROJECTIONS:
            break
        if len(set(projection)) != len(projection):
            continue
        projected = Counter(tuple(row[j] for j in projection) for row in predicted)
        if projected == target:
            return False, True
    return False, False


def _zero_filled(row: Tuple[Any, ...]) -> bool:
    """A group reported only to say it has nothing: some value is 0 or empty."""
    return any(v is None or (isinstance(v, float) and v == 0.0) for v in row)


def has_swallowed_comment(sql: str) -> bool:
    """True when a one-line statement carries a ``--`` comment.

    On a single line a line comment runs to the end of the statement, so
    everything after it is silently dropped -- the defect ``qa.json`` exports
    had before ``tools/export_qa.py`` learned to strip comments first. Executing
    such a statement "works" and scores against the wrong reference.
    """
    if "\n" in sql:
        return False
    quote: Optional[str] = None
    for i, ch in enumerate(sql):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif sql.startswith("--", i):
            return True
    return False


# ----------------------------------------------------------------------
# Running one question
# ----------------------------------------------------------------------


@dataclass
class Outcome:
    index: int
    question: str
    difficulty: Optional[str] = None
    skills: List[str] = field(default_factory=list)
    exact: bool = False
    relaxed: bool = False
    #: Some successful statement in the turn matched, though not the last --
    #: an agent that answers and then runs a sanity check (a date range, a row
    #: count) is scored on the check. Diagnostic only; not in the headline.
    any_relaxed: bool = False
    generated_sql: Optional[str] = None
    attempts: int = 0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    retrieval_strategy: Optional[str] = None
    mentions_suggest_joins: bool = False
    #: The agent stopped to ask which of several readings was meant. With
    #: ``--answer-clarifications`` the first option was sent back and the
    #: score is for the follow-up turn; without it the question scores 0.
    clarified: bool = False
    #: What the agent said, abbreviated -- the only clue when it ran no SQL
    #: (asked a clarifying question, refused, or ran out of iterations).
    answer: Optional[str] = None
    seconds: float = 0.0
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class Workspace:
    """Where a dataset's questions are asked: the workspace and its database."""

    tenant: str
    data_source: str = ""


def _ask(
    args: argparse.Namespace,
    workspace: Workspace,
    question: str,
    conversation_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    import httpx

    conversation_id = conversation_id or f"eval-{uuid.uuid4()}"
    response = httpx.post(
        f"{args.server.rstrip('/')}/api/vanna/v2/chat_poll",
        json={
            "message": question,
            "conversation_id": conversation_id,
            "metadata": (
                {"data_source_id": workspace.data_source} if workspace.data_source else {}
            ),
        },
        headers={
            "Authorization": f"Bearer {args.token}",
            "x-tenant-id": workspace.tenant,
        },
        timeout=args.timeout,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("conversation_id") or conversation_id, body


def clarification_options(body: Any) -> List[str]:
    """The choices offered by a ``request_clarification`` card, in order.

    The card's buttons each carry a complete question as their ``action`` --
    what the UI sends back when one is clicked. Searched for rather than read
    from a fixed path, so a change in how components nest does not silently
    turn every clarification into "agent ran no SQL".
    """
    if isinstance(body, dict):
        actions = body.get("actions")
        if isinstance(actions, list) and actions and all(
            isinstance(a, dict) and isinstance(a.get("action"), str) for a in actions
        ):
            return [a["action"] for a in actions]
        for value in body.values():
            found = clarification_options(value)
            if found:
                return found
    elif isinstance(body, list):
        for value in body:
            found = clarification_options(value)
            if found:
                return found
    return []


def _answer_text(body: Dict[str, Any], limit: int = 600) -> str:
    """The text of every simple component in a chat_poll response, joined."""
    parts: List[str] = []
    for chunk in body.get("chunks") or []:
        simple = chunk.get("simple") or {}
        for key in ("text", "content", "message"):
            value = simple.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
    text = " | ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _generations(app_db: Any, tenant: str, conversation_id: str) -> List[Dict[str, Any]]:
    with app_db.cursor() as cursor:
        cursor.execute(
            """SELECT sql, status, error, prompt_tokens, completion_tokens,
                      retrieval_strategy, retrieved_table_names
                 FROM vanna_app.generations
                WHERE tenant_id = %s AND conversation_id = %s
                ORDER BY created_at""",
            (tenant, conversation_id),
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


@dataclass
class Generation:
    """One question put to the agent: what it said and every statement it ran."""

    conversation_id: str
    body: Dict[str, Any]
    clarified: bool = False
    notes: List[str] = field(default_factory=list)
    #: Rows of ``vanna_app.generations`` for the conversation, oldest first.
    rows: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["status"] in ("valid", "empty")]

    @property
    def final(self) -> Optional[Dict[str, Any]]:
        """The statement the turn is scored on: the last one that ran."""
        succeeded = self.succeeded
        return succeeded[-1] if succeeded else None

    @property
    def failure(self) -> Optional[str]:
        if self.final is not None:
            return None
        return "no successful SQL" if self.rows else "agent ran no SQL"


def generate(
    args: argparse.Namespace, workspace: Workspace, question: str, app_db: Any
) -> Generation:
    """Ask *question* as the UI would and read back the SQL the agent ran."""
    conversation_id, body = _ask(args, workspace, question)
    options = clarification_options(body)
    notes: List[str] = []
    if options and args.answer_clarifications:
        # What a user does: click the first reading offered. Same thread,
        # so the follow-up turn sees the original question as history.
        notes.append(f"clarification answered with: {options[0]}")
        conversation_id, body = _ask(args, workspace, options[0], conversation_id)
    rows = _generations(app_db, workspace.tenant, conversation_id)
    return Generation(conversation_id, body, bool(options), notes, rows)


def _execute(engine: Any, sql: str) -> Rows:
    from sqlalchemy import text

    with engine.connect() as connection:
        result = connection.execute(text(sql))
        return normalize_rows(result.fetchall())


def run_one(
    args: argparse.Namespace,
    workspace: Workspace,
    index: int,
    record: Dict[str, Any],
    app_db: Any,
    engine: Any,
) -> Outcome:
    question = record["question"]
    outcome = Outcome(
        index=index,
        question=question,
        difficulty=record.get("difficulty"),
        skills=list(record.get("skills") or []),
    )
    started = time.monotonic()
    try:
        generation = generate(args, workspace, question, app_db)
        outcome.clarified = generation.clarified
        outcome.notes.extend(generation.notes)
        outcome.mentions_suggest_joins = "suggest_joins" in json.dumps(generation.body)
        outcome.answer = _answer_text(generation.body)

        rows = generation.rows
        outcome.attempts = len(rows)
        succeeded = generation.succeeded
        final = generation.final
        if final is None:
            outcome.error = generation.failure
            return outcome
        outcome.generated_sql = final["sql"]
        # Running totals per turn, so the last row carries the turn's cost.
        outcome.prompt_tokens = rows[-1].get("prompt_tokens")
        outcome.completion_tokens = rows[-1].get("completion_tokens")
        outcome.retrieval_strategy = final.get("retrieval_strategy")

        gold = _execute(engine, record["query"])
        predicted = _execute(engine, final["sql"])
        outcome.exact, outcome.relaxed = compare_results(gold, predicted)
        outcome.any_relaxed = outcome.relaxed
        for earlier in reversed(succeeded[:-1]):
            if outcome.any_relaxed:
                break
            try:
                outcome.any_relaxed = compare_results(gold, _execute(engine, earlier["sql"]))[1]
            except Exception:
                continue
    except Exception as exc:  # one bad question must not end the run
        outcome.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


def summarize(label: str, outcomes: List[Outcome]) -> Dict[str, Any]:
    """Headline rates plus breakdowns by difficulty and by skill.

    The breakdowns are what make a score actionable: "73%" says little,
    "fan_trap 1/4, anti_join 5/5" says where to look.
    """
    n = len(outcomes) or 1

    def breakdown(keys_of: Any) -> Dict[str, Dict[str, Any]]:
        groups: Dict[str, List[Outcome]] = {}
        for outcome in outcomes:
            for key in keys_of(outcome):
                groups.setdefault(key, []).append(outcome)
        return {
            key: {
                "n": len(group),
                "relaxed": sum(o.relaxed for o in group),
                "rate": round(sum(o.relaxed for o in group) / len(group), 3),
            }
            for key, group in sorted(groups.items())
        }

    return {
        "label": label,
        "questions": len(outcomes),
        "exact": round(sum(o.exact for o in outcomes) / n, 3),
        "relaxed": round(sum(o.relaxed for o in outcomes) / n, 3),
        "relaxed_any_statement": round(sum(o.any_relaxed for o in outcomes) / n, 3),
        "clarification_rate": round(sum(o.clarified for o in outcomes) / n, 3),
        "errors": sum(1 for o in outcomes if o.error),
        "mean_sql_attempts": round(sum(o.attempts for o in outcomes) / n, 2),
        "mean_prompt_tokens": _mean(o.prompt_tokens for o in outcomes),
        "suggest_joins_rate": round(sum(o.mentions_suggest_joins for o in outcomes) / n, 3),
        "strategies": dict(Counter(o.retrieval_strategy or "?" for o in outcomes)),
        "by_difficulty": breakdown(lambda o: [o.difficulty or "unrated"]),
        "by_skill": breakdown(lambda o: o.skills),
    }


def score_dataset(
    args: argparse.Namespace,
    workspace: Workspace,
    records: List[Dict[str, Any]],
    engine: Any,
    app_db: Any,
    label: str,
) -> Tuple[Dict[str, Any], List[Outcome]]:
    """Ask every question, score it, write the report. Returns (summary, outcomes)."""
    outcomes: List[Outcome] = []
    for index, record in enumerate(records):
        outcome = run_one(args, workspace, index, record, app_db, engine)
        outcomes.append(outcome)
        mark = "EXACT" if outcome.exact else "ok~" if outcome.relaxed else "FAIL"
        print(f"[{index + 1:>3}/{len(records)}] {mark:<5} {outcome.seconds:>6.1f}s  "
              f"{question_preview(outcome.question)}"
              + (f"  ({outcome.error})" if outcome.error else ""), flush=True)
    summary = summarize(label, outcomes)
    _write_report(args, label, summary, outcomes)
    return summary, outcomes


def _write_report(
    args: argparse.Namespace, label: str, summary: Dict[str, Any], outcomes: List[Outcome]
) -> Path:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"sql_accuracy_{label}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    report.write_text(
        json.dumps({"summary": summary, "outcomes": [asdict(o) for o in outcomes]},
                   indent=2, default=str),
        encoding="utf-8",
    )
    print("\n" + json.dumps(summary, indent=2))
    print(f"Report: {report}", flush=True)
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

DATASETS_DIR = Path(__file__).resolve().parent / "datasets" / "sql_accuracy"


def _parse(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--server", default="http://localhost:3000")
    p.add_argument("--token", default="", help="API token (Bearer) for an analyst")
    p.add_argument("--tenant", default="", help="Workspace id (x-tenant-id)")
    p.add_argument("--data-source", default="", help="Data source id; empty = default")
    p.add_argument("--app-db", default="", help="Control-plane Postgres URL")
    p.add_argument("--target-db", default="", help="SQLAlchemy URL of the warehouse")
    p.add_argument(
        "--check-gold",
        action="store_true",
        help="Only execute the reference SQL and report errors and empty results. "
        "Needs nothing but the warehouse; run it on a new dataset before trusting it.",
    )
    p.add_argument("--dataset", default=str(Path(__file__).resolve().parents[2] / "qa.json"))
    p.add_argument(
        "--suite",
        default="",
        help="Run every dataset in a suite manifest (see datasets/sql_accuracy/suite.json) "
        "instead of one --dataset. Needs --target-host.",
    )
    p.add_argument(
        "--target-host",
        default="",
        help="Suite mode: server URL without a database, e.g. "
        "postgresql://user:pass@localhost:5432 -- each dataset's database is appended.",
    )
    p.add_argument(
        "--workspace",
        action="append",
        default=[],
        metavar="DATABASE=TENANT[:DATA_SOURCE]",
        help="Suite mode: which workspace answers a database's questions. Repeat per "
        "database; datasets whose database has none are skipped when scoring the agent.",
    )
    p.add_argument(
        "--answer-clarifications",
        action="store_true",
        help="When the agent asks which reading was meant, reply with its first option "
        "and score the follow-up -- separates SQL ability from the decision to ask.",
    )
    p.add_argument("--label", default="run", help="Names the report, e.g. full / search")
    p.add_argument("--limit", type=int, default=0, help="Only the first N questions")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    return p.parse_args(argv)


def parse_workspaces(values: Sequence[str]) -> Dict[str, Workspace]:
    """``chinook=demo:postgresql://db/chinook`` -> {"chinook": Workspace(...)}.

    The data source id may itself contain colons (it is usually a URL), so only
    the first colon after the tenant separates the two.
    """
    mapping: Dict[str, Workspace] = {}
    for value in values:
        database, sep, rest = value.partition("=")
        if not sep or not database or not rest:
            raise ValueError(f"--workspace {value!r} is not DATABASE=TENANT[:DATA_SOURCE]")
        tenant, _, data_source = rest.partition(":")
        mapping[database.strip()] = Workspace(tenant=tenant.strip(), data_source=data_source.strip())
    return mapping


def load_suite(path: Path) -> List[Dict[str, Any]]:
    """Suite entries, each {"dataset": file, "database": name}, with paths resolved."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        entry["path"] = (path.parent / entry["dataset"]).resolve()
    return entries


def _load_records(path: Path, limit: int) -> List[Dict[str, Any]]:
    records = json.loads(path.read_text(encoding="utf-8"))
    return records[:limit] if limit else records


def _refuse_broken(records: List[Dict[str, Any]], name: str) -> bool:
    broken = [i for i, r in enumerate(records) if has_swallowed_comment(r["query"])]
    if broken:
        print(
            f"{name}: {len(broken)} of {len(records)} reference queries are one line with "
            "a '--' comment, which comments out the rest of the statement. Re-export the "
            "dataset with backend/tools/export_qa.py (it now strips comments first). "
            f"Affected indexes: {broken[:10]}{'...' if len(broken) > 10 else ''}",
            file=sys.stderr,
        )
    return bool(broken)


def _app_db(args: argparse.Namespace) -> Any:
    import psycopg2

    connection = psycopg2.connect(args.app_db)
    connection.autocommit = True
    return connection


def _missing_agent_args(args: argparse.Namespace, *, needs_tenant: bool) -> bool:
    fields = ["token", "app_db"] + (["tenant"] if needs_tenant else [])
    missing = [f for f in fields if not getattr(args, f)]
    if missing:
        print(
            "Scoring the agent needs " + ", ".join(f"--{m.replace('_', '-')}" for m in missing)
            + " (or pass --check-gold to validate the dataset only).",
            file=sys.stderr,
        )
    return bool(missing)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse(argv)
    if args.suite:
        return run_suite(args)

    if not args.target_db:
        print("--target-db is required (or use --suite with --target-host).", file=sys.stderr)
        return 2
    records = _load_records(Path(args.dataset), args.limit)
    if _refuse_broken(records, Path(args.dataset).name):
        return 2

    from sqlalchemy import create_engine

    engine = create_engine(args.target_db)
    if args.check_gold:
        return check_gold(records, engine)
    if _missing_agent_args(args, needs_tenant=True):
        return 2

    workspace = Workspace(tenant=args.tenant, data_source=args.data_source)
    score_dataset(args, workspace, records, engine, _app_db(args), args.label)
    return 0


def run_suite(args: argparse.Namespace) -> int:
    """Every dataset in the manifest: gold check, or agent scoring per workspace."""
    from sqlalchemy import create_engine

    if not args.target_host:
        print("--suite needs --target-host.", file=sys.stderr)
        return 2
    try:
        workspaces = parse_workspaces(args.workspace)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    entries = load_suite(Path(args.suite))
    host = args.target_host.rstrip("/")

    if not args.check_gold and _missing_agent_args(args, needs_tenant=False):
        return 2
    app_db = None if args.check_gold else _app_db(args)

    status = 0
    all_outcomes: List[Outcome] = []
    for entry in entries:
        name = entry["path"].stem
        records = _load_records(entry["path"], args.limit)
        print(f"\n===== {name} ({entry['database']}, {len(records)} questions)", flush=True)
        if _refuse_broken(records, name):
            status = 2
            continue
        engine = create_engine(f"{host}/{entry['database']}")
        if args.check_gold:
            status = max(status, check_gold(records, engine))
            continue
        workspace = workspaces.get(entry["database"])
        if workspace is None:
            print(f"skipped: no --workspace for database {entry['database']!r}")
            continue
        _, outcomes = score_dataset(
            args, workspace, records, engine, app_db, f"{args.label}_{name}"
        )
        all_outcomes.extend(outcomes)

    if all_outcomes:
        print("\n===== suite total")
        _write_report(args, f"{args.label}_suite", summarize(f"{args.label}_suite", all_outcomes),
                      all_outcomes)
    return status


def check_gold(records: List[Dict[str, Any]], engine: Any) -> int:
    """Execute every reference query; exit 1 if any fails or returns nothing.

    An empty result is almost always a wrong literal ('Brazil' vs 'brazil')
    rather than a true answer, and a question whose reference answer is empty
    scores any agent that also finds nothing as correct.
    """
    bad = 0
    for index, record in enumerate(records):
        try:
            rows = _execute(engine, record["query"])
            status = f"{len(rows)} row(s)" if rows else "EMPTY"
            bad += 0 if rows else 1
        except Exception as exc:
            status = f"ERROR {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            bad += 1
        print(f"[{index + 1:>3}/{len(records)}] {status:<12} "
              f"{question_preview(record['question'])}", flush=True)
    print(f"\n{len(records) - bad} of {len(records)} reference queries usable.")
    return 1 if bad else 0


def question_preview(question: str, width: int = 70) -> str:
    return question if len(question) <= width else question[: width - 1] + "…"


def _mean(values: Any) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 1) if present else None


if __name__ == "__main__":
    sys.exit(main())
