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

Multi-hop sets (3+ joins, fan traps) for three warehouses live in
``datasets/sql_accuracy/``; Northwind's declares no foreign keys at all, so it
also measures inferred relationships. Validate a dataset against its warehouse
first -- this runs only the reference SQL, no agent, no model calls:

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
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Projections tried before relaxed matching gives up. Real results have a
#: handful of columns; this only bounds a pathological wide result.
_MAX_PROJECTIONS = 2_000

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
        return round(float(value), 2)
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
    exact: bool = False
    relaxed: bool = False
    generated_sql: Optional[str] = None
    attempts: int = 0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    retrieval_strategy: Optional[str] = None
    mentions_suggest_joins: bool = False
    seconds: float = 0.0
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _ask(args: argparse.Namespace, question: str) -> Tuple[str, Dict[str, Any]]:
    import httpx

    conversation_id = f"eval-{uuid.uuid4()}"
    response = httpx.post(
        f"{args.server.rstrip('/')}/api/vanna/v2/chat_poll",
        json={
            "message": question,
            "conversation_id": conversation_id,
            "metadata": {"data_source_id": args.data_source} if args.data_source else {},
        },
        headers={
            "Authorization": f"Bearer {args.token}",
            "x-tenant-id": args.tenant,
        },
        timeout=args.timeout,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("conversation_id") or conversation_id, body


def _generations(app_db: Any, tenant: str, conversation_id: str) -> List[Dict[str, Any]]:
    with app_db.cursor() as cursor:
        cursor.execute(
            """SELECT sql, status, prompt_tokens, completion_tokens, retrieval_strategy
                 FROM vanna_app.generations
                WHERE tenant_id = %s AND conversation_id = %s
                ORDER BY created_at""",
            (tenant, conversation_id),
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def _execute(engine: Any, sql: str) -> Rows:
    from sqlalchemy import text

    with engine.connect() as connection:
        result = connection.execute(text(sql))
        return normalize_rows(result.fetchall())


def run_one(
    args: argparse.Namespace, index: int, record: Dict[str, Any], app_db: Any, engine: Any
) -> Outcome:
    question = record["question"]
    outcome = Outcome(index=index, question=question)
    started = time.monotonic()
    try:
        conversation_id, body = _ask(args, question)
        outcome.mentions_suggest_joins = "suggest_joins" in json.dumps(body)

        rows = _generations(app_db, args.tenant, conversation_id)
        outcome.attempts = len(rows)
        succeeded = [r for r in rows if r["status"] in ("valid", "empty")]
        if not succeeded:
            outcome.error = "no successful SQL" if rows else "agent ran no SQL"
            return outcome
        final = succeeded[-1]
        outcome.generated_sql = final["sql"]
        # Running totals per turn, so the last row carries the turn's cost.
        outcome.prompt_tokens = rows[-1].get("prompt_tokens")
        outcome.completion_tokens = rows[-1].get("completion_tokens")
        outcome.retrieval_strategy = final.get("retrieval_strategy")

        gold = _execute(engine, record["query"])
        predicted = _execute(engine, final["sql"])
        outcome.exact, outcome.relaxed = compare_results(gold, predicted)
    except Exception as exc:  # one bad question must not end the run
        outcome.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _parse(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--server", default="http://localhost:3000")
    p.add_argument("--token", default="", help="API token (Bearer) for an analyst")
    p.add_argument("--tenant", default="", help="Workspace id (x-tenant-id)")
    p.add_argument("--data-source", default="", help="Data source id; empty = default")
    p.add_argument("--app-db", default="", help="Control-plane Postgres URL")
    p.add_argument("--target-db", required=True, help="SQLAlchemy URL of the warehouse")
    p.add_argument(
        "--check-gold",
        action="store_true",
        help="Only execute the reference SQL and report errors and empty results. "
        "Needs nothing but --target-db; run it on a new dataset before trusting it.",
    )
    p.add_argument("--dataset", default=str(Path(__file__).resolve().parents[2] / "qa.json"))
    p.add_argument("--label", default="run", help="Names the report, e.g. full / search")
    p.add_argument("--limit", type=int, default=0, help="Only the first N questions")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse(argv)
    records = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    if args.limit:
        records = records[: args.limit]

    broken = [i for i, r in enumerate(records) if has_swallowed_comment(r["query"])]
    if broken:
        print(
            f"{len(broken)} of {len(records)} reference queries are one line with a "
            "'--' comment, which comments out the rest of the statement. Re-export "
            "the dataset with backend/tools/export_qa.py (it now strips comments "
            f"first). Affected indexes: {broken[:10]}{'...' if len(broken) > 10 else ''}",
            file=sys.stderr,
        )
        return 2

    from sqlalchemy import create_engine

    engine = create_engine(args.target_db)
    if args.check_gold:
        return check_gold(records, engine)

    missing = [f for f in ("token", "tenant", "app_db") if not getattr(args, f)]
    if missing:
        print(
            "Scoring the agent needs " + ", ".join(f"--{m.replace('_', '-')}" for m in missing)
            + " (or pass --check-gold to validate the dataset only).",
            file=sys.stderr,
        )
        return 2

    import psycopg2

    app_db = psycopg2.connect(args.app_db)
    app_db.autocommit = True

    outcomes: List[Outcome] = []
    for index, record in enumerate(records):
        outcome = run_one(args, index, record, app_db, engine)
        outcomes.append(outcome)
        mark = "EXACT" if outcome.exact else "ok~" if outcome.relaxed else "FAIL"
        print(f"[{index + 1:>3}/{len(records)}] {mark:<5} {outcome.seconds:>6.1f}s  "
              f"{question_preview(outcome.question)}"
              + (f"  ({outcome.error})" if outcome.error else ""))

    n = len(outcomes) or 1
    summary = {
        "label": args.label,
        "questions": len(outcomes),
        "exact": round(sum(o.exact for o in outcomes) / n, 3),
        "relaxed": round(sum(o.relaxed for o in outcomes) / n, 3),
        "errors": sum(1 for o in outcomes if o.error),
        "mean_sql_attempts": round(sum(o.attempts for o in outcomes) / n, 2),
        "mean_prompt_tokens": _mean(o.prompt_tokens for o in outcomes),
        "suggest_joins_rate": round(sum(o.mentions_suggest_joins for o in outcomes) / n, 3),
        "strategies": dict(Counter(o.retrieval_strategy or "?" for o in outcomes)),
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"sql_accuracy_{args.label}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    report.write_text(
        json.dumps({"summary": summary, "outcomes": [asdict(o) for o in outcomes]},
                   indent=2, default=str),
        encoding="utf-8",
    )
    print("\n" + json.dumps(summary, indent=2))
    print(f"Report: {report}")
    return 0


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
              f"{question_preview(record['question'])}")
    print(f"\n{len(records) - bad} of {len(records)} reference queries usable.")
    return 1 if bad else 0


def question_preview(question: str, width: int = 70) -> str:
    return question if len(question) <= width else question[: width - 1] + "…"


def _mean(values: Any) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 1) if present else None


if __name__ == "__main__":
    sys.exit(main())
