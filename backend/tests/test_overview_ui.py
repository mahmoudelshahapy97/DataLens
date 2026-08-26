"""The Overview and Audit tabs, driven in a real browser.

The console has no build step and no framework, so nothing else in the suite
executes a line of it: a typo in a template literal, a handler wired to a
selector that no longer exists, or a chart handed its data as an attribute
instead of a property all ship silently. These tests load the real
``console.js`` into Chromium, stub the API with the shapes ``routes/overview.py``
actually returns, and drive the page the way an operator would.

Same harness as ``test_permissions_ui.py`` -- the container's path layout rather
than the source tree's, and a stub for the one built asset -- for the same
reasons, written out there.
"""

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

PUBLIC = Path(__file__).resolve().parents[2] / "frontend" / "public"

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
}

#: The chart element, recording what it was handed rather than drawing it.
#:
#: The real one lazy-loads over a megabyte of Plotly, which these tests neither
#: need nor should download. What is worth asserting is the *figure the console
#: built* -- and, more to the point, that it arrived at all: `data` and `layout`
#: are properties on a custom element, and assigning them before the element
#: upgrades silently leaves the chart empty. Recording them onto the DOM is how
#: a test can tell the difference.
COMPONENT_STUB = """
class PlotlyChartStub extends HTMLElement {
  connectedCallback() {
    this.classList.add('js-plotly-plot');
    this.setAttribute('data-traces', (this.data || []).map((t) => t.type).join(','));
    this.setAttribute('data-points', String(((this.data || [])[0] || {}).x?.length ?? 0));
    this.textContent = (this.data || []).map((t) => t.name || t.type).join(',');
  }
}
customElements.define('plotly-chart', PlotlyChartStub);
"""


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output readable
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/assets/vanna-components.js":
            return self._send(COMPONENT_STUB.encode(), CONTENT_TYPES[".js"])

        source = ROUTES.get(path)
        if source is None or not source.exists():
            self.send_error(404)
            return
        self._send(
            source.read_bytes(),
            CONTENT_TYPES.get(source.suffix, "application/octet-stream"),
        )

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


# ----------------------------------------------------------------------
# The API, in the shapes routes/overview.py returns
# ----------------------------------------------------------------------

SERIES = [
    {"day": f"2026-08-{day:02d}", "questions": day % 7, "succeeded": max(0, day % 7 - 1)}
    for day in range(1, 31)
]

AUDIT = [
    {"id": "a1", "actor_email": "root@example.com", "action": "member.add",
     "tenant_id": "acme", "target": "grace@acme.test", "actor_ip": "10.0.0.4",
     "details": {"role": "analyst"}, "created_at": "2026-08-25T09:00:00+00:00"},
    {"id": "a2", "actor_email": "root@example.com", "action": "tenant.rebind",
     "tenant_id": "globex", "target": "globex", "actor_ip": "10.0.0.4",
     "details": {"fields": ["database_url"]}, "created_at": "2026-08-24T09:00:00+00:00"},
]

ACCESS = [
    {"event_id": "e1", "event_type": "tool.invoked", "user_email": "ada@acme.test",
     "tool_name": "run_sql", "access_granted": True, "request_id": "r1",
     "payload": {"table": "orders"}, "created_at": "2026-08-25T10:00:00+00:00"},
    {"event_id": "e2", "event_type": "access.denied", "user_email": "eve@acme.test",
     "tool_name": "run_sql", "access_granted": False, "request_id": "r2",
     "payload": {"table": "salaries"}, "created_at": "2026-08-25T11:00:00+00:00"},
]

ACTIONS = ["member.add", "member.remove", "tenant.create", "tenant.rebind"]


def _me(platform_admin):
    return {
        "user": {"id": "u1", "email": "root@example.com", "name": "Root",
                 "role": "admin"},
        "tenant": {"id": "acme", "name": "Acme", "description": "",
                   "data_source": "pg", "allow_byo_key": True},
        "memberships": ["acme", "globex"],
        "is_platform_admin": platform_admin,
        "is_admin": True,
        "control_plane": True,
    }


