#!/usr/bin/env python3
"""Fill a running workspace with realistic content, so no screen is empty.

A fresh install is correct and completely uninformative. Every list view renders
its empty state, the dashboards screen has nothing to draw, and a screenshot run
photographs a dozen variations on "no data yet" -- which tells you the routes
resolve and nothing else. Review, demos and visual diffs all need a workspace that
looks like somebody has been using it.

So this asks the application, over its own HTTP API, to do what an analyst would
do: run a set of real questions against the bound warehouse, keep the good ones as
saved queries, and assemble them into dashboards. Nothing is inserted into the
database behind the app's back -- every row here went through the SQL policy, the
tool registry and the same authorisation an interactive user gets, which is also
why this doubles as a broad smoke test of the write paths.

    python tools/seed_demo_data.py --url http://localhost:3000 \
        --tenant chinook --email demo@example.com --password ...

``--tenant`` is not optional in spirit. An account here belongs to nine workspaces,
each bound to its own warehouse, and the session's default is not necessarily the
one the page has open -- seed the wrong one and every call succeeds while the screen
you are looking at stays empty.

``--clean`` first removes saved queries and dashboards this script (or a previous
e2e run) left behind, so re-running it does not stack up duplicates. Queries whose
SQL the warehouse rejects are reported and skipped rather than saved: a saved query
that does not run is worse than an empty list.

Written against the Chinook sample warehouse the demo workspace is bound to. The
SQL is deliberately plain -- it is here to produce rows, not to be admired.
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

# ----------------------------------------------------------------------
# The content
# ----------------------------------------------------------------------

#: Questions an analyst would actually ask this warehouse, each with the SQL that
#: answers it. Both halves matter: the question text is what the history screen
#: shows, the SQL is what makes the row real.
QUESTIONS: List[Tuple[str, str]] = [
    (
        "What is our total revenue and how many invoices did it take?",
        """SELECT ROUND(SUM(total)::numeric, 2) AS total_revenue,
                  COUNT(*) AS invoices,
                  ROUND(AVG(total)::numeric, 2) AS average_invoice
           FROM chinook.invoice""",
    ),
    (
        "Who are our top 10 customers by lifetime spend?",
        """SELECT c.first_name || ' ' || c.last_name AS customer,
                  c.country,
                  ROUND(SUM(i.total)::numeric, 2) AS lifetime_spend,
                  COUNT(i.invoice_id) AS invoices
           FROM chinook.invoice i
           JOIN chinook.customer c ON c.customer_id = i.customer_id
           GROUP BY 1, 2
           ORDER BY lifetime_spend DESC
           LIMIT 10""",
    ),
    (
        "How does revenue break down by country?",
        """SELECT billing_country AS country,
                  ROUND(SUM(total)::numeric, 2) AS revenue,
                  COUNT(*) AS invoices,
                  ROUND(AVG(total)::numeric, 2) AS average_invoice
           FROM chinook.invoice
           GROUP BY 1
           ORDER BY revenue DESC
           LIMIT 25""",
    ),
    (
        "Show me the monthly revenue trend",
        """SELECT TO_CHAR(DATE_TRUNC('month', invoice_date), 'YYYY-MM') AS month,
                  ROUND(SUM(total)::numeric, 2) AS revenue,
                  COUNT(*) AS invoices
           FROM chinook.invoice
           GROUP BY 1
           ORDER BY 1
           LIMIT 100""",
    ),
    (
        "Which genres sell the most tracks?",
        """SELECT g.name AS genre,
                  SUM(il.quantity) AS tracks_sold,
                  ROUND(SUM(il.unit_price * il.quantity)::numeric, 2) AS revenue
           FROM chinook.invoice_line il
           JOIN chinook.track t ON t.track_id = il.track_id
           JOIN chinook.genre g ON g.genre_id = t.genre_id
           GROUP BY 1
           ORDER BY revenue DESC
           LIMIT 15""",
    ),
    (
        "Who are the top 10 best selling artists?",
        """SELECT ar.name AS artist,
                  SUM(il.quantity) AS tracks_sold,
                  ROUND(SUM(il.unit_price * il.quantity)::numeric, 2) AS revenue
           FROM chinook.invoice_line il
           JOIN chinook.track t ON t.track_id = il.track_id
           JOIN chinook.album al ON al.album_id = t.album_id
           JOIN chinook.artist ar ON ar.artist_id = al.artist_id
           GROUP BY 1
           ORDER BY revenue DESC
           LIMIT 10""",
    ),
    (
        "Which albums earned us the most money?",
        """SELECT al.title AS album,
                  ar.name AS artist,
                  ROUND(SUM(il.unit_price * il.quantity)::numeric, 2) AS revenue
           FROM chinook.invoice_line il
           JOIN chinook.track t ON t.track_id = il.track_id
           JOIN chinook.album al ON al.album_id = t.album_id
           JOIN chinook.artist ar ON ar.artist_id = al.artist_id
           GROUP BY 1, 2
           ORDER BY revenue DESC
           LIMIT 12""",
    ),
    (
        "How is each sales rep performing?",
        """SELECT e.first_name || ' ' || e.last_name AS sales_rep,
                  e.title,
                  COUNT(DISTINCT c.customer_id) AS customers,
                  ROUND(SUM(i.total)::numeric, 2) AS revenue
           FROM chinook.employee e
           JOIN chinook.customer c ON c.support_rep_id = e.employee_id
           JOIN chinook.invoice i ON i.customer_id = c.customer_id
           GROUP BY 1, 2
           ORDER BY revenue DESC
           LIMIT 20""",
    ),
    (
        "What are the 15 highest earning tracks?",
        """SELECT t.name AS track,
                  ar.name AS artist,
                  SUM(il.quantity) AS units,
                  ROUND(SUM(il.unit_price * il.quantity)::numeric, 2) AS revenue
           FROM chinook.invoice_line il
           JOIN chinook.track t ON t.track_id = il.track_id
           LEFT JOIN chinook.album al ON al.album_id = t.album_id
           LEFT JOIN chinook.artist ar ON ar.artist_id = al.artist_id
           GROUP BY 1, 2
           ORDER BY revenue DESC
           LIMIT 15""",
    ),
    (
        "How do our customers split across media types?",
        """SELECT m.name AS media_type,
                  COUNT(DISTINCT t.track_id) AS tracks,
                  SUM(il.quantity) AS units_sold
           FROM chinook.track t
           JOIN chinook.media_type m ON m.media_type_id = t.media_type_id
           LEFT JOIN chinook.invoice_line il ON il.track_id = t.track_id
           GROUP BY 1
           ORDER BY units_sold DESC NULLS LAST
           LIMIT 10""",
    ),
    (
        "Which cities generate the most revenue?",
        """SELECT billing_city AS city,
                  billing_country AS country,
                  ROUND(SUM(total)::numeric, 2) AS revenue
           FROM chinook.invoice
           GROUP BY 1, 2
           ORDER BY revenue DESC
           LIMIT 20""",
    ),
    (
        "How long are our playlists?",
        """SELECT p.name AS playlist,
                  COUNT(pt.track_id) AS tracks
           FROM chinook.playlist p
           LEFT JOIN chinook.playlist_track pt ON pt.playlist_id = p.playlist_id
           GROUP BY 1
           ORDER BY tracks DESC
           LIMIT 20""",
    ),
    (
        "What does an average customer spend per invoice, by country?",
        """SELECT c.country,
                  COUNT(DISTINCT c.customer_id) AS customers,
                  ROUND(AVG(i.total)::numeric, 2) AS average_invoice,
                  ROUND(SUM(i.total)::numeric, 2) AS revenue
           FROM chinook.customer c
           JOIN chinook.invoice i ON i.customer_id = c.customer_id
           GROUP BY 1
           HAVING COUNT(DISTINCT c.customer_id) > 1
           ORDER BY average_invoice DESC
           LIMIT 20""",
    ),
    (
        "Show me the 20 most recent invoices",
        """SELECT i.invoice_id,
                  i.invoice_date::date AS invoice_date,
                  c.first_name || ' ' || c.last_name AS customer,
                  i.billing_country AS country,
                  ROUND(i.total::numeric, 2) AS total
           FROM chinook.invoice i
           JOIN chinook.customer c ON c.customer_id = i.customer_id
           ORDER BY i.invoice_date DESC
           LIMIT 20""",
    ),
    (
        "Which tracks are the longest in the catalogue?",
        """SELECT t.name AS track,
                  ar.name AS artist,
                  ROUND((t.milliseconds / 60000.0)::numeric, 1) AS minutes
           FROM chinook.track t
           LEFT JOIN chinook.album al ON al.album_id = t.album_id
           LEFT JOIN chinook.artist ar ON ar.artist_id = al.artist_id
           ORDER BY t.milliseconds DESC
           LIMIT 15""",
    ),
    (
        "How many customers have never bought anything?",
        """SELECT COUNT(*) AS customers_without_invoices
           FROM chinook.customer c
           WHERE NOT EXISTS (
               SELECT 1 FROM chinook.invoice i WHERE i.customer_id = c.customer_id
           )""",
    ),
    (
        "What is the revenue per year?",
        """SELECT EXTRACT(YEAR FROM invoice_date)::int AS year,
                  ROUND(SUM(total)::numeric, 2) AS revenue,
                  COUNT(*) AS invoices
           FROM chinook.invoice
           GROUP BY 1
           ORDER BY 1
           LIMIT 20""",
    ),
    (
        "Which composers appear most often in the catalogue?",
        """SELECT composer,
                  COUNT(*) AS tracks,
                  ROUND(AVG(milliseconds / 60000.0)::numeric, 1) AS average_minutes
           FROM chinook.track
           WHERE composer IS NOT NULL AND composer <> ''
           GROUP BY 1
           ORDER BY tracks DESC
           LIMIT 15""",
    ),
]

#: Which of the above are worth keeping as saved queries, by index, and under what
#: title. Not all of them: a saved list is a curated thing, and one that mirrors
#: every question ever asked is just the history screen again.
SAVED: List[Tuple[int, str]] = [
    (0, "Revenue at a glance"),
    (1, "Top 10 customers by lifetime spend"),
    (2, "Revenue by country"),
    (3, "Monthly revenue trend"),
    (4, "Genre performance"),
    (5, "Top 10 artists by revenue"),
    (6, "Best selling albums"),
    (7, "Sales rep leaderboard"),
    (8, "Highest earning tracks"),
    (10, "Revenue by city"),
    (12, "Average invoice by country"),
    (13, "Latest invoices"),
    (16, "Revenue by year"),
]


#: Verified examples -- the curated question/SQL pairs retrieval draws on. Written
#: by hand and stored already verified, which is what the API does for an admin:
#: a human writing the pair *is* the review.
#:
#: Deliberately not the same list as the saved queries. An example exists to teach
#: the model a join path or a convention, so the useful ones are small and show one
#: idea each; a saved query exists for a person to re-run, so it is whole.
EXAMPLES: List[Tuple[str, str, List[str]]] = [
    (
        "How many customers do we have?",
        "SELECT COUNT(*) AS customers FROM chinook.customer",
        ["count"],
    ),
    (
        "What is total revenue?",
        "SELECT ROUND(SUM(total)::numeric, 2) AS total_revenue FROM chinook.invoice",
        ["revenue"],
    ),
    (
        "Revenue by country",
        "SELECT billing_country AS country, ROUND(SUM(total)::numeric, 2) AS revenue "
        "FROM chinook.invoice GROUP BY 1 ORDER BY revenue DESC",
        ["revenue", "geography"],
    ),
    (
        "Which customer spent the most?",
        "SELECT c.first_name || ' ' || c.last_name AS customer, "
        "ROUND(SUM(i.total)::numeric, 2) AS lifetime_spend "
        "FROM chinook.invoice i JOIN chinook.customer c ON c.customer_id = i.customer_id "
        "GROUP BY 1 ORDER BY lifetime_spend DESC LIMIT 1",
        ["customers", "join"],
    ),
    (
        "How many tracks are in each genre?",
        "SELECT g.name AS genre, COUNT(*) AS tracks FROM chinook.track t "
        "JOIN chinook.genre g ON g.genre_id = t.genre_id GROUP BY 1 ORDER BY tracks DESC",
        ["catalogue", "join"],
    ),
    (
        "Which tracks sold the most units?",
        "SELECT t.name AS track, SUM(il.quantity) AS units FROM chinook.invoice_line il "
        "JOIN chinook.track t ON t.track_id = il.track_id GROUP BY 1 "
        "ORDER BY units DESC LIMIT 10",
        ["catalogue", "sales"],
    ),
    (
        "Revenue per month in 2025",
        "SELECT TO_CHAR(DATE_TRUNC('month', invoice_date), 'YYYY-MM') AS month, "
        "ROUND(SUM(total)::numeric, 2) AS revenue FROM chinook.invoice "
        "WHERE invoice_date >= '2025-01-01' AND invoice_date < '2026-01-01' "
        "GROUP BY 1 ORDER BY 1",
        ["revenue", "time"],
    ),
    (
        "Which employee supports the most customers?",
        "SELECT e.first_name || ' ' || e.last_name AS sales_rep, "
        "COUNT(c.customer_id) AS customers FROM chinook.employee e "
        "JOIN chinook.customer c ON c.support_rep_id = e.employee_id "
        "GROUP BY 1 ORDER BY customers DESC",
        ["employees", "join"],
    ),
    (
        "How long is the average track, in minutes?",
        "SELECT ROUND(AVG(milliseconds / 60000.0)::numeric, 2) AS average_minutes "
        "FROM chinook.track",
        ["catalogue"],
    ),
    (
        "List the albums by a given artist",
        "SELECT al.title AS album FROM chinook.album al "
        "JOIN chinook.artist ar ON ar.artist_id = al.artist_id "
        "WHERE ar.name = 'AC/DC' ORDER BY al.title",
        ["catalogue", "filter"],
    ),
]

# Workspace instructions and starter questions used to live here, pushed over the
# admin API. They have moved to backend/domains/domains.yml, which is version
# controlled and is what `python -m vanna_app.domains provision` applies to every
# deployment.
#
# Two sources of truth for the same content was not a stylistic problem. The rule
# seeded here said revenue was invoice.total while the provisioned rule said it was
# the sum of invoice_line, so the model was handed both and told they were both
# true. And because this script pushed starters on top of the provisioned three,
# the chat screen ended up with thirteen buttons in an order nobody chose.
#
# tools/prune_chinook_seed.py removes what this script already wrote.


def dashboards(saved_ids: Dict[str, str]) -> List[Dict[str, Any]]:
    """Three dashboards, built from the queries that were saved successfully.

    Tiles point at saved queries by id wherever one exists, which is the shape the
    application prefers -- the SQL then lives in exactly one place. Inline SQL is
    used only for the single-number tiles, which have no saved equivalent worth
    cluttering the list with.
    """

    def ref(title: str) -> Optional[Dict[str, str]]:
        saved_id = saved_ids.get(title)
        return {"source": "saved", "saved_query_id": saved_id} if saved_id else None

    def tile(**kw: Any) -> Dict[str, Any]:
        return kw

    out: List[Dict[str, Any]] = []

    # -- 1. the commercial overview ------------------------------------
    revenue_tiles = [
        tile(
            kind="text",
            title="",
            text=(
                "## Commercial overview\n"
                "Chinook store performance across the whole invoice history. "
                "Figures are in the invoice currency and include every settled line."
            ),
            grid={"x": 0, "y": 0, "width": 12, "height": 2},
        ),
        tile(
            kind="metric",
            title="Total revenue",
            query={
                "source": "sql",
                "sql": "SELECT ROUND(SUM(total)::numeric, 2) AS total_revenue FROM chinook.invoice",
            },
            grid={"x": 0, "y": 2, "width": 3, "height": 3},
        ),
        tile(
            kind="metric",
            title="Invoices",
            query={
                "source": "sql",
                "sql": "SELECT COUNT(*) AS invoices FROM chinook.invoice",
            },
            grid={"x": 3, "y": 2, "width": 3, "height": 3},
        ),
        tile(
            kind="metric",
            title="Customers",
            query={
                "source": "sql",
                "sql": "SELECT COUNT(*) AS customers FROM chinook.customer",
            },
            grid={"x": 6, "y": 2, "width": 3, "height": 3},
        ),
        tile(
            kind="metric",
            title="Average invoice",
            query={
                "source": "sql",
                "sql": "SELECT ROUND(AVG(total)::numeric, 2) AS average_invoice FROM chinook.invoice",
            },
            grid={"x": 9, "y": 2, "width": 3, "height": 3},
        ),
    ]
    if ref("Monthly revenue trend"):
        revenue_tiles.append(
            tile(
                kind="chart",
                title="Monthly revenue",
                query=ref("Monthly revenue trend"),
                chart={"type": "line", "x": "month", "y": ["revenue"]},
                grid={"x": 0, "y": 5, "width": 8, "height": 5},
            )
        )
    if ref("Revenue by country"):
        revenue_tiles.append(
            tile(
                kind="chart",
                title="Revenue by country",
                query=ref("Revenue by country"),
                chart={"type": "bar", "x": "country", "y": ["revenue"], "limit": 10},
                grid={"x": 8, "y": 5, "width": 4, "height": 5},
            )
        )
    if ref("Top 10 customers by lifetime spend"):
        revenue_tiles.append(
            tile(
                kind="table",
                title="Top customers",
                query=ref("Top 10 customers by lifetime spend"),
                grid={"x": 0, "y": 10, "width": 6, "height": 5},
            )
        )
    if ref("Latest invoices"):
        revenue_tiles.append(
            tile(
                kind="table",
                title="Latest invoices",
                query=ref("Latest invoices"),
                grid={"x": 6, "y": 10, "width": 6, "height": 5},
            )
        )
    out.append(
        {
            "title": "Revenue overview",
            "description": "Headline numbers, the monthly trend, and where the money comes from.",
            "tiles": revenue_tiles,
        }
    )

    # -- 2. the catalogue ---------------------------------------------
    catalogue_tiles = [
        tile(
            kind="text",
            title="",
            text=(
                "## Catalogue performance\n"
                "What people are actually buying, by genre, artist and album."
            ),
            grid={"x": 0, "y": 0, "width": 12, "height": 2},
        ),
        tile(
            kind="metric",
            title="Tracks in catalogue",
            query={"source": "sql", "sql": "SELECT COUNT(*) AS tracks FROM chinook.track"},
            grid={"x": 0, "y": 2, "width": 4, "height": 3},
        ),
        tile(
            kind="metric",
            title="Artists",
            query={"source": "sql", "sql": "SELECT COUNT(*) AS artists FROM chinook.artist"},
            grid={"x": 4, "y": 2, "width": 4, "height": 3},
        ),
        tile(
            kind="metric",
            title="Albums",
            query={"source": "sql", "sql": "SELECT COUNT(*) AS albums FROM chinook.album"},
            grid={"x": 8, "y": 2, "width": 4, "height": 3},
        ),
    ]
    if ref("Genre performance"):
        catalogue_tiles.append(
            tile(
                kind="chart",
                title="Revenue by genre",
                query=ref("Genre performance"),
                chart={"type": "bar", "x": "genre", "y": ["revenue"], "limit": 10},
                grid={"x": 0, "y": 5, "width": 6, "height": 5},
            )
        )
    if ref("Top 10 artists by revenue"):
        catalogue_tiles.append(
            tile(
                kind="chart",
                title="Top artists",
                query=ref("Top 10 artists by revenue"),
                chart={"type": "bar", "x": "artist", "y": ["revenue"]},
                grid={"x": 6, "y": 5, "width": 6, "height": 5},
            )
        )
    if ref("Best selling albums"):
        catalogue_tiles.append(
            tile(
                kind="table",
                title="Best selling albums",
                query=ref("Best selling albums"),
                grid={"x": 0, "y": 10, "width": 6, "height": 5},
            )
        )
    if ref("Highest earning tracks"):
        catalogue_tiles.append(
            tile(
                kind="table",
                title="Highest earning tracks",
                query=ref("Highest earning tracks"),
                grid={"x": 6, "y": 10, "width": 6, "height": 5},
            )
        )
    out.append(
        {
            "title": "Catalogue performance",
            "description": "Genres, artists and albums ranked by what they earn.",
            "tiles": catalogue_tiles,
        }
    )

    # -- 3. the sales team --------------------------------------------
    team_tiles = [
        tile(
            kind="text",
            title="",
            text=(
                "## Sales team\n"
                "Per-rep revenue and the geography each of them covers. "
                "Reps are attached to customers, so revenue follows the customer."
            ),
            grid={"x": 0, "y": 0, "width": 12, "height": 2},
        ),
    ]
    if ref("Sales rep leaderboard"):
        team_tiles += [
            tile(
                kind="chart",
                title="Revenue by rep",
                query=ref("Sales rep leaderboard"),
                chart={"type": "bar", "x": "sales_rep", "y": ["revenue"]},
                grid={"x": 0, "y": 2, "width": 7, "height": 5},
            ),
            tile(
                kind="table",
                title="Rep detail",
                query=ref("Sales rep leaderboard"),
                grid={"x": 0, "y": 7, "width": 12, "height": 4},
            ),
        ]
    if ref("Revenue by city"):
        team_tiles.append(
            tile(
                kind="chart",
                title="Revenue by city",
                query=ref("Revenue by city"),
                chart={"type": "bar", "x": "city", "y": ["revenue"], "limit": 12},
                grid={"x": 7, "y": 2, "width": 5, "height": 5},
            )
        )
    out.append(
        {
            "title": "Sales team",
            "description": "How the eight reps compare, and the cities behind their numbers.",
            "tiles": team_tiles,
        }
    )

    return out


# ----------------------------------------------------------------------
# A very small HTTP client
# ----------------------------------------------------------------------


def _decode(raw: str) -> Any:
    """JSON when the response is JSON, the text otherwise.

    ``GET /`` is fetched only to be issued the CSRF cookie and answers with the
    page itself, so a client that insists on JSON falls over before it has signed
    in -- on the one call whose body nobody wants.
    """
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"detail": raw[:300]}


class Client:
    """Just enough of a session to hold cookies and echo the CSRF token.

    ``requests`` would be one import, but this script is run against a container
    by whoever is reviewing it and the standard library is always there.
    """

    def __init__(self, base: str, tenant: Optional[str] = None) -> None:
        self.base = base.rstrip("/")
        self.tenant = tenant
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    # -- plumbing ------------------------------------------------------

    def _csrf(self) -> str:
        for cookie in self.jar:
            if cookie.name == "vanna_csrf":
                return urllib.parse.unquote(cookie.value or "")
        return ""

    def call(
        self, method: str, path: str, body: Optional[Any] = None
    ) -> Tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        token = self._csrf()
        if token:
            headers["X-CSRF-Token"] = token
        # The workspace is chosen per request, not per session: an account can
        # belong to nine of them and the session's default is only one. Omitting
        # this header seeds whichever workspace the session defaults to, while the
        # page you are looking at sends its own -- so the data lands somewhere real
        # and appears nowhere. That is worth a whole flag.
        if self.tenant:
            headers["X-Tenant-Id"] = self.tenant

        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read().decode(errors="replace")
                return response.status, _decode(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            return exc.code, _decode(raw)

    # -- the app -------------------------------------------------------

    def sign_in(self, email: str, password: str) -> None:
        # GET first: the CSRF cookie is issued by the page, and the login POST is
        # itself protected, so without this the very first write is a 403.
        self.call("GET", "/")
        body: Dict[str, Any] = {"email": email, "password": password}
        if self.tenant:
            body["tenant"] = self.tenant
        status, payload = self.call("POST", "/api/vanna/v2/auth/login", body)
        if status != 200:
            raise SystemExit(f"sign-in failed ({status}): {_why(payload)}")

    def whoami(self) -> str:
        """Which workspace these writes will actually land in.

        Printed rather than assumed, because the failure it guards against is
        silent: everything succeeds, against the wrong workspace.
        """
        status, payload = self.call("GET", "/api/vanna/v2/me")
        if status != 200 or not isinstance(payload, dict):
            return "unknown"
        tenant = payload.get("tenant") or {}
        memberships = payload.get("memberships") or []
        effective = self.tenant or tenant.get("id")
        note = "" if not memberships else f" (of {len(memberships)}: {', '.join(memberships)})"
        return f"{effective}{note}"


# ----------------------------------------------------------------------
# Seeding
# ----------------------------------------------------------------------


def run_questions(client: Client) -> Dict[int, bool]:
    """Execute every question's SQL, which is what puts it in the history.

    ``run-sql`` goes through the recording tool, so each successful call becomes a
    generation row with its SQL, row count and duration. Failures are reported --
    a question that cannot run is a broken seed, not a data point.
    """
    ok: Dict[int, bool] = {}
    for index, (question, sql) in enumerate(QUESTIONS):
        status, payload = client.call(
            "POST",
            "/api/vanna/v2/run-sql",
            {"sql": " ".join(sql.split()), "question": question, "limit": 200},
        )
        rows = payload.get("row_count") if isinstance(payload, dict) else None
        ok[index] = status == 200 and not (
            isinstance(payload, dict) and payload.get("error")
        )
        state = f"{rows} rows" if ok[index] else f"FAILED {status}: {_why(payload)}"
        print(f"  [{index:>2}] {question[:58]:<60} {state}")
    return ok


def save_queries(client: Client, ok: Dict[int, bool]) -> Dict[str, str]:
    """Keep the curated subset, skipping anything that did not run."""
    ids: Dict[str, str] = {}
    for index, title in SAVED:
        if not ok.get(index):
            print(f"  skipped {title!r}: its SQL did not run")
            continue
        question, sql = QUESTIONS[index]
        status, payload = client.call(
            "POST",
            "/api/vanna/v2/saved-queries",
            {"title": title, "sql": " ".join(sql.split()), "question": question},
        )
        if status == 200 and isinstance(payload, dict):
            ids[title] = payload["saved"]["id"]
            print(f"  saved {title!r}")
        else:
            print(f"  FAILED to save {title!r} ({status}): {_why(payload)}")
    return ids


def save_dashboards(client: Client, saved_ids: Dict[str, str]) -> int:
    """Store each dashboard, then render it so a broken tile surfaces here."""
    made = 0
    for document in dashboards(saved_ids):
        status, payload = client.call("POST", "/api/vanna/v2/dashboards", document)
        if status != 200 or not isinstance(payload, dict):
            print(f"  FAILED {document['title']!r} ({status}): {_why(payload)}")
            continue

        made += 1
        stored = payload["dashboard"]
        warnings = payload.get("warnings") or []
        print(
            f"  built {document['title']!r} "
            f"({len(document['tiles'])} tiles)"
            + (f" warnings: {warnings}" if warnings else "")
        )

        # Render it once. A dashboard that stores fine and dies on read is the
        # exact failure this script exists to avoid handing to a screenshot run.
        code, data = client.call(
            "GET", f"/api/vanna/v2/dashboards/{stored['id']}/data"
        )
        if code != 200:
            print(f"    but it does not render ({code}): {_why(data)}")
        else:
            results = data.get("results") or data.get("tiles") or []
            broken = [
                r.get("title") or r.get("tile_id")
                for r in results
                if isinstance(r, dict) and r.get("error")
            ]
            if broken:
                print(f"    tiles with errors: {broken}")
    return made


def save_examples(client: Client) -> int:
    """Author the verified examples, skipping any the warehouse rejects.

    Each one is executed before it is stored. An example is training data for the
    model, so a wrong one is worse than a missing one -- it teaches a join path
    that does not work and then ranks highly for exactly the question it breaks.
    """
    made = 0
    for question, sql, tags in EXAMPLES:
        statement = " ".join(sql.split())
        code, result = client.call(
            "POST", "/api/vanna/v2/run-sql", {"sql": statement, "limit": 50}
        )
        if code != 200 or (isinstance(result, dict) and result.get("error")):
            print(f"  skipped {question!r}: {_why(result)}")
            continue

        status, payload = client.call(
            "POST",
            "/api/vanna/v2/admin/examples",
            {"question": question, "sql": statement, "tags": tags},
        )
        if status == 200:
            made += 1
            print(f"  verified {question!r}")
        else:
            print(f"  FAILED {question!r} ({status}): {_why(payload)}")
    return made


def clean_admin(client: Client, tenant: str) -> None:
    """Remove the verified examples this script authored.

    Matched on content rather than on a marker, because the endpoint stores no field
    this script controls -- so the only honest way to recognise its own rows is that
    it knows exactly what it wrote.

    Instructions and starter questions are deliberately not touched. They belong to
    backend/domains/domains.yml now, and a seeder that deletes provisioned content
    would undo a deployment step every time somebody asked for demo data.
    """
    status, payload = client.call("GET", "/api/vanna/v2/admin/examples")
    questions = {q for q, _, _ in EXAMPLES}
    for row in (payload.get("examples") or []) if status == 200 else []:
        if row.get("question") in questions:
            client.call("DELETE", f"/api/vanna/v2/admin/examples/{row['id']}")


def clean(client: Client) -> None:
    """Remove what an earlier run of this script -- or of the e2e suite -- left."""
    status, payload = client.call("GET", "/api/vanna/v2/saved-queries")
    titles = {title for _, title in SAVED} | {"e2e probe"}
    for row in (payload.get("saved") or []) if status == 200 else []:
        if row.get("title") in titles:
            client.call("DELETE", f"/api/vanna/v2/saved-queries/{row['id']}")
    print(f"  saved queries now: {_count(client, 'saved-queries', 'saved')}")

    status, payload = client.call("GET", "/api/vanna/v2/dashboards")
    wanted = {d["title"] for d in dashboards({})}
    for row in (payload.get("dashboards") or []) if status == 200 else []:
        document = row.get("document") or row
        if document.get("title") in wanted:
            client.call("DELETE", f"/api/vanna/v2/dashboards/{document['id']}")
    print(f"  dashboards now: {_count(client, 'dashboards', 'dashboards')}")


def _count(client: Client, path: str, key: str) -> int:
    status, payload = client.call("GET", f"/api/vanna/v2/{path}")
    return len(payload.get(key) or []) if status == 200 else -1


def _why(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("error") or payload)[:200]
    return str(payload)[:200]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--tenant",
        default="chinook",
        help=(
            "workspace to seed, sent as X-Tenant-Id. Must be the one the page you "
            "are looking at has open, and must be bound to the Chinook warehouse. "
            "Pass an empty string to use the session default."
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="delete this script's saved queries and dashboards first",
    )
    args = parser.parse_args()

    client = Client(args.url, tenant=args.tenant or None)
    client.sign_in(args.email, args.password)
    print(f"signed in to {args.url} as {args.email}")
    print(f"seeding workspace: {client.whoami()}")

    tenant = args.tenant or ""

    if args.clean:
        print("\nclearing previous seed data")
        clean(client)
        if tenant:
            clean_admin(client, tenant)

    print(f"\nrunning {len(QUESTIONS)} questions")
    ok = run_questions(client)

    print(f"\nsaving {len(SAVED)} queries")
    ids = save_queries(client, ok)

    print("\nbuilding dashboards")
    made = save_dashboards(client, ids)

    print(f"\nauthoring {len(EXAMPLES)} verified examples")
    examples = save_examples(client)

    if tenant:
        print("\nstarter questions and workspace instructions")
        starters = save_starters(client, tenant)
        rules = save_instructions(client, tenant)
    else:
        print("\nskipping starters and instructions: they are addressed per tenant")
        starters = rules = 0

    print("\n--- result ---")
    print(f"  questions that ran : {sum(ok.values())}/{len(QUESTIONS)}")
    print(f"  saved queries      : {_count(client, 'saved-queries', 'saved')}")
    print(f"  dashboards         : {made}")
    print(f"  history rows       : {_count(client, 'history', 'history')}")
    print(f"  verified examples  : {examples}")
    print("\n  instructions and starter questions come from domains.yml:")
    print("    docker compose exec backend python -m vanna_app.domains provision")

    failed = len(QUESTIONS) - sum(ok.values())
    if failed:
        print(f"\n{failed} question(s) did not run; see FAILED above")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
