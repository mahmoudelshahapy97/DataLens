"""The Permissions page, driven in a real browser.

The admin console has no build step and no framework, so nothing else in the
suite executes a line of it -- a typo in a template literal or a handler wired to
the wrong selector ships silently. These tests load the real ``console.js`` into
Chromium, stub the API, and drive the page the way an administrator would.

Two details make the harness faithful rather than approximate:

* **The container's path layout, not the source tree's.** ``console.js`` imports
  ``./shared/core.js``, which resolves only because the Dockerfile lands both
  under ``/assets/``. Serving the source tree directly would 404 on that import,
  so the server below maps paths the way ``frontend/nginx.conf`` does. That
  makes this a test of what actually ships.
* **The API is stubbed, not mocked out.** Requests are intercepted and answered
  with the shapes ``routes/grants.py`` really returns, and every mutation is
  recorded -- so an assertion can check the exact request body the UI sent.
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

#: Served path -> file on disk. Very nearly a plain join now: `frontend/public/`
#: is the document root and the build copies it into the image untouched, so the
#: URL is the path. `/admin/` is the one entry that is not, because a directory
#: URL resolves to its index.
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

ME = {
    "user": {"id": "u1", "email": "admin@acme.test", "name": "Admin", "role": "admin"},
    "tenant": {"id": "acme", "name": "Acme", "description": "", "data_source": "pg",
               "allow_byo_key": True},
    "memberships": ["acme"],
    "is_platform_admin": False,
    "is_admin": True,
    "control_plane": True,
    "deployment_mode": "test",
}

RESOURCES = [
    {"schema": "erp", "table": "erp.customers", "columns": [
        {"name": "customer_id", "data_type": "int8",
         "is_primary_key": True, "is_generated": True},
        {"name": "email", "data_type": "text",
         "is_primary_key": False, "is_generated": False},
        {"name": "region", "data_type": "text",
         "is_primary_key": False, "is_generated": False},
    ]},
    {"schema": "erp", "table": "erp.orders", "columns": [
        {"name": "order_id", "data_type": "int8",
         "is_primary_key": True, "is_generated": True},
        {"name": "status", "data_type": "text",
         "is_primary_key": False, "is_generated": False},
    ]},
]


PRESETS = [
    {"name": "none", "title": "No access", "description": "Grants nothing.",
     "table": {"can_read": False}, "column": {"can_read": False}},
    {"name": "viewer", "title": "Viewer", "description": "Read and aggregate.",
     "table": {"can_read": True}, "column": {"can_read": True}},
    {"name": "analyst", "title": "Analyst", "description": "Full read access.",
     "table": {"can_read": True}, "column": {"can_read": True}},
    {"name": "admin", "title": "Administrator", "description": "Read and write.",
     "table": {"can_read": True}, "column": {"can_read": True}},
]


#: The chart element, which lives in the built bundle rather than in `public/`.
#:
#: The console's overview tab draws with `<plotly-chart>`, so `admin/index.html`
#: loads `/assets/vanna-components.js` -- a file Vite emits at build time and
#: which therefore has no source under `public/` for this server to map. Stubbed
#: rather than skipped: an unmapped path 404s into a console error, and this
#: suite fails a page that logs one. Same treatment, and same reasoning, as the
#: chat element in test_ask_ui.py.
COMPONENT_STUB = """
class PlotlyChartStub extends HTMLElement {
  connectedCallback() {
    this.classList.add('js-plotly-plot');
    this.textContent = (this.data || []).map((trace) => trace.type).join(',');
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
            body = COMPONENT_STUB.encode()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES[".js"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

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
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class Api:
    """A stub grants API that remembers what the page asked it to do."""

    def __init__(self):
        self.tables = {}       # (role, table) -> flags
        self.columns = {}      # (role, table, column) -> flags
        self.version = 0
        self.requests = []     # every mutation, in order
        self.policy = {}       # role -> its default

    def payload(self, role):
        return {
            "version": self.version,
            "roles": ["admin", "analyst", "viewer"],
            "resources": RESOURCES,
            "tables": [
                {"role": r, "table": t, **flags}
                for (r, t), flags in self.tables.items() if r == role
            ],
            "columns": [
                {"role": r, "table": t, "column": c, **flags}
                for (r, t, c), flags in self.columns.items() if r == role
            ],
        }

    def install(self, page):
        def handle(route, request):
            url = request.url
            if "/me" in url:
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(ME))
            if "/grants/presets" in url:
                return route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"presets": PRESETS}),
                )
            if "/grants/policy" in url:
                if request.method == "GET":
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"data_source": "wh", "version": self.version,
                                         "roles": self.policy}),
                    )
                body = request.post_data_json
                self.requests.append(("policy", body))
                for role, entry in (body.get("roles") or {}).items():
                    self.policy[role] = {
                        **entry, "last_applied_at": None, "last_applied_version": None
                    }
                if body.get("apply"):
                    self.version += 1
                return route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"version": self.version, "applied": {}}),
                )
            if url.rstrip("/").endswith("grants") or "grants?" in url:
                role = "analyst"
                if "role=" in url:
                    role = url.split("role=")[1].split("&")[0]
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(self.payload(role)))
            if "/grants/table" in url:
                body = request.post_data_json
                self.requests.append(("table", body))
                self.version += 1
                self.tables[(body["role"], body["table"])] = {
                    "can_read": body.get("can_read", False),
                    "can_insert": body.get("can_insert", False),
                    "can_update": body.get("can_update", False),
                    "can_delete": body.get("can_delete", False),
                }
                # Mirror the server's autofill exactly, provenance included:
                # rows autofill owns follow the table's access level, rows a
                # person set are left alone. Getting this wrong in the stub
                # would hide the bug it was written to catch.
                if body.get("can_read") and body.get("autofill_columns"):
                    writable = body.get("can_insert") or body.get("can_update")
                    for resource in RESOURCES:
                        if resource["table"] != body["table"]:
                            continue
                        for column in resource["columns"]:
                            key = (body["role"], body["table"], column["name"])
                            can_write = bool(writable) and not column["is_generated"]
                            existing = self.columns.get(key)
                            if existing and existing.get("granted_by") != "autofill":
                                continue
                            self.columns[key] = {
                                "can_read": True, "can_filter": True,
                                "can_aggregate": True, "can_write": can_write,
                                "granted_by": "autofill",
                            }
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps({"version": self.version}))
            if "/grants/column" in url:
                body = request.post_data_json
                self.requests.append(("column", body))
                self.version += 1
                # Store what was sent, not a fixed shape: the endpoint replaces
                # the row, so hard-coding filter/aggregate true would hide exactly
                # the bug where clearing one of them is silently dropped.
                self.columns[(body["role"], body["table"], body["column"])] = {
                    "can_read": body.get("can_read", False),
                    "can_filter": body.get("can_filter", False),
                    "can_aggregate": body.get("can_aggregate", False),
                    "can_write": body.get("can_write", False),
                    "granted_by": ME["user"]["email"],
                }
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps({"version": self.version}))
            # Everything else the console loads at boot.
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({}))

        page.route("**/api/**", handle)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        chromium = p.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def console(server, browser):
    """The console, signed in, on the Permissions tab."""
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    api = Api()
    api.install(page)
    page.add_init_script(
        "localStorage.setItem('vanna.identity',"
        " JSON.stringify({tenant:'acme', email:'admin@acme.test'}));"
    )
    page.goto(f"{server}/admin/index.html")
    page.get_by_role("tab", name="Permissions").click()
    page.wait_for_selector("table.data")

    yield page, api, errors
    page.close()