class Api:
    """A stub overview API that remembers what the page asked it for."""

    def __init__(self, *, platform_admin):
        self.platform_admin = platform_admin
        self.requests = []            # every GET, in order

    def install(self, page):
        def handle(route, request):
            url = request.url
            path = url.split("/api/vanna/v2/", 1)[-1]
            self.requests.append(path)

            if "/me" in url:
                return self._json(route, _me(self.platform_admin))
            if "admin/overview" in url:
                return self._json(route, self.overview(url))
            if "admin/audit" in url:
                return self._json(route, {"events": self._filtered(url)})
            if "admin/access-log" in url:
                denied = "denied_only=true" in url
                events = [e for e in ACCESS if not denied or not e["access_granted"]]
                return self._json(route, {"events": events})
            if "admin/tenants" in url:
                return self._json(route, {"tenants": [
                    {"id": "acme", "name": "Acme"},
                    {"id": "globex", "name": "Globex"},
                ]})
            if "admin/examples" in url:
                return self._json(route, {"examples": [
                    {"id": "x1", "status": "verified", "question": "q"},
                    {"id": "x2", "status": "candidate", "question": "q"},
                    {"id": "x3", "status": "candidate", "question": "q"},
                ]})
            return self._json(route, {})

        page.route("**/api/vanna/v2/**", handle)

    @staticmethod
    def _filtered(url):
        if "action=member.add" in url:
            return [e for e in AUDIT if e["action"] == "member.add"]
        return AUDIT

    def overview(self, url):
        scoped = "tenant_id=" in url
        payload = {
            "scope": "acme" if scoped else "",
            "window_days": 90 if "days=90" in url else 30,
            "series": SERIES,
            "recent": AUDIT,
            "actions": ACTIONS,
        }
        if scoped:
            payload["kpis"] = {
                "workspaces": 1, "members": 4, "questions": 120, "succeeded": 114,
                "success_rate": 0.95, "active_users": 3, "liked": 20, "disliked": 2,
                "last_activity": "2026-08-25T09:00:00+00:00",
            }
        else:
            payload["kpis"] = {
                "workspaces": 2, "active_workspaces": 2, "members": 7,
                "questions": 300, "succeeded": 270, "success_rate": 0.9,
                "active_users": 5, "liked": 30, "disliked": 4,
                "last_activity": "2026-08-25T09:00:00+00:00",
            }
            # Nested under `usage`, as list_tenants_with_usage really returns it.
            payload["workspaces"] = [
                {"id": "acme", "name": "Acme", "is_active": True,
                 "usage": {"tenant_id": "acme", "window_days": 30, "members": 4,
                           "questions": 200, "succeeded": 190, "liked": 20,
                           "disliked": 2, "active_users": 3,
                           "last_activity": "2026-08-25T09:00:00+00:00"}},
                {"id": "globex", "name": "Globex", "is_active": False,
                 "usage": {"tenant_id": "globex", "window_days": 30, "members": 3,
                           "questions": 100, "succeeded": 80, "liked": 10,
                           "disliked": 2, "active_users": 2,
                           "last_activity": None}},
            ]

        if self.platform_admin:
            payload["kpis"]["cost_usd"] = 48.29
            payload["spend"] = {"cost_usd": 48.29, "window_days": 30,
                                "prompt_tokens": 10, "completion_tokens": 5,
                                "questions": 300, "by_model": []}
            payload["data_sources"] = [
                {"tenant_id": "globex", "data_source_id": "b:5432/two",
                 "label": "warehouse", "last_ok": False,
                 "last_checked_at": "2026-08-25T08:00:00+00:00",
                 "last_error": "connection refused"},
                {"tenant_id": "acme", "data_source_id": "a:5432/one",
                 "label": "unchecked", "last_ok": None,
                 "last_checked_at": None, "last_error": None},
            ]
        return payload

    @staticmethod
    def _json(route, body):
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps(body))


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        chromium = p.chromium.launch()
        yield chromium
        chromium.close()


def _open(server, browser, *, platform_admin):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    api = Api(platform_admin=platform_admin)
    api.install(page)
    page.add_init_script(
        "localStorage.setItem('vanna.identity',"
        " JSON.stringify({tenant:'acme', email:'root@example.com'}));"
    )
    page.goto(f"{server}/admin/index.html")
    page.wait_for_selector(".kpi-row")
    return page, api, errors


@pytest.fixture
def platform(server, browser):
    """The console as a platform admin, which lands on the overview."""
    page, api, errors = _open(server, browser, platform_admin=True)
    yield page, api, errors
    page.close()


@pytest.fixture
def workspace(server, browser):
    """The console as a workspace admin."""
    page, api, errors = _open(server, browser, platform_admin=False)
    yield page, api, errors
    page.close()


# ----------------------------------------------------------------------
# The overview
# ----------------------------------------------------------------------


