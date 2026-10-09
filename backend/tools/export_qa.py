#!/usr/bin/env python3
"""Export question-and-answer history as qa.json.

    python tools/export_qa.py --tenant chinook --password ... --out qa.json
    python tools/export_qa.py --tenant all --password ...   # every workspace

Each record is one exchange:

    {"question": "...", "answer": "...", "query": "...", "database": "..."}

Assembled from two places, because no single endpoint holds all four. The
*generation* store has the question and the SQL that reached the database; the
*conversation* store has the prose the user actually read. They are joined on
``conversation_id``, falling back to the question text when a generation predates
the thread it belongs to.

The join is the whole job, and it is not one-to-one in either direction:

* One turn can record several generations -- a repair retry executes twice, and a
  question needing two lookups runs two statements. ``--all-queries`` keeps each as
  its own record; by default the last successful statement wins, since that is the
  one the answer was written from.
* A conversation can hold several exchanges, so messages are walked in order and
  each user message is paired with the assistant reply that follows it.
* A generation with no question came from ``run-sql`` rather than from a chat turn
  -- the question is captured by a lifecycle hook the direct-SQL path never runs.
  Those are skipped: a record with an empty question is not a Q&A pair.

Signed in over HTTP with the workspace header set, exactly as the seeders do. An
account here belongs to several workspaces and the session default is not
necessarily the one you mean, so ``--tenant`` decides -- and ``all`` walks every
membership, which is how one file ends up spanning eight different databases. The
``database`` field is read per workspace from the app rather than assumed, so a
record always names the warehouse its SQL was actually run against.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Dict, List, Optional, Tuple


def _one_line(sql: str) -> str:
    """Collapse *sql* onto one line without changing what it means.

    ``--`` comments run to the end of their line, so collapsing whitespace first
    turns ``SELECT a, -- note`` + ``b FROM t`` into a statement that is all
    comment after ``a,``. Line comments are dropped first; quoted text is copied
    verbatim so a ``'--'`` literal survives. Block comments are self-delimiting
    and are left alone.
    """
    out: List[str] = []
    i, n = 0, len(sql)
    quote: Optional[str] = None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        else:
            out.append(ch)
        i += 1
    return " ".join("".join(out).split())


def _decode(raw: str) -> Any:
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"detail": raw[:300]}


class Client:
    """A session that holds cookies, echoes CSRF, and names the workspace."""

    def __init__(self, base: str, tenant: Optional[str]) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "vanna_csrf":
                return urllib.parse.unquote(cookie.value or "")
        return ""

    def call(self, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token := self._csrf():
            headers["X-CSRF-Token"] = token
        if self.tenant:
            headers["X-Tenant-Id"] = self.tenant

        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, _decode(response.read().decode(errors="replace"))
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read().decode(errors="replace"))

    def sign_in(self, email: str, password: str) -> None:
        self.call("GET", "/")  # issues the CSRF cookie the login POST needs
        body: Dict[str, Any] = {"email": email, "password": password}
        if self.tenant:
            body["tenant"] = self.tenant
        status, payload = self.call("POST", "/api/vanna/v2/auth/login", body)
        if status != 200:
            raise SystemExit(f"sign-in failed ({status}): {payload}")


# ----------------------------------------------------------------------
# Gathering
# ----------------------------------------------------------------------


def database_name(client: Client) -> str:
    """What this workspace is querying, as the app itself reports it."""
    status, payload = client.call("GET", "/api/vanna/v2/schema")
    if status != 200 or not isinstance(payload, dict):
        return ""
    return str(payload.get("data_source") or payload.get("dialect") or "")


def generations(client: Client, limit: int) -> List[Dict[str, Any]]:
    status, payload = client.call("GET", f"/api/vanna/v2/history?limit={limit}")
    if status != 200:
        raise SystemExit(f"history unavailable ({status}): {payload}")
    return payload.get("history") or []


def exchanges(client: Client, limit: int) -> List[Tuple[str, str, str]]:
    """Every (conversation_id, question, answer) the workspace has stored.

    Walked in message order rather than by taking the first and last: a thread can
    hold several exchanges, and pairing the opening question with the closing answer
    would attribute the last reply to the first question.
    """
    status, payload = client.call("GET", f"/api/vanna/v2/conversations?limit={limit}")
    if status != 200:
        return []

    out: List[Tuple[str, str, str]] = []
    for summary in payload.get("conversations") or []:
        thread_id = summary.get("id")
        if not thread_id:
            continue
        code, thread = client.call(
            "GET", f"/api/vanna/v2/conversations/{urllib.parse.quote(thread_id)}"
        )
        if code != 200 or not isinstance(thread, dict):
            continue

        pending: Optional[str] = None
        for message in thread.get("messages") or []:
            role, content = message.get("role"), (message.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                pending = content
            elif role == "assistant" and pending:
                out.append((thread_id, pending, content))
                pending = None
    return out


def build_one(
    client: Client, *, limit: int, all_queries: bool
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    database = database_name(client)
    rows = generations(client, limit)
    pairs = exchanges(client, limit)

    # Index the SQL two ways. conversation_id is exact; question text is the
    # fallback for a generation recorded before its thread was persisted.
    by_thread: Dict[str, List[Dict[str, Any]]] = {}
    by_question: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if not (row.get("sql") or "").strip():
            continue
        if thread := row.get("conversation_id"):
            by_thread.setdefault(thread, []).append(row)
        if question := (row.get("question") or "").strip():
            by_question.setdefault(question, []).append(row)

    def sql_for(thread_id: str, question: str) -> List[str]:
        """The statements behind one exchange, oldest first.

        Successful ones only when there are any: a repair retry leaves the failed
        attempt in the store, and exporting the statement that errored as "the
        query" for an answer that worked would be a lie in a training file.
        """
        candidates = by_thread.get(thread_id) or by_question.get(question) or []
        good = [r for r in candidates if r.get("status") == "valid"] or candidates
        statements = [_one_line(r.get("sql") or "") for r in reversed(good)]
        return statements if all_queries else statements[-1:]

    records: List[Dict[str, str]] = []
    counts = {"exchanges": len(pairs), "without_sql": 0, "generations": len(rows)}

    for thread_id, question, answer in pairs:
        statements = sql_for(thread_id, question)
        if not statements:
            counts["without_sql"] += 1
            continue
        for statement in statements:
            records.append(
                {
                    "question": question,
                    "answer": answer,
                    "query": statement,
                    "database": database,
                }
            )

    counts["records"] = len(records)
    return records, counts


def deduplicate(records: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], int]:
    """Collapse repeats of the same question over the same SQL, per database.

    Keyed on (database, question, query) rather than on the whole record. The same
    question asked twice produces two differently worded answers over identical SQL,
    so deduplicating on all four fields keeps every one of them -- and a question the
    test suite asks on every run then dominates the file. Nine of twenty-nine records
    in the first export were one starter question asked nine times.

    The database is part of the key because "What is our total revenue?" is a
    different question against the music store than against the hotel, and both
    belong in the file.
    """
    seen = set()
    unique = []
    for record in records:
        key = (record["database"], record["question"], record["query"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique, len(records) - len(unique)


def workspaces(client: Client) -> List[str]:
    """Every workspace this account belongs to.

    Read from the account rather than hard-coded, so a deployment with different
    workspaces exports its own rather than a list that was true here once.
    """
    status, payload = client.call("GET", "/api/vanna/v2/me")
    if status != 200 or not isinstance(payload, dict):
        return []
    return list(payload.get("memberships") or [])


def balance(records: List[Dict[str, str]], cap: int) -> List[Dict[str, str]]:
    """Take ``cap`` records, spread as evenly as the data allows across databases.

    Truncating the list instead would hand back whichever workspace happened to be
    exported first. Chinook had 46 records to healthcare's none, so a plain
    ``[:100]`` would have been half one music store -- technically a hundred
    records, and useless as a cross-database corpus.

    Round-robin rather than a quota per database, because the databases have very
    different amounts of history: a fixed share leaves the cap unfilled when a
    workspace cannot meet it, whereas this simply keeps dealing from whichever
    piles still have cards.
    """
    if cap <= 0 or len(records) <= cap:
        return records

    piles: Dict[str, List[Dict[str, str]]] = {}
    for record in records:
        piles.setdefault(record["database"], []).append(record)

    out: List[Dict[str, str]] = []
    while len(out) < cap:
        dealt = False
        for pile in piles.values():
            if not pile:
                continue
            out.append(pile.pop(0))
            dealt = True
            if len(out) == cap:
                break
        if not dealt:  # every pile empty; cannot reach the cap
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--tenant",
        default="chinook",
        help="a workspace, a comma-separated list of them, or 'all' for every "
        "workspace this account belongs to",
    )
    parser.add_argument("--out", default="qa.json")
    parser.add_argument(
        "--limit", type=int, default=200, help="how much history to read per workspace"
    )
    parser.add_argument(
        "--all-queries",
        action="store_true",
        help="one record per statement, not just the last successful one",
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="do not collapse repeats of the same question over the same SQL",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        dest="cap",
        help="cap the file at N records, spread evenly across the databases",
    )
    args = parser.parse_args()

    # Sign in once without a workspace header to discover the memberships, then
    # talk to each workspace with its own client. One session, many workspaces --
    # which is exactly how the browser does it.
    scout = Client(args.url, None)
    scout.sign_in(args.email, args.password)

    if args.tenant == "all":
        tenants = workspaces(scout)
        if not tenants:
            return _fail("this account belongs to no workspaces")
    else:
        tenants = [t.strip() for t in args.tenant.split(",") if t.strip()]

    records: List[Dict[str, str]] = []
    per_database: Dict[str, int] = {}
    skipped_no_sql = 0

    for tenant in tenants:
        client = Client(args.url, tenant)
        client.sign_in(args.email, args.password)
        try:
            found, counts = build_one(
                client, limit=args.limit, all_queries=args.all_queries
            )
        except SystemExit as exc:  # one unreadable workspace is not fatal
            print(f"  {tenant:<12} unavailable: {exc}")
            continue

        skipped_no_sql += counts["without_sql"]
        records.extend(found)
        database = found[0]["database"] if found else "?"
        per_database[tenant] = len(found)
        print(f"  {tenant:<12} {len(found):>4} records  ({database})")

    dropped = 0
    if not args.keep_duplicates:
        records, dropped = deduplicate(records)

    available = len(records)
    records = balance(records, args.cap)
    if args.cap and available < args.cap:
        print(f"\nonly {available} records available; --max {args.cap} not reached")

    if not records:
        return _fail(
            "nothing to export: no chat exchanges with SQL behind them.\n"
            "Ask some questions first: tools/ask_demo_questions.py --tenant all"
        )

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(f"\nwrote {args.out}")
    print(f"  records            : {len(records)}")
    print(f"  databases          : {len({r['database'] for r in records})}")
    print(f"  distinct questions : {len({(r['database'], r['question']) for r in records})}")
    for database in sorted({r["database"] for r in records}):
        share = sum(1 for r in records if r["database"] == database)
        print(f"    {share:>4}  {database}")
    if dropped:
        print(f"  duplicates dropped : {dropped}")
    if skipped_no_sql:
        print(
            f"  skipped, no SQL    : {skipped_no_sql} "
            "(answered without querying, or the thread outlived its history page)"
        )
    return 0


def _fail(message: str) -> int:
    print(message)
    return 1


if __name__ == "__main__":
    sys.exit(main())
