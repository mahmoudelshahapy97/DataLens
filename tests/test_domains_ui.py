"""The business-domains console tab, driven in a browser.

Five endpoints shipped with the feature and nothing reached them: ``grep -i domain``
over the frontend returned nothing, so the only way to describe a domain was raw
SQL. The model reads all of it -- name, description, terminology, membership -- so
an unreachable editor meant the feature was effectively off.

What is asserted here is that each of those five endpoints is now reachable, that
membership is offered against the workspace's own catalog rather than as free text
(the backend refuses the whole call on one unknown table), and that the screen says
plainly that membership is not an access control.
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public"

ROUTES = {
    "/admin/": PUBLIC / "admin/index.html",
    "/admin/index.html": PUBLIC / "admin/index.html",
    "/assets/console.js": PUBLIC / "assets/console.js",
    "/assets/console.css": PUBLIC / "assets/console.css",
    "/assets/shared/core.js": PUBLIC / "assets/shared/core.js",
    "/assets/shared/dialogs.js": PUBLIC / "assets/shared/dialogs.js",
    "/admin/locales/en.json": PUBLIC / "admin/locales/en.json",
    "/admin/locales/ar.json": PUBLIC / "admin/locales/ar.json",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

ME = {
    "user": {"id": "u1", "email": "admin@acme.test", "name": "Admin", "role": "admin"},
    "tenant": {"id": "acme", "name": "Acme"},
    "memberships": ["acme"],
    "is_platform_admin": False,
    "is_admin": True,
    "control_plane": True,
    "deployment_mode": "test",
}

#: What `GET /grants` reports as the workspace's catalog, which is where the
#: membership checkboxes come from.
RESOURCES = [
    {"schema": "public", "table": "public.invoices", "columns": []},
    {"schema": "public", "table": "public.customers", "columns": []},
    {"schema": "public", "table": "public.tracks", "columns": []},
]


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        source = ROUTES.get(path)
        if source is None or not source.exists():
            self.send_error(404)
            return
        body = source.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", CONTENT_TYPES.get(source.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class Api:
    """Stateful enough that a save shows up on the next repaint."""

    def __init__(self, domains=None):
        self.domains = list(domains or [])
        self.calls = []  # (method, path, body)
        self._next = 100

    def install(self, page):
        def handle(route, request):
            url, method = request.url, request.method
            path = url.split("/api/", 1)[-1].split("?")[0]

            if "/me" in url:
                return self._json(route, ME)
            if "/domains" in url:
                self.calls.append((method, path, request.post_data_json))
                return self._domains(route, request, path)
            if "/grants" in url:
                return self._json(
                    route, {"resources": RESOURCES, "tables": [], "columns": [],
                            "roles": ["admin"], "version": 1},
                )
            return self._json(route, {})

        page.route("**/api/vanna/v2/**", handle)

    def _domains(self, route, request, path):
        method = request.method
        tail = path.split("/domains", 1)[-1].strip("/")
        body = request.post_data_json or {}

        if method == "GET":
            return self._json(
                route, {"domains": self.domains, "data_source": "postgresql://wh/chinook"}
            )
        if method == "POST":
            self._next += 1
            self.domains.append({
                "id": str(self._next), "name": body.get("name", ""),
                "description": body.get("description", ""),
                "terminology": body.get("terminology", {}),
                "tables": [], "is_enabled": True,
                "data_source_id": "postgresql://wh/chinook",
            })
            return self._json(route, self.domains[-1], status=201)
        if method == "PATCH":
            for domain in self.domains:
                if domain["id"] == tail:
                    domain.update({k: v for k, v in body.items() if v is not None})
            return self._json(route, {})
        if method == "PUT":
            target = tail.split("/")[0]
            for domain in self.domains:
                if domain["id"] == target:
                    domain["tables"] = body.get("tables", [])
            return self._json(
                route,
                next((d for d in self.domains if d["id"] == target), {}),
            )
        self.domains = [d for d in self.domains if d["id"] != tail]
        return self._json(route, {"deleted": tail})

    @staticmethod
    def _json(route, payload, status=200):
        return route.fulfill(
            status=status, content_type="application/json", body=json.dumps(payload)
        )

    def call_paths(self, method):
        return [path for verb, path, _ in self.calls if verb == method]


def a_domain(identifier="1", *, name="Sales", tables=(), terminology=None, enabled=True):
    return {
        "id": identifier,
        "name": name,
        "description": "Orders and the customers they belong to.",
        "terminology": dict(terminology or {}),
        "tables": list(tables),
        "is_enabled": enabled,
        "data_source_id": "postgresql://wh/chinook",
    }


def open_domains(server, api):
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1200})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    api.install(page)
    page.goto(f"{server}/admin/", wait_until="networkidle")
    page.wait_for_selector("#tabs button")
    page.click('#tabs button[data-tab="domains"]')
    page.wait_for_selector("#dom-new")

    def close():
        browser.close()
        playwright.stop()

    return page, errors, close


class TestTheTabExists:
    def test_it_lists_the_workspaces_domains(self, server):
        api = Api([a_domain(name="Sales", tables=["public.invoices"]),
                   a_domain("2", name="Support")])
        page, errors, close = open_domains(server, api)
        try:
            body = page.locator("#content").inner_text()
            assert "Sales" in body and "Support" in body
            assert errors == []
        finally:
            close()

    def test_it_says_membership_is_not_an_access_control(self, server):
        """A screen listing tables per group invites the other reading, and the
        store's own docstring is emphatic that this steers retrieval only."""
        page, errors, close = open_domains(server, Api([a_domain()]))
        try:
            assert "not an access control" in page.locator(".banner").inner_text()
            assert errors == []
        finally:
            close()

    def test_an_empty_workspace_says_what_to_do(self, server):
        page, errors, close = open_domains(server, Api([]))
        try:
            assert "No domains yet" in page.locator("#content").inner_text()
            assert errors == []
        finally:
            close()


