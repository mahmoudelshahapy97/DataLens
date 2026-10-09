"""The Instructions and Starter library screens, driven in a real browser.

The admin console has no build step and no framework, so nothing else in the
suite executes a line of it. These load the real ``console.js`` into Chromium,
stub the API with the shapes ``routes/instructions.py`` actually returns, and
drive the page the way an administrator would.

What is worth asserting here, rather than at the API:

* **A platform rule offers no Delete button.** The server refuses it with a 409,
  so the product is safe either way -- but a button that always errors is a
  worse answer than no button, and only a rendering test can tell them apart.
* **A rule that may not even be switched off offers no toggle either.** The two
  kinds of platform rule look identical in the payload apart from one flag.
* **Editing sends a partial patch.** The page could easily send the whole object
  and silently revert a field it did not display.
* **A pack that has been taken offers Remove, not Add.**

The harness mirrors the container's path layout, not the source tree's, because
``console.js`` imports ``./shared/core.js`` and that only resolves because the
Dockerfile lands both under ``/assets/``.
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

ME = {
    "user": {"id": "u1", "email": "admin@acme.test", "name": "Admin", "role": "admin"},
    "tenant": {"id": "acme", "name": "Acme"},
    "memberships": ["acme"],
    "is_platform_admin": False,
    "is_admin": True,
    "control_plane": True,
    "deployment_mode": "test",
}

#: One of each kind, which is the whole point of the screen.
INSTRUCTIONS = [
    {
        "id": "platform.schema-only",
        "text": "Only reference tables in the schema context.",
        "scope": "global", "scope_ref": None, "priority": 1000, "enabled": True,
        "origin": "platform", "locked": True, "disableable": False,
        "source_pack": None, "created_by": "platform", "updated_at": None,
    },
    {
        "id": "platform.explicit-columns",
        "text": "Prefer an explicit column list to SELECT *.",
        "scope": "global", "scope_ref": None, "priority": 500, "enabled": True,
        "origin": "platform", "locked": True, "disableable": True,
        "source_pack": None, "created_by": "platform", "updated_at": None,
    },
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "text": "Amounts in _cents are integer cents.",
        "scope": "global", "scope_ref": None, "priority": 60, "enabled": True,
        "origin": "library", "locked": False, "disableable": False,
        "source_pack": "finance-conventions", "created_by": "admin@acme.test",
        "updated_at": None,
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "text": "Our fiscal year starts in February.",
        "scope": "global", "scope_ref": None, "priority": 10, "enabled": True,
        "origin": "tenant", "locked": False, "disableable": False,
        "source_pack": None, "created_by": "admin@acme.test", "updated_at": None,
    },
]

PACKS = [
    {
        "id": "finance-conventions", "name": "Finance conventions",
        "description": "Currency units and the fiscal calendar.",
        "instruction_count": 4, "enabled": True,
        "preview": ["Amounts in _cents are integer cents."],
    },
    {
        "id": "data-hygiene", "name": "Data hygiene",
        "description": "Nulls, duplicates and time zones.",
        "instruction_count": 5, "enabled": False,
        "preview": ["NULL is not zero."],
    },
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
    def log_message(self, *args):
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
    """A stub instructions API that records what the page asked it to do."""

    def __init__(self):
        self.requests = []

    def install(self, page):
        def handle(route, request):
            url = request.url
            method = request.method

            if "/me" in url:
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(ME))
            if "instruction-packs" in url:
                if method == "GET":
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"packs": PACKS}),
                    )
                self.requests.append((method, url, request.post_data_json))
                body = ({"ok": True, "added": 4, "skipped": 0} if method == "POST"
                        else {"ok": True, "removed": 3, "kept": 1})
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(body))
            if "/instructions" in url:
                if method == "GET":
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"instructions": INSTRUCTIONS,
                                         "store_configured": True}),
                    )
                self.requests.append((method, url, request.post_data_json))
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps({"ok": True}))

            # Anything else the console loads on start.
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({}))

        page.route("**/api/vanna/v2/**", handle)


@pytest.fixture
def api():
    return Api()


@pytest.fixture
def page(server, api):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 1200})
        page = context.new_page()
        page._errors = []
        page.on("pageerror", lambda e: page._errors.append(str(e)))
        api.install(page)
        page.goto(f"{server}/admin/", wait_until="networkidle")
        page.wait_for_selector("#tabs button")
        yield page
        assert not page._errors, page._errors
        browser.close()


def open_tab(page, name: str) -> None:
    page.click(f'#tabs button[data-tab="{name}"]')
    page.wait_for_timeout(250)


def card_for(page, text: str):
    return page.locator(".card", has=page.locator(f'p:text-is("{text}")')).first


class TestTheRulesScreen:
    def test_it_groups_rules_by_who_owns_them(self, page):
        open_tab(page, "rules")
        body = page.locator("#content").inner_text()

        for heading in ("Platform baseline", "From the starter library", "Written here"):
            assert heading in body, f"missing the {heading!r} section"

    def test_every_rule_is_shown(self, page):
        open_tab(page, "rules")
        body = page.locator("#content").inner_text()
        for rule in INSTRUCTIONS:
            assert rule["text"] in body

    def test_a_platform_rule_has_no_delete_button(self, page):
        """The server answers 409. A button that always fails is a worse
        answer than no button, and only the page can be asked which it is."""
        open_tab(page, "rules")
        card = card_for(page, "Only reference tables in the schema context.")
        assert card.locator("[data-delrule]").count() == 0
        assert card.locator("[data-editrule]").count() == 0

    def test_a_platform_rule_that_cannot_be_switched_off_has_no_toggle(self, page):
        open_tab(page, "rules")
        card = card_for(page, "Only reference tables in the schema context.")
        assert card.locator("[data-toggle]").count() == 0

    def test_a_disableable_platform_rule_does_offer_a_toggle(self, page):
        """The two kinds differ by one flag in the payload and must not render
        the same."""
        open_tab(page, "rules")
        card = card_for(page, "Prefer an explicit column list to SELECT *.")
        assert card.locator("[data-toggle]").count() == 1
        assert card.locator("[data-delrule]").count() == 0

    def test_a_library_copy_is_fully_editable(self, page):
        """A pack is a starting point, not a subscription."""
        open_tab(page, "rules")
        card = card_for(page, "Amounts in _cents are integer cents.")
        assert card.locator("[data-editrule]").count() == 1
        assert card.locator("[data-delrule]").count() == 1
        # Lowercased for comparison: the tag is uppercased by CSS, and asserting
        # on the rendered casing would break on a styling change.
        assert "finance-conventions" in card.inner_text().lower()

    def test_a_workspace_rule_is_fully_editable(self, page):
        open_tab(page, "rules")
        card = card_for(page, "Our fiscal year starts in February.")
        assert card.locator("[data-editrule]").count() == 1
        assert card.locator("[data-delrule]").count() == 1

    def test_priority_is_shown(self, page):
        """It orders the prompt and decides what survives truncation, so a
        screen that hides it cannot explain why one rule wins."""
        open_tab(page, "rules")
        assert "1000" in page.locator("#content").inner_text()


class TestEditing:
    def test_editing_sends_a_patch_to_the_workspace_path(self, page, api):
        open_tab(page, "rules")
        card_for(page, "Our fiscal year starts in February.").locator(
            "[data-editrule]"
        ).click()
        page.wait_for_selector("#edit-text")

        page.fill("#edit-text", "Our fiscal year starts in March.")
        page.fill("#edit-priority", "42")
        page.click("[data-saverule]")
        page.wait_for_timeout(300)

        method, url, body = api.requests[-1]
        assert method == "PUT"
        assert "/admin/tenants/acme/instructions/22222222" in url, (
            f"the edit went to {url}, not the workspace-scoped path"
        )
        assert body["text"] == "Our fiscal year starts in March."
        assert body["priority"] == 42

    def test_cancelling_sends_nothing(self, page, api):
        open_tab(page, "rules")
        card_for(page, "Our fiscal year starts in February.").locator(
            "[data-editrule]"
        ).click()
        page.wait_for_selector("#edit-text")
        page.click("[data-cancelrule]")
        page.wait_for_timeout(200)

        assert api.requests == []
        assert page.locator("#edit-text").count() == 0

    def test_the_scope_reference_unlocks_with_the_scope(self, page):
        open_tab(page, "rules")
        card_for(page, "Our fiscal year starts in February.").locator(
            "[data-editrule]"
        ).click()
        page.wait_for_selector("#edit-scope")

        assert page.locator("#edit-ref").is_disabled(), "global needs no reference"
        page.select_option("#edit-scope", "table")
        assert not page.locator("#edit-ref").is_disabled()


class TestAdding:
    def test_a_new_rule_can_set_its_priority(self, page, api):
        """It was hardcoded to 0, so every rule tied and the order was uuid
        order -- which also made the truncation guarantee meaningless."""
        open_tab(page, "new")
        page.fill("#rt", "Exclude internal accounts.")
        page.fill("#rp", "75")
        page.click("#add-rule")
        page.wait_for_timeout(300)

        method, url, body = api.requests[-1]
        assert method == "POST"
        assert "/admin/tenants/acme/instructions" in url
        assert body["priority"] == 75
        assert body["text"] == "Exclude internal accounts."


class TestTheLibrary:
    def test_it_lists_the_packs(self, page):
        open_tab(page, "library")
        body = page.locator("#content").inner_text()
        assert "Finance conventions" in body
        assert "Data hygiene" in body
        assert "5 rules" in body

    def test_a_pack_already_taken_offers_removal(self, page):
        open_tab(page, "library")
        card = page.locator(".card", has_text="Finance conventions").first
        assert card.locator("[data-removepack]").count() == 1
        assert card.locator("[data-addpack]").count() == 0

    def test_a_pack_not_taken_offers_adding(self, page):
        open_tab(page, "library")
        card = page.locator(".card", has_text="Data hygiene").first
        assert card.locator("[data-addpack]").count() == 1

    def test_adding_a_pack_posts_to_enable(self, page, api):
        open_tab(page, "library")
        page.locator('[data-addpack="data-hygiene"]').click()
        page.wait_for_timeout(300)

        method, url, _ = api.requests[-1]
        assert method == "POST"
        assert url.endswith("/instruction-packs/data-hygiene/enable")

    def test_removing_a_pack_says_what_it_kept(self, page, api):
        """`kept` is not a rounding error to hide: a rule somebody reworded is
        theirs, and the admin needs to know it survived."""
        open_tab(page, "library")
        page.on("dialog", lambda d: d.accept())
        page.locator('[data-removepack="finance-conventions"]').click()
        page.wait_for_timeout(400)

        method, url, _ = api.requests[-1]
        assert method == "DELETE"
        assert url.endswith("/instruction-packs/finance-conventions")
        assert "1" in page.locator("#toast").inner_text()