class TestThePageRenders:
    def test_it_loads_without_a_script_error(self, console):
        """The console has no build step, so nothing else executes this file."""
        page, _, errors = console
        assert errors == []

    def test_the_tab_exists_and_is_selected(self, console):
        page, _, _ = console
        tab = page.get_by_role("tab", name="Permissions")
        assert tab.get_attribute("aria-selected") == "true"

    def test_every_catalog_table_is_listed_even_ungranted_ones(self, console):
        page, _, _ = console
        rows = page.locator("table.data tbody tr")
        text = rows.all_inner_texts()
        assert any("erp.customers" in row for row in text)
        assert any("erp.orders" in row for row in text)

    def test_tables_start_at_no_access(self, console):
        """Default deny, shown as such rather than left blank."""
        page, _, _ = console
        buttons = page.locator("button.perm")
        assert buttons.count() == 2
        for i in range(buttons.count()):
            assert buttons.nth(i).inner_text().strip() == "No access"

    def test_keys_are_named_so_the_row_explains_itself(self, console):
        page, _, _ = console
        row = page.locator("tbody tr", has_text="erp.customers").first
        assert "customer_id" in row.inner_text()


class TestCyclingAccess:
    def _button(self, page, table):
        return page.locator(f'button.perm[data-table="{table}"]')

    def test_first_click_grants_read_only(self, console):
        page, api, _ = console
        self._button(page, "erp.customers").click()
        page.wait_for_selector('button.perm[data-table="erp.customers"].perm--read')

        kind, body = api.requests[-1]
        assert kind == "table"
        assert body["table"] == "erp.customers" and body["role"] == "analyst"
        assert body["can_read"] is True
        assert not any(body[v] for v in ("can_insert", "can_update", "can_delete"))

    def test_second_click_grants_read_and_write(self, console):
        page, api, _ = console
        page.on("dialog", lambda d: d.accept())

        self._button(page, "erp.customers").click()
        page.wait_for_selector('button.perm[data-table="erp.customers"].perm--read')
        self._button(page, "erp.customers").click()
        page.wait_for_selector('button.perm[data-table="erp.customers"].perm--write')

        _, body = api.requests[-1]
        assert all(body[v] for v in
                   ("can_read", "can_insert", "can_update", "can_delete"))
        assert self._button(page, "erp.customers").inner_text().strip() == "Read & write"

    def test_granting_write_asks_first(self, console):
        """The one destructive-ish action on the page confirms, like allow_writes."""
        page, api, _ = console
        seen = []
        page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))

        self._button(page, "erp.customers").click()          # -> read, no dialog
        page.wait_for_selector('button.perm[data-table="erp.customers"].perm--read')
        before = len(api.requests)
        self._button(page, "erp.customers").click()          # -> write, dialog
        page.wait_for_timeout(150)

        assert seen and "erp.customers" in seen[0]
        assert len(api.requests) == before, "dismissing must not send the grant"

    def test_third_click_returns_to_no_access(self, console):
        page, api, _ = console
        page.on("dialog", lambda d: d.accept())
        for expected in ("read", "write", "none"):
            self._button(page, "erp.customers").click()
            page.wait_for_selector(
                f'button.perm[data-table="erp.customers"].perm--{expected}')

        _, body = api.requests[-1]
        assert not any(body[v] for v in
                       ("can_read", "can_insert", "can_update", "can_delete"))

    def test_each_table_is_granted_independently(self, console):
        """Per table, not per database -- the whole point of the page."""
        page, _, _ = console
        self._button(page, "erp.customers").click()
        page.wait_for_selector('button.perm[data-table="erp.customers"].perm--read')
        assert "perm--none" in (
            self._button(page, "erp.orders").get_attribute("class"))

    def test_the_control_is_announced_to_a_screen_reader(self, console):
        """A cycling button is unreadable without saying what the next click does."""
        page, _, _ = console
        label = self._button(page, "erp.customers").get_attribute("aria-label")
        assert "No access" in label and "Read only" in label