class TestCreatingAndEditing:
    def test_a_new_domain_posts_name_description_and_terms(self, server):
        api = Api([])
        page, errors, close = open_domains(server, api)
        try:
            page.click("#dom-new")
            page.wait_for_selector("#dom-name")
            page.fill("#dom-name", "Sales")
            page.fill("#dom-desc", "Orders and invoices.")
            page.fill("#dom-terms .term-key", "churn")
            page.fill("#dom-terms .term-value", "no order in 90 days")
            page.click("#dom-save")
            page.wait_for_function(
                "document.getElementById('content').textContent.includes('Sales')"
            )

            posted = [body for verb, _, body in api.calls if verb == "POST"]
            assert posted[0]["name"] == "Sales"
            assert posted[0]["description"] == "Orders and invoices."
            assert posted[0]["terminology"] == {"churn": "no order in 90 days"}
            assert errors == []
        finally:
            close()

    def test_a_half_filled_term_is_dropped_rather_than_stored_blank(self, server):
        """A term with no meaning reaches the prompt as noise."""
        api = Api([])
        page, errors, close = open_domains(server, api)
        try:
            page.click("#dom-new")
            page.wait_for_selector("#dom-name")
            page.fill("#dom-name", "Sales")
            page.fill("#dom-terms .term-key", "churn")
            page.click("#dom-save")
            page.wait_for_function(
                "document.getElementById('content').textContent.includes('Sales')"
            )

            posted = [body for verb, _, body in api.calls if verb == "POST"]
            assert posted[0]["terminology"] == {}
            assert errors == []
        finally:
            close()

    def test_editing_prefills_and_patches(self, server):
        api = Api([a_domain(terminology={"churn": "no order in 90 days"})])
        page, errors, close = open_domains(server, api)
        try:
            page.click('[data-dom-edit="0"]')
            page.wait_for_selector("#dom-name")
            assert page.locator("#dom-name").input_value() == "Sales"
            assert page.locator("#dom-terms .term-key").first.input_value() == "churn"

            page.fill("#dom-name", "Revenue")
            page.click("#dom-save")
            page.wait_for_function(
                "document.getElementById('content').textContent.includes('Revenue')"
            )

            patched = [body for verb, _, body in api.calls if verb == "PATCH"]
            assert patched[0]["name"] == "Revenue"
            assert errors == []
        finally:
            close()

    def test_a_domain_without_a_name_is_not_sent(self, server):
        api = Api([])
        page, errors, close = open_domains(server, api)
        try:
            page.click("#dom-new")
            page.wait_for_selector("#dom-name")
            page.click("#dom-save")
            page.wait_for_timeout(200)
            assert api.call_paths("POST") == []
            assert errors == []
        finally:
            close()

    def test_enabling_and_disabling_is_one_click(self, server):
        api = Api([a_domain(enabled=True)])
        page, errors, close = open_domains(server, api)
        try:
            page.click('[data-dom-toggle="0"]')
            page.wait_for_function(
                "document.querySelector('[data-dom-toggle]')"
                ".getAttribute('aria-pressed') === 'false'"
            )
            patched = [body for verb, _, body in api.calls if verb == "PATCH"]
            assert patched[0] == {"is_enabled": False}
            assert errors == []
        finally:
            close()


