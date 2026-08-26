"""The account screen: tokens, sessions, and signing one device out.

Two things are pinned here. The first is a bug this pass found on the running
deployment: ``renderAccount`` asked the *response envelope* for its ``.length``
(``{tokens: [...]}`` is not an array), so the first paint of this screen has always
claimed there were no tokens and no other sessions however many there were. The
token list quietly recovered on the next refresh; the session list had nothing to
recover it, so "sign out everywhere else" was never offered either.

The second is the new per-session revoke: the list rendered every session and only
offered an all-or-nothing purge, which is the wrong shape for the usual case -- one
unfamiliar device, and no reason to sign out the three that are fine.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from test_ask_ui import (  # noqa: E402 - harness reuse; tests/ is not a package
    ME,
    ONE_DATABASE,
    browser,  # noqa: F401 - pytest fixture
    server,  # noqa: F401 - pytest fixture
)

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0 Safari/537.36"
)
FIREFOX = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15) Gecko/20100101 Firefox/141.0"


def a_session(identifier, *, current=False, agent=CHROME):
    return {
        "id": identifier,
        "user_agent": agent,
        "ip": "203.0.113.7",
        "created_at": "2026-08-20T09:00:00+00:00",
        "expires_at": "2026-08-27T09:00:00+00:00",
        "scope": "full",
        "is_current": current,
    }


def a_token(identifier, name="laptop"):
    return {
        "id": identifier,
        "name": name,
        "created_at": "2026-08-01T09:00:00+00:00",
        "last_used_at": None,
        "expires_at": None,
    }


class Api:
    def __init__(self, *, sessions=(), tokens=()):
        self.sessions = list(sessions)
        self.tokens = list(tokens)
        self.revoked = []  # session ids passed to DELETE
        self.purges = 0
        self.created = []  # bodies passed to POST /auth/tokens

    def install(self, page):
        def handle(route, request):
            url, method = request.url, request.method
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
            if "/v2/usage" in url:
                return self._json(route, {"enabled": False})
            if "/auth/sessions" in url:
                return self._sessions(route, request)
            if "/auth/tokens" in url:
                if method == "POST":
                    self.created.append(request.post_data_json or {})
                    return self._json(route, {"token": "vn_secret", "note": "once"})
                return self._json(route, {"tokens": self.tokens})
            return self._json(route, {})

        page.route("**/api/**", handle)

    def _sessions(self, route, request):
        tail = request.url.split("/auth/sessions", 1)[-1].strip("/").split("?")[0]
        if request.method == "DELETE":
            if tail:
                self.revoked.append(tail)
                self.sessions = [s for s in self.sessions if s["id"] != tail]
                return self._json(route, {"ended": 1})
            self.purges += 1
            ended = [s for s in self.sessions if not s["is_current"]]
            self.sessions = [s for s in self.sessions if s["is_current"]]
            return self._json(route, {"ended": len(ended)})
        return self._json(route, {"sessions": self.sessions})

    @staticmethod
    def _json(route, payload):
        return route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        )


def open_account(server, browser, api):
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
    page.click('button[data-view="account"]')
    page.wait_for_selector("#token-new", timeout=5_000)
    return page, errors


class TestTheFirstPaintShowsWhatIsThere:
    def test_sessions_are_listed(self, server, browser):
        """They were not: ``{sessions: [...]}.length`` is undefined."""
        api = Api(sessions=[a_session("aaaa1111", current=True),
                            a_session("bbbb2222", agent=FIREFOX)])
        page, errors = open_account(server, browser, api)
        try:
            assert page.locator("[data-session]").count() == 1
            body = page.locator("#view-other").inner_text()
            assert "No other sessions" not in body
            assert "Firefox" in body
            assert errors == []
        finally:
            page.close()

    def test_tokens_are_listed(self, server, browser):
        api = Api(sessions=[a_session("aaaa1111", current=True)],
                  tokens=[a_token("t1", "laptop")])
        page, errors = open_account(server, browser, api)
        try:
            assert "laptop" in page.locator("#token-list").inner_text()
            assert errors == []
        finally:
            page.close()

    def test_the_bulk_purge_appears_once_there_is_something_to_purge(
        self, server, browser
    ):
        api = Api(sessions=[a_session("aaaa1111", current=True)])
        page, errors = open_account(server, browser, api)
        try:
            assert page.locator("#sess-purge").count() == 0
            assert page.locator("[data-session]").count() == 0
            assert errors == []
        finally:
            page.close()

        api = Api(sessions=[a_session("aaaa1111", current=True), a_session("bbbb2222")])
        page, errors = open_account(server, browser, api)
        try:
            assert page.locator("#sess-purge").count() == 1
            assert errors == []
        finally:
            page.close()


class TestSigningOutOneDevice:
    def test_the_current_session_offers_no_revoke(self, server, browser):
        """Revoking it would sign the caller out while the page believes otherwise;
        the server refuses it, and the row does not offer it."""
        api = Api(sessions=[a_session("aaaa1111", current=True), a_session("bbbb2222")])
        page, errors = open_account(server, browser, api)
        try:
            rows = page.locator(".row-line")
            # The chip is text-transform: uppercase, so compare case-folded.
            assert "this browser" in rows.nth(0).inner_text().lower()
            assert rows.nth(0).locator("[data-session]").count() == 0
            assert rows.nth(1).locator("[data-session]").count() == 1
            assert errors == []
        finally:
            page.close()

    def test_it_asks_first_and_revokes_only_that_one(self, server, browser):
        api = Api(sessions=[a_session("aaaa1111", current=True),
                            a_session("bbbb2222"), a_session("cccc3333")])
        page, errors = open_account(server, browser, api)
        try:
            page.locator('[data-session="bbbb2222"]').click()
            page.wait_for_selector("#overlay.on")
            page.click("#dlg-no")
            page.wait_for_selector("#overlay.on", state="hidden")
            assert api.revoked == []

            page.locator('[data-session="bbbb2222"]').click()
            page.wait_for_selector("#overlay.on")
            page.click("#dlg-yes")
            page.wait_for_function(
                "document.querySelectorAll('[data-session]').length === 1"
            )
            assert api.revoked == ["bbbb2222"]
            assert api.purges == 0
            assert errors == []
        finally:
            page.close()

    def test_the_revoke_button_names_the_device(self, server, browser):
        api = Api(sessions=[a_session("aaaa1111", current=True),
                            a_session("bbbb2222", agent=FIREFOX)])
        page, errors = open_account(server, browser, api)
        try:
            label = page.locator('[data-session="bbbb2222"]').get_attribute("aria-label")
            assert "Firefox" in label
            assert errors == []
        finally:
            page.close()


class TestTokenExpiry:
    def test_the_chosen_expiry_is_sent(self, server, browser):
        """``POST /auth/tokens`` has taken ``ttl_days`` since it was written; the
        form never offered it, so every token issued from here was immortal."""
        api = Api(sessions=[a_session("aaaa1111", current=True)])
        page, errors = open_account(server, browser, api)
        try:
            page.fill("#token-name", "ci")
            page.select_option("#token-ttl", "30")
            page.click("#token-new")
            page.wait_for_function(
                "document.getElementById('token-result').textContent.length > 0"
            )
            assert api.created[0] == {"name": "ci", "ttl_days": 30}
            assert errors == []
        finally:
            page.close()

    def test_no_expiry_sends_no_ttl_rather_than_zero(self, server, browser):
        api = Api(sessions=[a_session("aaaa1111", current=True)])
        page, errors = open_account(server, browser, api)
        try:
            page.fill("#token-name", "forever")
            page.select_option("#token-ttl", "")
            page.click("#token-new")
            page.wait_for_function(
                "document.getElementById('token-result').textContent.length > 0"
            )
            assert api.created[0] == {"name": "forever"}
            assert errors == []
        finally:
            page.close()