class TestTheOverviewLands:
    def test_it_is_the_tab_the_console_opens_on(self, platform):
        """It opened on the review queue, so "is anything wrong" meant reading tabs."""
        page, _, _ = platform
        tab = page.get_by_role("tab", name="Overview")
        assert tab.get_attribute("aria-selected") == "true"

    def test_it_loads_without_a_script_error(self, platform):
        _page, _api, errors = platform
        assert errors == []

    def test_the_headline_numbers_are_rendered(self, platform):
        # .kpi .k is uppercased by the stylesheet, and inner_text() reports what
        # is rendered rather than what the template wrote.
        page, _, _ = platform
        text = page.locator(".kpi-row").inner_text()
        assert "workspaces" in text.lower()
        assert "2 active" in text        # the note under the workspace count
        assert "300" in text             # questions, grouped by locale
        assert "90.0%" in text           # success_rate, as a percentage
        assert "270 succeeded" in text
        assert "$48.29" in text          # spend, as currency

    def test_a_workspace_admin_is_not_shown_cost(self, workspace):
        """Omitted rather than zeroed -- the two mean different things."""
        page, _, errors = workspace
        assert errors == []
        text = page.locator(".kpi-row").inner_text()
        assert "$" not in text
        assert "members" in text.lower()  # its first tile, in place of Workspaces
        assert "workspaces" not in text.lower()

    def test_both_charts_receive_their_figures_as_properties(self, platform):
        """An attribute would arrive as "[object Object]" and draw nothing."""
        page, _, _ = platform
        series = page.locator("#ov-series plotly-chart")
        series.wait_for()
        assert series.get_attribute("data-traces") == "scatter,scatter"
        assert series.get_attribute("data-points") == str(len(SERIES))

        breakdown = page.locator("#ov-breakdown plotly-chart")
        breakdown.wait_for()
        assert breakdown.get_attribute("data-traces") == "bar"

    def test_the_recent_activity_feed_lists_what_happened(self, platform):
        page, _, _ = platform
        feed = page.locator(".feed").inner_text()
        assert "member.add" in feed
        assert "grace@acme.test" in feed

    def test_clicking_an_activity_row_opens_the_audit_tab(self, platform):
        page, _, _ = platform
        page.locator(".feed button").first.click()
        assert page.get_by_role("tab", name="Audit").get_attribute("aria-selected") == "true"

    def test_an_unchecked_database_does_not_read_as_healthy(self, platform):
        """`last_ok` is tri-state and the unknown case is the dangerous one."""
        page, _, _ = platform
        card = page.locator(".card", has_text="Data sources")
        health = card.inner_text().lower()
        assert "failing" in health
        assert "connection refused" in health
        assert "unchecked" in health
        # Asserted on the tag itself rather than the word: "healthy" is the one
        # state neither of these sources is in, and the tag is what says so.
        assert card.locator(".tag.active").count() == 0

    def test_the_per_workspace_table_is_sorted_by_volume(self, platform):
        page, _, _ = platform
        rows = page.locator(".card", has_text="By workspace").locator("tbody tr")
        assert "Acme" in rows.nth(0).inner_text()
        assert "Globex" in rows.nth(1).inner_text()

        # The counts themselves, not just the order. They live under `usage`,
        # and read from the wrong level they render as 0 -- which still sorts,
        # still fills the table, and is wrong in a way order alone cannot show.
        acme = rows.nth(0).inner_text()
        assert "200" in acme and "4" in acme
        assert "95.0%" in acme
        assert "inactive" in rows.nth(1).inner_text().lower()
        assert "never" in rows.nth(1).inner_text()

    def test_a_workspace_admin_gets_the_knowledge_panel_instead(self, workspace):
        page, _, _ = workspace
        knowledge = page.locator(".card", has_text="Knowledge")
        assert knowledge.count() == 1
        text = knowledge.inner_text()
        assert "1" in text and "verified" in text
        assert "awaiting review" in text


class TestTheOverviewControls:
    def test_changing_the_window_refetches(self, platform):
        page, api, _ = platform
        page.select_option("#dash-days", "90")
        page.wait_for_function(
            "() => document.querySelector('.kpi-row').innerText.includes('90')"
        )
        assert any("days=90" in request for request in api.requests)

    def test_choosing_a_workspace_scopes_the_request(self, platform):
        page, api, _ = platform
        page.select_option("#dash-scope", "globex")
        page.wait_for_selector(".card:has-text('Knowledge'), #ov-breakdown plotly-chart")
        assert any("tenant_id=globex" in request for request in api.requests)

    def test_a_workspace_admin_has_no_workspace_picker(self, workspace):
        """There is nothing to pick, and the server would refuse the attempt."""
        page, _, _ = workspace
        assert page.locator("#dash-scope").count() == 0

    def test_auto_refresh_stops_when_the_tab_is_left(self, platform):
        """A poll that outlives its panel writes into a detached node."""
        page, api, errors = platform
        page.check("#dash-auto")
        page.get_by_role("tab", name="Audit").click()
        page.wait_for_selector("table.data")

        before = len(api.requests)
        page.wait_for_timeout(1200)
        assert len(api.requests) == before
        assert errors == []


