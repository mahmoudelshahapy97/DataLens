#!/usr/bin/env python3
"""Ask real questions through the chat, so the history screen has real Q&A in it.

``seed_demo_data.py`` fills the history table by running SQL, and those rows are
genuine -- but they have no *question*. That is not an oversight in the seeder, it
is how the application works: the question is captured by a lifecycle hook on the
agent's ``before_message``, so only a chat turn has one. Anything that posts SQL
directly, including the app's own "Run SQL" button, records the statement and
leaves the question blank. The history screen shows those rows as
"(no question recorded)".

So the only way to get question-and-answer pairs is to ask, the way a person does:
type into the chat and let the agent write the SQL.

    python tools/ask_demo_questions.py --tenant chinook --password ...
    python tools/ask_demo_questions.py --tenant all --password ...

One bank of questions per workspace, because each is bound to a different database
and a question about invoices means nothing to the world atlas. ``--tenant all``
walks every bank in turn, switching workspace between them.

Each question costs an LLM call and thirty seconds or more, so a full run over every
workspace takes an hour or two. Progress is printed per question, with the SQL the
agent produced and the row count it got, because an agent that answers confidently
in prose while executing nothing looks identical from the outside until you check.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the message is the point
    sys.exit("playwright is not installed: pip install playwright && playwright install chromium")


# ----------------------------------------------------------------------
# The questions
# ----------------------------------------------------------------------
#
# Phrased the way a person types them rather than as SQL with the keywords removed,
# and deliberately mixed in shape: ranked lists, single-sentence answers, filters,
# time comparisons, ratios, negations and self-joins. A corpus of nothing but
# "top N by revenue" exercises one code path and tells you little about the rest.
#
# Several questions need a join the question does not mention. That is the point --
# it is what the semantic layer and the retrieved examples are for.

BANKS: Dict[str, List[str]] = {
    # -- music store -------------------------------------------------
    "chinook": [
        "What is our total revenue?",
        "Who are our top 10 customers by lifetime spend?",
        "How does revenue break down by country?",
        "Show me the monthly revenue trend",
        "Which genres sell the most tracks?",
        "Who are our best selling artists?",
        "Which albums earned the most money?",
        "How is each sales rep performing?",
        "What are the 15 highest earning tracks?",
        "Which cities generate the most revenue?",
        "How many customers have never bought anything?",
        "What is the revenue per year?",
        "Which composers appear most often in the catalogue?",
        "How long is the average track in minutes?",
        "Show me the 20 most recent invoices",
        "What does an average customer spend per invoice?",
        "How many tracks are in each playlist?",
        "Which media types do we sell the most of?",
        "Which country has the highest average invoice value?",
        "How many invoices were there in 2025 compared with 2024?",
        "What share of revenue comes from the top 5 customers?",
        "Which genres have no sales at all?",
        "What is the longest track we sell, and who wrote it?",
        "How many customers are there in each country?",
        "Which employees report to Nancy Edwards?",
        "What is the most expensive track in the catalogue?",
        "How many albums does each of our top 5 artists have?",
        "Which customers bought music in more than one genre?",
        "What was our best month ever for revenue?",
        "Are there any tracks that appear on no playlist?",
        "Which support rep has the highest average invoice?",
        "How many distinct tracks have we ever sold?",
        "What is the average number of tracks per invoice?",
        "Which city has the most customers?",
        "How does revenue in the USA compare with Canada?",
        "Which artist has the most tracks in the catalogue?",
    ],
    # -- classic wholesale orders ------------------------------------
    "northwind": [
        "What is our total sales revenue?",
        "Who are our top 10 customers by order value?",
        "Which products sell the most units?",
        "How do sales break down by country?",
        "Which employees have the highest sales?",
        "What are our best selling product categories?",
        "Which suppliers do we buy the most from?",
        "How many orders did each shipper handle?",
        "Which products have been discontinued?",
        "What is the average order value?",
        "Show me the monthly order trend",
        "Which customers have never placed an order?",
        "What is the most expensive product we sell?",
        "How many products are in each category?",
    ],
    # -- DVD rental --------------------------------------------------
    "pagila": [
        "What is our total rental revenue?",
        "Which films are rented most often?",
        "Who are our top 10 customers by total payments?",
        "Which film categories are the most popular?",
        "Which actors appear in the most films?",
        "What is the average rental duration?",
        "How does revenue break down by store?",
        "What are the longest films in the catalogue?",
        "How many films are there in each rating?",
        "Which cities do most of our customers live in?",
        "Show me the monthly revenue trend",
        "Which staff member processed the most rentals?",
        "How many films has each language got?",
        "Which films have never been rented?",
    ],
    # -- world atlas -------------------------------------------------
    "world": [
        "Which countries have the largest population?",
        "What are the 10 biggest cities in the world?",
        "Which continent has the most countries?",
        "What are the most widely spoken languages?",
        "Which countries have the highest life expectancy?",
        "What is the average population by continent?",
        "Which countries have the highest GNP?",
        "How many cities are there in each country?",
        "Which countries became independent most recently?",
        "What languages are spoken in Switzerland?",
        "How many countries have a population under one million?",
        "Which country has the highest population density?",
        "How many people speak Spanish worldwide?",
        "What is the capital city of each country in South America?",
    ],
    # -- HR, and large -----------------------------------------------
    "employees": [
        "How many employees do we have?",
        "What is the average salary by department?",
        "Who are the 10 highest paid employees?",
        "How many employees are in each department?",
        "What is the gender split across the company?",
        "Which department has the highest average salary?",
        "How has the average salary changed over time?",
        "Who are the current department managers?",
        "What job titles do we have, and how many people hold each?",
        "How many employees were hired each year?",
        "Which employees have worked in more than one department?",
        "What is the highest and lowest salary in the company?",
        "How many people have the title Senior Engineer?",
        "What is the average length of service?",
    ],
    # -- clinic ------------------------------------------------------
    "healthcare": [
        "How many patients do we have?",
        "How many appointments have been booked?",
        "What is our total billing amount?",
        "Which departments see the most patients?",
        "What are the most common allergies?",
        "Which medications are prescribed most often?",
        "How many lab referrals have been made?",
        "Which doctors see the most patients?",
        "What is the average bill per appointment?",
        "How many appointments are there per month?",
        "Which patients have the most prescriptions?",
        "How many medical staff work in each department?",
        "How many patients have no recorded allergies?",
        "What is the busiest day of the week for appointments?",
    ],
    # -- online shop -------------------------------------------------
    "ecommerce": [
        "What is our total revenue?",
        "Who are our top 10 customers by spend?",
        "Which products sell the best?",
        "How many orders were placed each month?",
        "What is the average order value?",
        "Which product categories generate the most revenue?",
        "How many carts were never checked out?",
        "Which payment methods are used most?",
        "Which carriers ship the most orders?",
        "How many orders have not shipped yet?",
        "Which products are low on stock?",
        "How many users have never placed an order?",
        "What is the most expensive product we sell?",
        "What is the most common order status?",
    ],
    # -- hotel -------------------------------------------------------
    "booking": [
        "How many reservations do we have?",
        "Which rooms are booked most often?",
        "How many bookings were cancelled?",
        "What is the average length of stay?",
        "Which guests have stayed with us most often?",
        "What amenities do our rooms offer?",
        "How many bookings come from each market segment?",
        "What is the average daily rate?",
        "Which months are busiest for bookings?",
        "What is our cancellation rate?",
        "How many staff do we employ?",
        "Which room types are the most popular?",
        "How many guests are in an average reservation?",
        "Which country do most of our guests come from?",
    ],
}


#: Requests that would *change* data, kept apart from the read bank because they
#: exercise a different path entirely: ``propose_write`` and ``confirm_write``, not
#: ``run_sql``. Selected with ``--writes``.
#:
#: Three kinds, and the middle kind is the valuable one:
#:
#: * **Allowed** -- small, self-contained edits, which succeed only where a grant
#:   exists. ``tools/grant_demo_writes.py`` is what creates those.
#: * **Refused** -- over the row cap, against an ungranted table, or not expressible
#:   as a plan at all. These need no grants and cannot change anything, so they are
#:   safe to run against any deployment. A corpus that only contains permitted
#:   actions teaches nothing about where the boundary is.
#: * **Ambiguous** -- underspecified requests the agent should ask about rather than
#:   guess at. Guessing which row to update is the failure that matters here.
#:
#: Nothing here touches ``invoice``, ``invoice_line`` or ``track``. The ledger stays
#: read-only, so every question in this file is either harmless or refused.
WRITE_BANKS: Dict[str, List[str]] = {
    "chinook": [
        # -- allowed, where a grant exists ---------------------------
        "Add a new genre called Ambient",
        "Add a genre called Field Recording",
        "Rename the genre Ambient to Ambient & Drone",
        "Create a new playlist called Focus",
        "Rename the playlist Focus to Deep Focus",
        "Delete the playlist called Deep Focus",
        "Update customer 5's email to ada@example.com",
        "Change customer 12's city to Manchester",
        # -- refused: no grant on that table ------------------------
        "Delete invoice 100",
        "Change the unit price of track 1 to 0.49",
        "Add a new invoice for customer 3",
        # -- refused: too many rows, or not a plan ------------------
        "Delete all invoices",
        "Delete every customer",
        "Set every track's price to zero",
        "Drop the customer table",
        "Truncate the invoice_line table",
        # -- ambiguous: the agent should ask, not guess -------------
        "Update the customer's email",
        "Delete the old playlists",
        "Fix the wrong genre name",
    ],
}


# ----------------------------------------------------------------------
# Driving the page
# ----------------------------------------------------------------------


def _api(page: Any, path: str, tenant: str) -> Dict[str, Any]:
    """Read the app's API from inside the page, with the workspace header set.

    The header is what makes this the right workspace: an account here belongs to
    several, and a fetch without it answers for the session default instead.
    """
    return page.evaluate(
        """async ([path, tenant]) => {
            const token = document.cookie.match(/vanna_csrf=([^;]*)/);
            const r = await fetch(path, {
                credentials: 'include',
                headers: {
                    'X-Tenant-Id': tenant,
                    'X-CSRF-Token': token ? decodeURIComponent(token[1]) : '',
                },
            });
            return r.ok ? await r.json() : {error: r.status};
        }""",
        [path, tenant],
    )


def _sign_in(page: Any, base: str, email: str, password: str) -> None:
    page.goto(f"{base}/", wait_until="networkidle")
    if page.locator("#si-email").is_visible():
        page.fill("#si-email", email)
        page.fill("#si-password", password)
        page.click("#si-go")
    page.wait_for_selector("#app.ready", timeout=60_000)


def _switch(page: Any, tenant: str) -> None:
    """Open a different workspace by writing the selection the app persists.

    The switcher in the header reloads the page anyway, so this is a shortcut
    through the UI rather than around it -- the server still checks membership on
    every request. The generous wait is not padding: a cold workspace builds its
    runtime and scans its database on first open, and `employees` has 2.8 million
    salary rows to walk before it will answer anything.
    """
    page.evaluate(
        "(id) => localStorage.setItem('vanna.identity', JSON.stringify({tenant: id}))",
        tenant,
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#app.ready", timeout=240_000)


def _ask(page: Any, question: str) -> None:
    """Open a fresh conversation and send one question.

    Fresh each time so the agent answers the question asked rather than continuing
    a thread thirty questions deep -- which also keeps each exchange its own
    conversation, which is what makes the export able to pair them up.
    """
    # Once the chat element has answered once it takes the `maximized` class and
    # covers the sidebar, so a real click on "New chat" is intercepted by the
    # component and Playwright retries until it times out. Dispatching the event
    # straight at the button bypasses hit-testing, which is the right call here:
    # the button is present and enabled, it is merely underneath something.
    button = page.locator("#new-chat")
    try:
        button.click(timeout=3000)
    except Exception:
        button.dispatch_event("click")

    box = page.locator("input[placeholder*='Ask'], textarea[placeholder*='Ask']").first
    box.wait_for(state="visible", timeout=60_000)
    box.fill(question)
    box.press("Enter")


def _ids_for(page: Any, tenant: str, question: str) -> set:
    """The generations already recorded for this exact question."""
    rows = (_api(page, "/api/vanna/v2/history?limit=200", tenant).get("history")) or []
    return {r["id"] for r in rows if (r.get("question") or "") == question}


def _wait_for_answer(
    page: Any, tenant: str, question: str, *, seen: set, timeout: int
) -> Optional[Dict[str, Any]]:
    """Poll the history API until *this question* records a new generation.

    Server-side truth rather than screen-scraping the component's shadow root: a
    generation row means SQL actually reached the database. A timeout is reported
    as "no answer", not raised -- one slow question should not abandon the rest.

    Matched on the question text, not on "a row newer than the one I saw last".
    Newer-than looked right and quietly reported the wrong SQL for half a run: one
    turn can record more than one generation -- a repair retry executes twice -- so a
    straggler from the previous question lands after this question's baseline is
    taken, satisfies "something is newer" immediately, and gets printed as this
    question's answer. The tell was a three-second answer, which no LLM turn is.

    Not counted, either: the history endpoint pages, so once a workspace holds more
    generations than the page size a count is identical before and after and every
    question reads as unanswered. That is the trap the e2e suite fell into.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = (_api(page, "/api/vanna/v2/history?limit=200", tenant).get("history")) or []
        for row in rows:  # newest first
            if (row.get("question") or "") == question and row["id"] not in seen:
                return row
        page.wait_for_timeout(3000)
    return None


