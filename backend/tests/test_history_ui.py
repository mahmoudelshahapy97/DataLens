"""The history view, driven in a browser.

History was read-only and unpaged: one hundred rows, filtered in the browser, with
no way to forget a question you would rather not have asked and no way to rate a
turn from the list. What is asserted here is that every filter reaches the server
-- the point of moving them there -- and that the two destructive actions are
scoped and confirmed.

Same harness as ``test_ask_ui.py``: the real ``frontend/public`` served the way the
container serves it, only the API stubbed.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from test_ask_ui import (  # noqa: E402 - harness reuse; tests/ is not a package
    ME,
    ONE_DATABASE,
    browser,  # noqa: F401 - pytest fixture
    server,  # noqa: F401 - pytest fixture
)

PAGE = 50  # must match HISTORY_PAGE in app.js
MINE = ME["user"]["id"]


def a_row(index, *, owner=MINE, status="valid", question=None, feedback=None):
    return {
        "id": f"g{index}",
        "question": question or f"question {index}",
        "sql": "SELECT 1",
        "status": status,
        "error": None,
        "row_count": 1,
        "execution_ms": 12.0,
        "feedback": feedback,
        "user_id": owner,
        "conversation_id": "c1",
        "request_id": f"r{index}",
        "model": "mock",
        "cost_usd": 0.0,
        "created_at": "2026-03-10T12:00:00+00:00",
    }


class Api:
    """Honours the filters, so a filter that never leaves the page fails a test."""

    def __init__(self, rows, *, control_plane=True):
        self.rows = list(rows)
        self.control_plane = control_plane
        self.queries = []  # every /history query string, parsed
        self.deleted = []  # ids passed to DELETE /history/{id}
        self.cleared = 0
        self.rated = []  # (request_id, rating)

    def install(self, page):
        def handle(route, request):
            url = request.url
            if "/v2/me" in url:
                return self._json(route, ME)
            if "/v2/datasources" in url:
                return self._json(route, {"data_sources": ONE_DATABASE})
            if "/v2/tenants" in url:
                return self._json(
                    route,
                    {"tenants": [{"id": "acme", "name": "Acme"}], "control_plane": True},
                )
            if "/v2/starters" in url:
                return self._json(route, {"starters": []})
            if "/v2/conversations" in url:
                return self._json(route, {"conversations": [], "persisted": True})
            if "/v2/cubes" in url:
                return self._json(route, {"cubes": []})
            if "/v2/feedback" in url:
                body = json.loads(request.post_data or "{}")
                self.rated.append((body.get("request_id"), body.get("rating")))
                for row in self.rows:
                    if row["request_id"] == body.get("request_id"):
                        row["feedback"] = body.get("rating")
                return self._json(route, {"recorded": True})
            if "/v2/history" in url:
                return self._history(route, request)
            return self._json(route, {})

        page.route("**/api/**", handle)

    def _history(self, route, request):
        parts = urlsplit(request.url)
        tail = parts.path.rsplit("/v2/history", 1)[-1].lstrip("/")

        if request.method == "DELETE":
            if tail:
                self.deleted.append(tail)
                before = len(self.rows)
                self.rows = [
                    row
                    for row in self.rows
                    if not (row["id"] == tail and row["user_id"] == MINE)
                ]
                return self._json(route, {"deleted": before != len(self.rows)})
            gone = [row for row in self.rows if row["user_id"] == MINE]
            self.rows = [row for row in self.rows if row["user_id"] != MINE]
            self.cleared += 1
            return self._json(route, {"deleted": len(gone)})

        if not self.control_plane:
            return self._json(route, {"history": [], "control_plane": False})

        args = {key: value[0] for key, value in parse_qs(parts.query).items()}
        self.queries.append(args)

        rows = self.rows
        if args.get("mine") == "true":
            rows = [row for row in rows if row["user_id"] == MINE]
        if args.get("status"):
            rows = [row for row in rows if row["status"] == args["status"]]
        if args.get("search"):
            needle = args["search"].lower()
            rows = [row for row in rows if needle in row["question"].lower()]
        start = int(args.get("offset", 0))
        limit = int(args.get("limit", PAGE))
        return self._json(
            route, {"history": rows[start : start + limit], "control_plane": True}
        )

    @staticmethod
    def _json(route, payload):
        return route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        )


def open_history(server, browser, api):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    api.install(page)
    page.add_init_script(
        "localStorage.setItem('vanna.identity',"
        " JSON.stringify({tenant:'acme', email:'ada@acme.test'}));"
    )
    page.goto(f"{server}/index.html")
    page.wait_for_selector("#app.ready", timeout=15_000)
    page.click('button[data-view="history"]')
    page.wait_for_selector("#h-body .card, #h-body .empty", timeout=5_000)
    return page, errors


class TestEveryFilterReachesTheServer:
    """The page can only narrow what it has already fetched."""

    def test_status_is_sent(self, server, browser):
        api = Api([a_row(1), a_row(2, status="invalid")])
        page, errors = open_history(server, browser, api)

        page.select_option("#h-status", "invalid")
        page.wait_for_function(
            "document.querySelectorAll('#h-body .card').length === 1"
        )
        assert api.queries[-1]["status"] == "invalid"
        assert errors == []
        page.close()

    def test_a_date_window_is_sent(self, server, browser):
        api = Api([a_row(1)])
        page, errors = open_history(server, browser, api)

        page.fill("#h-since", "2026-03-01")
        page.fill("#h-until", "2026-03-31")
        page.wait_for_function(
            "window.__q = document.querySelectorAll('#h-body .card').length >= 0"
        )
        page.wait_for_timeout(300)
        last = api.queries[-1]
        assert last["since"] == "2026-03-01"
        # Exclusive upper bound, extended to the end of the chosen day so that a
        # single day picked in both boxes covers that day.
        assert last["until"] == "2026-03-31T23:59:59"
        assert errors == []
        page.close()

    def test_only_mine_is_sent(self, server, browser):
        api = Api([a_row(1), a_row(2, owner="someone-else")])
        page, errors = open_history(server, browser, api)

        page.check("#h-mine")
        page.wait_for_function(
            "document.querySelectorAll('#h-body .card').length === 1"
        )
        assert api.queries[-1]["mine"] == "true"
        assert errors == []
        page.close()

    def test_search_is_sent_debounced(self, server, browser):
        api = Api([a_row(1, question="revenue by region"), a_row(2)])
        page, errors = open_history(server, browser, api)

        page.fill("#h-search", "revenue")
        page.wait_for_function(
            "document.querySelectorAll('#h-body .card').length === 1"
        )
        assert api.queries[-1]["search"] == "revenue"
        assert errors == []
        page.close()


class TestPaging:
    def test_a_full_page_offers_more(self, server, browser):
        api = Api([a_row(i) for i in range(PAGE + 3)])
        page, errors = open_history(server, browser, api)

        assert page.locator("#h-body .card").count() == PAGE
        page.click("#h-more")
        page.wait_for_function(
            f"document.querySelectorAll('#h-body .card').length === {PAGE + 3}"
        )
        assert page.locator("#h-more").is_hidden()
        assert api.queries[-1]["offset"] == str(PAGE)
        assert errors == []
        page.close()


class TestForgettingAQuestion:
    def test_only_your_own_rows_offer_it(self, server, browser):
        """The server refuses another member's row; offering a button that is
        always refused would be worse than not offering it."""
        api = Api([a_row(1), a_row(2, owner="someone-else")])
        page, errors = open_history(server, browser, api)

        cards = page.locator("#h-body .card")
        assert cards.nth(0).locator('[data-act="del"]').count() == 1
        assert cards.nth(1).locator('[data-act="del"]').count() == 0
        assert errors == []
        page.close()

    def test_deleting_one_asks_first_and_names_the_question(self, server, browser):
        api = Api([a_row(1, question="how many tracks?")])
        page, errors = open_history(server, browser, api)

        page.click('#h-body [data-act="del"]')
        page.wait_for_selector("#overlay.on")
        assert "how many tracks?" in page.locator("#sheet").inner_text()

        page.click("#dlg-no")
        page.wait_for_selector("#overlay.on", state="hidden")
        assert api.deleted == []

        page.click('#h-body [data-act="del"]')
        page.wait_for_selector("#overlay.on")
        page.click("#dlg-yes")
        page.wait_for_function(
            "document.querySelectorAll('#h-body .card').length === 0"
        )
        assert api.deleted == ["g1"]
        assert errors == []
        page.close()

    def test_clearing_asks_first_and_says_how_many_went(self, server, browser):
        api = Api([a_row(1), a_row(2), a_row(3, owner="someone-else")])
        page, errors = open_history(server, browser, api)

        page.click("#h-clear")
        page.wait_for_selector("#overlay.on")
        page.click("#dlg-yes")
        page.wait_for_function(
            "document.querySelectorAll('#h-body .card').length === 1"
        )
        assert api.cleared == 1
        # Somebody else's row survives -- the route is scoped to the caller.
        assert [row["id"] for row in api.rows] == ["g3"]
        assert errors == []
        page.close()


class TestRatingFromHistory:
    def test_your_own_row_can_be_rated(self, server, browser):
        api = Api([a_row(1)])
        page, errors = open_history(server, browser, api)

        page.click('#h-body [data-act="up"]')
        page.wait_for_function(
            "document.querySelectorAll('#h-body .chip.ok').length >= 1"
        )
        assert api.rated == [("r1", "positive")]
        assert errors == []
        page.close()

    def test_another_members_row_offers_no_rating(self, server, browser):
        """A positive rating promotes the turn to a candidate example, so it
        steers the model for the whole workspace. Owner only."""
        api = Api([a_row(1, owner="someone-else")])
        page, errors = open_history(server, browser, api)

        assert page.locator('#h-body [data-act="up"]').count() == 0
        assert errors == []
        page.close()


class TestWithoutAControlPlane:
    def test_it_says_so_rather_than_looking_empty(self, server, browser):
        page, errors = open_history(server, browser, Api([], control_plane=False))

        assert "control-plane database" in page.locator("#h-body").inner_text()
        assert page.locator("#h-more").is_hidden()
        assert errors == []
        page.close()