class TestColumnPermissions:
    """Per-column read / filter / aggregate / write.

    The console used to offer a single write tick, and only once the whole table
    was writable -- so the three read-shaped flags the grant model has always
    carried were unreachable from any screen. They are enforced in SQL now, which
    makes them worth setting.
    """

    def _expand(self, page, table="erp.customers"):
        page.locator(f'button.linkish[data-expand="{table}"]').click()

    def _grant_read(self, page, table="erp.customers"):
        """Cycle a table to Read and open its column list."""
        page.on("dialog", lambda d: d.accept())
        button = page.locator(f'button.perm[data-table="{table}"]')
        button.click()
        page.wait_for_selector(f'button.perm[data-table="{table}"].perm--read')
        self._expand(page, table)
        page.wait_for_selector("table.perm-cols")

    def _box(self, page, column, use):
        return page.locator(f'input[data-column="{column}"][data-col-use="{use}"]')

    def test_an_ungranted_table_says_what_to_do_first(self, console):
        page, _, _ = console
        self._expand(page)
        assert page.locator("table.perm-cols").count() == 0
        assert "read access first" in page.inner_text("tbody")

    def test_a_readable_table_lists_every_column_and_use(self, console):
        page, _, _ = console
        self._grant_read(page)

        assert self._box(page, "email", "can_read").count() == 1
        assert self._box(page, "email", "can_filter").count() == 1
        assert self._box(page, "email", "can_aggregate").count() == 1
        assert self._box(page, "email", "can_write").count() == 1

    def test_write_is_unavailable_until_the_table_is_writable(self, console):
        """Ticking it could only ever produce a statement the validator refuses."""
        page, _, _ = console
        self._grant_read(page)

        assert self._box(page, "email", "can_write").is_disabled()
        assert not self._box(page, "email", "can_read").is_disabled()

    def test_a_generated_column_can_never_be_written(self, console):
        """"Why is that greyed out" is a better question than "where is it"."""
        page, _, _ = console
        self._grant_read(page)

        assert self._box(page, "customer_id", "can_write").is_disabled()
        # ...but it is still readable, which is the common case for a key.
        assert not self._box(page, "customer_id", "can_read").is_disabled()

    def test_granting_filter_implies_read(self, console):
        """The database CHECK refuses filter without read, so the UI must not offer it."""
        page, api, _ = console
        self._grant_read(page)
        self._box(page, "email", "can_read").uncheck()
        page.wait_for_timeout(250)

        self._box(page, "email", "can_filter").check()
        page.wait_for_timeout(250)

        kind, body = api.requests[-1]
        assert kind == "column"
        assert body["can_filter"] is True, api.requests
        assert body["can_read"] is True, api.requests

    def test_clearing_read_clears_everything_below_it(self, console):
        page, api, _ = console
        self._grant_read(page)

        self._box(page, "email", "can_read").uncheck()
        page.wait_for_timeout(250)

        kind, body = api.requests[-1]
        assert kind == "column"
        assert body["can_read"] is False
        assert body["can_filter"] is False
        assert body["can_aggregate"] is False
        assert body["can_write"] is False

    def test_the_other_flags_are_sent_unchanged(self, console):
        """The endpoint replaces the row rather than patching one field.

        Sending only the flag that moved would silently clear the rest.
        """
        page, api, _ = console
        self._grant_read(page)

        self._box(page, "email", "can_aggregate").uncheck()
        page.wait_for_timeout(250)

        _, body = api.requests[-1]
        assert body["can_aggregate"] is False
        assert body["can_read"] is True
        assert set(body) >= {"can_read", "can_filter", "can_aggregate", "can_write"}

    def test_a_cleared_flag_survives_a_repaint(self, console):
        """Collapsing and reopening must not read a stale grant back."""
        page, _, _ = console
        self._grant_read(page)
        self._box(page, "email", "can_aggregate").uncheck()
        page.wait_for_timeout(250)

        self._expand(page)          # collapse
        self._expand(page)          # reopen
        page.wait_for_selector("table.perm-cols")

        assert not self._box(page, "email", "can_aggregate").is_checked()
        assert self._box(page, "email", "can_read").is_checked()