# ----------------------------------------------------------------------
# The audit trail
# ----------------------------------------------------------------------


def _show_access_log(page):
    """Switch to the access log and wait for it to actually be on screen.

    renderAudit is async, so the admin table is still rendered when the click
    returns -- waiting for `table.data` matches the table being left behind.
    `Tool` is a column only the access log has.
    """
    page.get_by_role("button", name="Access log").click()
    page.wait_for_selector("table.data th:text-is('Tool')")


@pytest.fixture
def audit(platform):
    page, api, errors = platform
    page.get_by_role("tab", name="Audit").click()
    page.wait_for_selector("table.data")
    # Also wait for the action filter to have its options.
    #
    # The tab paints in two stages: the table lands as soon as the audit fetch
    # returns, but the filter is built from the vocabulary the *overview* fetch
    # carries, and under a full-suite run that one can still be in flight. In
    # isolation it had always arrived first, so tests reading the filter passed
    # alone and failed in the suite -- the fixture was yielding a half-painted
    # panel and calling it ready.
    page.wait_for_function("() => document.querySelectorAll('#audit-action option').length > 1")
    return page, api, errors


class TestTheAuditTrail:
    def test_it_lists_what_operators_did(self, audit):
        page, _, errors = audit
        assert errors == []
        text = page.locator("table.data").inner_text()
        assert "member.add" in text
        assert "tenant.rebind" in text
        assert "10.0.0.4" in text

    def test_the_action_filter_is_served_not_guessed(self, audit):
        """Hardcoding the vocabulary is how it drifts from what the writer takes."""
        page, _, _ = audit
        options = page.locator("#audit-action option").all_inner_texts()
        assert options[0] == "All actions"
        assert options[1:] == ACTIONS

    def test_filtering_by_action_narrows_the_table(self, audit):
        page, api, _ = audit
        page.select_option("#audit-action", "member.add")
        page.wait_for_function(
            "() => document.querySelectorAll('table.data tbody tr').length === 1"
        )
        assert any("action=member.add" in request for request in api.requests)
        assert "tenant.rebind" not in page.locator("table.data").inner_text()

    def test_a_row_expands_to_the_details_it_recorded(self, audit):
        page, _, _ = audit
        page.locator("tr.expandable").first.click()
        detail = page.locator("tr.detail pre")
        detail.wait_for()
        assert '"role": "analyst"' in detail.inner_text()

    def test_the_access_log_is_a_separate_reading(self, audit):
        page, _, errors = audit
        _show_access_log(page)
        assert errors == []

        # The outcome tags are uppercased by the stylesheet.
        text = page.locator("table.data").inner_text().lower()
        assert "run_sql" in text
        assert "refused" in text
        assert "allowed" in text

    def test_the_access_log_says_which_workspace_it_is_showing(self, audit):
        """It is per workspace by nature; "all workspaces" has no reading."""
        page, _, _ = audit
        _show_access_log(page)
        assert "acme" in page.locator(".banner").inner_text()

    def test_refused_only_filters_to_the_denials(self, audit):
        page, api, _ = audit
        _show_access_log(page)
        page.check("#audit-denied")
        page.wait_for_function(
            "() => document.querySelectorAll('table.data tbody tr').length === 1"
        )
        assert any("denied_only=true" in request for request in api.requests)
        assert "allowed" not in page.locator("table.data").inner_text().lower()

    def test_reset_clears_the_filters(self, audit):
        page, _, _ = audit
        page.select_option("#audit-action", "member.add")
        page.wait_for_function(
            "() => document.querySelectorAll('table.data tbody tr').length === 1"
        )
        page.get_by_role("button", name="Reset").click()
        page.wait_for_function(
            "() => document.querySelectorAll('table.data tbody tr').length === 2"
        )
        assert page.locator("#audit-action").input_value() == ""

    def test_there_is_no_export_button_on_the_access_log(self, audit):
        """There is no CSV route for it, so offering one would be a dead button."""
        page, _, _ = audit
        assert page.locator("#audit-export").count() == 1
        _show_access_log(page)
        # Waited for rather than asserted immediately: switching sub-views is an
        # async repaint, so reading the count on the tick after the click can
        # still see the panel being left behind.
        page.wait_for_function("() => !document.querySelector('#audit-export')")
        assert page.locator("#audit-export").count() == 0