def _wait_for_reply(
    page: Any, tenant: str, question: str, *, timeout: int
) -> Optional[str]:
    """Poll the conversation store for the assistant's reply to this question.

    The write path needs its own signal. ``run_sql`` records a generation and the
    read bank waits on that, but ``propose_write``/``confirm_write`` record nothing
    there -- by design, since the recording tool wraps ``run_sql`` alone. So a write
    that succeeded, and a write that was refused, both leave the history table
    untouched and only the transcript shows what happened.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        listing = _api(page, "/api/vanna/v2/conversations?limit=10", tenant)
        for summary in listing.get("conversations") or []:
            if (summary.get("title") or "").strip() != question.strip():
                continue
            thread = _api(
                page,
                f"/api/vanna/v2/conversations/{urllib.parse.quote(summary['id'])}",
                tenant,
            )
            replies = [
                (m.get("content") or "").strip()
                for m in thread.get("messages") or []
                if m.get("role") == "assistant" and (m.get("content") or "").strip()
            ]
            if replies:
                return replies[-1]
        page.wait_for_timeout(3000)
    return None


def ask_workspace(
    page: Any, tenant: str, questions: List[str], timeout: int, writes: bool = False
) -> Dict[str, Any]:
    """Ask one workspace its whole bank, reporting each answer as it lands."""
    print(f"\n=== {tenant} · {len(questions)} questions " + "=" * 30)
    _switch(page, tenant)

    answered, failed = 0, []
    for index, question in enumerate(questions, start=1):
        seen = set() if writes else _ids_for(page, tenant, question)
        started = time.time()
        try:
            _ask(page, question)
        except Exception as exc:  # a wedged component should not end the run
            failed.append(question)
            print(f"[{index:>2}/{len(questions)}] {question}\n     could not ask: {exc}")
            continue

        print(f"[{index:>2}/{len(questions)}] {question}")
        if writes:
            reply = _wait_for_reply(page, tenant, question, timeout=timeout)
            took = time.time() - started
            if reply is None:
                failed.append(question)
                print(f"     no reply in {took:.0f}s")
                continue
            answered += 1
            flat = " ".join(reply.split())
            print(f"     {took:.0f}s · {flat[:200]}")
            continue

        row = _wait_for_answer(page, tenant, question, seen=seen, timeout=timeout)
        took = time.time() - started
        if row is None:
            failed.append(question)
            print(f"     no answer in {took:.0f}s")
            continue

        answered += 1
        sql = " ".join((row.get("sql") or "").split())
        print(
            f"     {row.get('status')} · {row.get('row_count')} rows · {took:.0f}s"
            + (f" · {row.get('error')}" if row.get("error") else "")
        )
        print(f"     {sql[:140]}")

    return {"tenant": tenant, "answered": answered, "asked": len(questions), "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--tenant",
        default="chinook",
        help=(
            "a workspace, a comma-separated list of them, or 'all'. "
            f"Known: {', '.join(sorted(BANKS))}"
        ),
    )
    parser.add_argument("--limit", type=int, default=0, help="ask only the first N per workspace")
    parser.add_argument("--skip", type=int, default=0, help="skip the first N per workspace")
    parser.add_argument(
        "--timeout", type=int, default=240, help="seconds to wait for each answer"
    )
    parser.add_argument("--headed", action="store_true", help="watch it work")
    parser.add_argument(
        "--writes",
        action="store_true",
        help="ask the write bank (propose_write/confirm_write) instead of the read bank",
    )
    args = parser.parse_args()

    banks = WRITE_BANKS if args.writes else BANKS
    if args.tenant == "all":
        tenants = sorted(banks)
    else:
        tenants = [t.strip() for t in args.tenant.split(",") if t.strip()]
        if unknown := [t for t in tenants if t not in banks]:
            sys.exit(
                f"no {'write ' if args.writes else ''}question bank for "
                f"{', '.join(unknown)}. Known: {', '.join(sorted(banks))}"
            )

    plan = {}
    for tenant in tenants:
        bank = banks[tenant][args.skip :]
        plan[tenant] = bank[: args.limit] if args.limit else bank
    total = sum(len(q) for q in plan.values())
    if not total:
        sys.exit("--skip/--limit selects nothing")

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed, args=["--disable-dev-shm-usage"]
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        _sign_in(page, args.url.rstrip("/"), args.email, args.password)
        print(f"signed in as {args.email}; {total} questions over {len(plan)} workspace(s)")

        for tenant, questions in plan.items():
            if not questions:
                continue
            try:
                results.append(
                    ask_workspace(page, tenant, questions, args.timeout, writes=args.writes)
                )
            except Exception as exc:
                # One unreachable workspace should not cost the other seven.
                print(f"\n=== {tenant}: skipped -- {exc}")
                results.append(
                    {"tenant": tenant, "answered": 0, "asked": len(questions), "failed": questions}
                )

        browser.close()

    print("\n--- result ---")
    answered = sum(r["answered"] for r in results)
    for r in results:
        print(f"  {r['tenant']:<12} {r['answered']}/{r['asked']}")
    print(f"  {'total':<12} {answered}/{total}")

    unanswered = [(r["tenant"], q) for r in results for q in r["failed"]]
    if unanswered:
        print("\n  no answer within the timeout:")
        for tenant, question in unanswered:
            print(f"    {tenant}: {question}")
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