class TestFilteringAndRoles:
    def test_the_filter_narrows_by_table_name(self, console):
        page, _, _ = console
        page.fill("#perm-filter", "orders")
        page.wait_for_timeout(120)
        rows = page.locator("table.data tbody tr").all_inner_texts()
        assert any("erp.orders" in r for r in rows)
        assert not any("erp.customers" in r for r in rows)

    def test_the_filter_also_matches_a_column_name(self, console):
        page, _, _ = console
        page.fill("#perm-filter", "region")
        page.wait_for_timeout(120)
        rows = page.locator("table.data tbody tr").all_inner_texts()
        assert any("erp.customers" in r for r in rows)
        assert not any("erp.orders" in r for r in rows)

    def test_filtering_costs_no_request(self, console):
        """It re-filters the payload already held."""
        page, api, _ = console
        before = len(api.requests)
        page.fill("#perm-filter", "orders")
        page.wait_for_timeout(150)
        assert len(api.requests) == before

    def test_grants_are_per_role(self, console):
        page, api, _ = console
        page.locator('button.perm[data-table="erp.customers"]').click()
        page.wait_for_selector(".perm--read")

        page.select_option("#perm-role", "viewer")
        page.wait_for_selector("table.data")
        # The analyst's grant must not appear under viewer.
        assert "perm--none" in page.locator(
            'button.perm[data-table="erp.customers"]').get_attribute("class")

        page.locator('button.perm[data-table="erp.customers"]').click()
        page.wait_for_selector(".perm--read")
        assert api.requests[-1][1]["role"] == "viewer"