class TestMembership:
    def test_tables_come_from_the_catalog_not_a_text_box(self, server):
        """The backend refuses the whole call on one unknown table, so a typed
        list is a form that rejects itself with no hint which line was wrong."""
        api = Api([a_domain(tables=["public.invoices"])])
        page, errors, close = open_domains(server, api)
        try:
            page.click('[data-dom-tables="0"]')
            page.wait_for_selector("#dom-table-list")

            boxes = page.locator("#dom-table-list input[type=checkbox]")
            assert boxes.count() == len(RESOURCES)
            # The one it already has is checked; the others are not.
            assert boxes.nth(0).is_checked()
            assert not boxes.nth(1).is_checked()
            assert errors == []
        finally:
            close()

    def test_saving_membership_puts_the_whole_set(self, server):
        api = Api([a_domain(tables=["public.invoices"])])
        page, errors, close = open_domains(server, api)
        try:
            page.click('[data-dom-tables="0"]')
            page.wait_for_selector("#dom-table-list")
            page.locator("#dom-table-list input[type=checkbox]").nth(1).check()
            page.click("#dom-tables-save")
            page.wait_for_function(
                "document.querySelector('#overlay') && "
                "!document.querySelector('#overlay').classList.contains('on')"
            )

            put = [body for verb, _, body in api.calls if verb == "PUT"]
            assert put[0]["tables"] == ["public.invoices", "public.customers"]
            assert errors == []
        finally:
            close()

    def test_the_list_can_be_filtered(self, server):
        api = Api([a_domain()])
        page, errors, close = open_domains(server, api)
        try:
            page.click('[data-dom-tables="0"]')
            page.wait_for_selector("#dom-filter")
            page.fill("#dom-filter", "track")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('#dom-table-list [data-table-row]'))"
                ".filter((row) => !row.hidden).length === 1"
            )
            assert errors == []
        finally:
            close()


class TestDeleting:
    def test_it_asks_first_and_says_what_survives(self, server):
        """``table_annotations.domain_id`` is ON DELETE SET NULL, so descriptions
        somebody wrote for these tables outlive the grouping."""
        api = Api([a_domain()])
        page, errors, close = open_domains(server, api)
        try:
            page.click('[data-dom-del="0"]')
            page.wait_for_selector("#overlay.on")
            sheet = page.locator("#sheet").inner_text()
            assert "Sales" in sheet
            assert "kept" in sheet

            page.click("#dlg-no")
            page.wait_for_selector("#overlay.on", state="hidden")
            assert api.call_paths("DELETE") == []

            page.click('[data-dom-del="0"]')
            page.wait_for_selector("#overlay.on")
            page.click("#dlg-yes")
            page.wait_for_function(
                "document.getElementById('content').textContent.includes('No domains yet')"
            )
            assert len(api.call_paths("DELETE")) == 1
            assert errors == []
        finally:
            close()