class TestLocalisation:
    def test_arabic_mirrors_the_page_but_not_the_identifiers(self, console):
        """Table names read left-to-right in every language."""
        page, _, _ = console
        page.select_option("#locale-btn", "ar")
        page.wait_for_timeout(400)

        assert page.get_attribute("html", "dir") == "rtl"
        assert page.locator("button.perm").first.inner_text().strip() == "بلا وصول"
        # .mono forces LTR under RTL, which is what keeps erp.customers readable.
        assert page.locator("td .mono").first.evaluate(
            "e => getComputedStyle(e).direction") == "ltr"

    def test_no_string_falls_back_to_its_key(self, console):
        """A missing key renders as the literal key, e.g. `perm.access.read`."""
        page, _, _ = console
        for language in ("ar", "en"):
            page.select_option("#locale-btn", language)
            page.wait_for_timeout(400)
            assert "perm." not in page.inner_text("#content"), language


class TestTheWorkspaceDefault:
    """The preset card: what a role is granted before anybody edits the matrix.

    A fresh workspace grants nothing, which is the correct default and a poor
    starting point -- an empty matrix and ninety columns per table is how
    "grant everything" happens. The card exists so the first action is one
    click, and the tests below are mostly about it not being *too* easy.
    """

    def test_the_presets_are_offered(self, console):
        page, _, _ = console
        options = page.locator("#policy-preset option").all_inner_texts()
        assert [o.strip() for o in options] == [
            "No access", "Viewer", "Analyst", "Administrator"
        ]

    def test_saving_the_default_does_not_apply_it(self, console):
        """Recording intent and changing permissions are separate decisions.

        Only applying moves the grant version, and moving it re-authorizes every
        write that was approved but has not run yet -- so it must not happen as
        a side effect of saving a dropdown.
        """
        page, api, _ = console
        page.select_option("#policy-preset", "analyst")
        page.click("#policy-save")
        page.wait_for_timeout(300)

        kind, body = api.requests[-1]
        assert kind == "policy"
        assert body["apply"] is False
        assert body["roles"]["analyst"]["preset"] == "analyst"

    def test_applying_asks_first(self, console):
        page, api, _ = console
        page.select_option("#policy-preset", "analyst")

        page.once("dialog", lambda d: d.dismiss())
        page.click("#policy-apply")
        page.wait_for_timeout(300)
        assert api.requests == [], "applying went ahead without confirmation"

        page.once("dialog", lambda d: d.accept())
        page.click("#policy-apply")
        page.wait_for_timeout(300)
        assert api.requests[-1][1]["apply"] is True

    def test_read_enforcement_is_off_until_it_is_chosen(self, console):
        """Grants have governed writes only. Defaulting this on would hide
        every table from every user of every existing workspace at once."""
        page, _, _ = console
        assert page.locator("#policy-enforce-reads").is_checked() is False

    def test_the_choices_are_sent_together(self, console):
        page, api, _ = console
        page.select_option("#policy-preset", "viewer")
        page.check("#policy-new-tables")
        page.check("#policy-enforce-reads")
        page.click("#policy-save")
        page.wait_for_timeout(300)

        entry = api.requests[-1][1]["roles"]["analyst"]
        assert entry == {
            "preset": "viewer",
            "apply_to_new_tables": True,
            "enforce_reads": True,
        }

    def test_the_card_follows_the_selected_role(self, console):
        page, api, _ = console
        page.select_option("#policy-preset", "admin")
        page.click("#policy-save")
        page.wait_for_timeout(300)
        assert "analyst" in api.requests[-1][1]["roles"]

        page.select_option("#perm-role", "viewer")
        page.wait_for_selector("table.data")
        page.select_option("#policy-preset", "viewer")
        page.click("#policy-save")
        page.wait_for_timeout(300)
        assert "viewer" in api.requests[-1][1]["roles"], (
            "the card wrote the default for the wrong role"
        )
