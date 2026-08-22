"""End-to-end, in a real browser, against the running stack.

Everything else in this suite drives the application through ASGI. That verifies
behaviour but not *delivery*: it cannot tell you the CSP blocked your own script, the
module import resolved to a 404, the focus trap does nothing, or the page renders a
row of raw i18n keys. Each of those has shipped from this repository at some point in
the last few hours.

So this one talks HTTP to the container the way a person does.

    docker compose up -d
    pytest tests/e2e -m e2e --headed      # watch it

Marked ``e2e`` and skipped unless ``VANNA_E2E_URL`` is set, so a normal ``pytest``
run on a laptop with no stack up stays green.
"""

from __future__ import annotations

import os
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = pytest.mark.e2e

BASE_URL = os.getenv("VANNA_E2E_URL", "").rstrip("/")
EMAIL = os.getenv("VANNA_E2E_EMAIL", "demo@example.com")
PASSWORD = os.getenv("VANNA_E2E_PASSWORD", "")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="VANNA_E2E_URL is not set"),
]


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    # Anything the browser refuses to run or fetch is a failure of *delivery*, and
    # delivery is the only thing this file exists to check. Collected rather than
    # asserted inline so a test can report every problem on the page at once.
    page.on("console", lambda m: _console.append((m.type, m.text)) if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: _errors.append(str(e)))
    page.on("requestfailed", lambda r: _failed.append(f"{r.method} {r.url} {r.failure}"))

    _console.clear()
    _errors.clear()
    _failed.clear()

    yield page
    context.close()


_console: list = []
_errors: list = []
_failed: list = []


def _sign_in(page: Page) -> None:
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    page.fill("#si-email", EMAIL)
    page.fill("#si-password", PASSWORD)
    page.click("#si-go")
    page.wait_for_selector("#app.ready", timeout=15_000)


# ----------------------------------------------------------------------
# Delivery
# ----------------------------------------------------------------------


class TestItLoads:
    def test_the_page_renders_with_no_console_errors(self, page: Page):
        page.goto(f"{BASE_URL}/", wait_until="networkidle")

        expect(page).to_have_title(re.compile("Vanna"))
        assert not _errors, f"uncaught JavaScript errors: {_errors}"
        assert not _failed, f"failed requests: {_failed}"

    def test_the_csp_does_not_block_our_own_assets(self, page: Page):
        """The risk of adding a strict CSP is blocking yourself.

        `script-src 'self'` with no 'unsafe-inline' is only viable because the CSS
        and JS were moved out of the HTML. If that ever regresses, the browser
        reports a CSP violation on the console and the page is dead -- which no
        server-side test can see.
        """
        page.goto(f"{BASE_URL}/", wait_until="networkidle")

        violations = [text for kind, text in _console if "content security policy" in text.lower()]
        assert not violations, f"CSP blocked our own assets: {violations}"

        # And the module actually executed: the locale picker is populated by JS.
        expect(page.locator("#si-locale option").first).to_be_attached()

    def test_the_shared_module_resolved(self, page: Page):
        """`import ... from './shared/core.js'` resolves to /assets/shared/core.js.

        A relative module specifier that 404s fails silently in the network tab and
        takes the whole page's script with it.
        """
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        assert not [f for f in _failed if "core.js" in f], _failed
        assert not [f for f in _failed if "dialogs.js" in f], _failed

    def test_every_mjs_chunk_is_served_as_javascript(self, page: Page):
        """A module served as `application/octet-stream` is refused by the browser.

        nginx 1.27's mime.types has no `.mjs` entry, and Vite names its dynamic
        chunks `.mjs`. The result was a 200 carrying 7MB of correct JavaScript that
        Chrome would not execute -- so `plotly.min-<hash>.mjs` failed to import and
        every chart in the product was blank, with nothing wrong anywhere a
        server-side test could look. The network tab was green.

        Asserted over whatever chunks the build actually produced, rather than one
        pinned filename, because the hash changes on every build.
        """
        page.goto(f"{BASE_URL}/", wait_until="networkidle")

        chunks = set()
        for asset in ("/assets/vanna-components.js", "/assets/app.js"):
            response = page.request.get(f"{BASE_URL}{asset}")
            if response.status == 200:
                chunks |= set(re.findall(r"[\w.\-]+\.mjs", response.text()))

        if not chunks:
            pytest.skip("this build emits no .mjs chunks")

        wrong = {}
        for chunk in sorted(chunks):
            response = page.request.get(f"{BASE_URL}/assets/{chunk}")
            content_type = response.headers.get("content-type", "")
            if "javascript" not in content_type and "ecmascript" not in content_type:
                wrong[chunk] = f"{response.status} {content_type!r}"

        assert not wrong, (
            "ES modules served under a type the browser will not execute: "
            f"{wrong}. Add `types {{ application/javascript mjs; }}` to the "
            "/assets/ location in frontend/nginx.conf."
        )

    def test_the_favicon_is_an_image_not_the_html_page(self, page: Page):
        response = page.request.get(f"{BASE_URL}/favicon.svg")
        assert response.status == 200
        assert "svg" in response.headers.get("content-type", "")

    def test_a_missing_asset_404s_rather_than_returning_the_page(self, page: Page):
        # The SPA catch-all must not answer asset requests with index.html.
        response = page.request.get(f"{BASE_URL}/nope.png")
        assert response.status == 404

    def test_the_translations_load(self, page: Page):
        """A missing dictionary renders raw keys like `nav.account`."""
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        body = page.locator("body").inner_text()
        assert not re.search(r"\b(signin|nav|common)\.[a-zA-Z]+\b", body), (
            f"untranslated keys on the page: {body[:200]}"
        )


class TestCaching:
    """No asset may be cached in a way a redeploy cannot dislodge.

    `/assets/` was served `expires 1y; immutable` under a comment claiming the
    filenames were hashed. They are not -- app.js keeps its name across every deploy
    -- so a browser that loaded a broken bundle kept it, and no reload could clear
    it: `immutable` means the browser never asks again. A blank page survived the
    fix being deployed.

    Correct caching is not something to verify by reading the config. nginx picks
    regex locations over prefix ones, which silently rerouted every /assets/ request
    away from the block that was supposed to handle it -- so the config said one
    thing and the wire said another.
    """

    #: (path, must the response be revalidatable). Everything here has a stable
    #: filename, so everything here must be.
    ASSETS = [
        "/assets/app.js",
        "/assets/app.css",
        "/assets/shared/core.js",
        "/assets/shared/dialogs.js",
        "/assets/vanna-components.js",
        "/locales/en.json",
        "/admin/locales/en.json",
        "/favicon.svg",
        "/",
    ]

    @pytest.mark.parametrize("path", ASSETS)
    def test_nothing_is_cached_without_revalidation(self, page: Page, path: str):
        response = page.request.get(f"{BASE_URL}{path}")
        assert response.status == 200, f"{path} -> {response.status}"

        cache_control = response.headers.get("cache-control", "")
        assert cache_control, (
            f"{path} is served with no Cache-Control at all, so the browser applies "
            "heuristic caching and may serve a stale copy without asking"
        )
        assert "immutable" not in cache_control, (
            f"{path} is immutable but its filename is not content-hashed: a bad "
            "deploy would be permanent"
        )
        assert "no-cache" in cache_control or "no-store" in cache_control, (
            f"{path} -> {cache_control!r}; a stable filename must revalidate"
        )

    def test_an_unchanged_asset_revalidates_cheaply(self, page: Page):
        """`no-cache` is not `no-store`: the 304 is what makes it affordable."""
        first = page.request.get(f"{BASE_URL}/assets/app.js")
        etag = first.headers.get("etag")
        assert etag, "no ETag, so revalidation would re-download the whole file"

        second = page.request.get(
            f"{BASE_URL}/assets/app.js", headers={"If-None-Match": etag}
        )
        assert second.status == 304, f"expected 304, got {second.status}"


# ----------------------------------------------------------------------
# The product
# ----------------------------------------------------------------------


class TestSignIn:
    def test_bad_credentials_are_refused_without_saying_why(self, page: Page):
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.fill("#si-email", "nobody@example.com")
        page.fill("#si-password", "wrong-password-here")
        page.click("#si-go")

        error = page.locator("#si-error")
        expect(error).to_be_visible(timeout=10_000)
        # Same message for unknown, disabled and wrong-password.
        expect(error).to_contain_text("not valid")

    @pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
    def test_signing_in_reaches_the_workspace(self, page: Page):
        _sign_in(page)
        expect(page.locator("#user-email")).to_contain_text(EMAIL)
        expect(page.locator("#tenant-name")).not_to_have_text("—")
        assert not _errors, f"JavaScript errors after sign-in: {_errors}"


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestNavigation:
    @pytest.mark.parametrize(
        "view,marker",
        [
            ("schema", "table"),
            ("history", ""),
            ("saved", ""),
            ("dashboards", ""),
            ("account", ""),
        ],
    )
    def test_each_view_renders(self, page: Page, view: str, marker: str):
        _sign_in(page)
        page.click(f"nav.side button[data-view='{view}']")

        panel = page.locator("#view-other")
        expect(panel).to_be_visible(timeout=15_000)
        # aria-busy must be cleared, or a screen reader reports "loading" forever.
        expect(panel).to_have_attribute("aria-busy", "false", timeout=15_000)
        assert not _errors, f"{view} raised: {_errors}"

    def test_the_schema_screen_shows_the_bound_database(self, page: Page):
        _sign_in(page)
        page.click("nav.side button[data-view='schema']")
        expect(page.locator("#view-other")).to_contain_text("chinook", timeout=20_000)

    def test_a_write_needs_no_csrf_dance_from_the_page(self, page: Page):
        """The CSRF token is issued and echoed by the shared fetch helper.

        If that wiring breaks, every write 403s -- which is exactly the sort of
        thing that looks fine server-side and is broken in the browser.
        """
        _sign_in(page)
        page.click("nav.side button[data-view='saved']")
        expect(page.locator("#view-other")).to_have_attribute(
            "aria-busy", "false", timeout=15_000
        )

        result = page.evaluate(
            """async () => {
                const token = document.cookie.match(/vanna_csrf=([^;]*)/);
                const r = await fetch('/api/vanna/v2/saved-queries', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': token ? decodeURIComponent(token[1]) : '',
                    },
                    body: JSON.stringify({title: 'e2e probe', sql: 'SELECT 1'}),
                });
                return r.status;
            }"""
        )
        assert result == 200, f"a write from the page returned {result}"


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestChatTransport:
    """The chat element reaches the backend.

    This is the one thing the rest of the suite could not see. <vanna-chat> owns its
    own transport and is handed its headers once, on `vanna-ready` -- so when the
    CSRF middleware arrived, every POST to /chat_sse and /chat_poll started coming
    back 403 and the widget showed "Connection failed. Unable to reach server".

    Server-side tests passed: the middleware was working exactly as designed. The
    page was broken because the component was never told about the token.
    """

    def test_the_chat_endpoint_is_not_rejected(self, page: Page):
        statuses = []
        page.on(
            "response",
            lambda r: statuses.append((r.status, r.url))
            if "/chat_" in r.url
            else None,
        )

        _sign_in(page)
        page.wait_for_timeout(5000)

        assert statuses, "the chat element never called the backend"
        rejected = [(s, u) for s, u in statuses if s >= 400]
        assert not rejected, f"chat transport rejected: {rejected}"

    def test_the_widget_does_not_report_a_connection_failure(self, page: Page):
        _sign_in(page)
        page.wait_for_timeout(5000)
        assert page.locator("text=Connection failed").count() == 0

    def test_the_csrf_token_is_sent_on_the_chat_call(self, page: Page):
        """The specific mechanism, so a fix by coincidence is not mistaken for a fix."""
        headers = []
        page.on(
            "request",
            lambda r: headers.append(r.headers) if "/chat_" in r.url else None,
        )

        _sign_in(page)
        page.wait_for_timeout(5000)

        assert headers, "no chat request was made"
        assert any(h.get("x-csrf-token") for h in headers), (
            "the chat element sent no X-CSRF-Token; chatHeaders() is not adding it"
        )


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestAdminConsole:
    """The operator console, which had no browser coverage until it broke.

    Its dictionary moved to /admin/locales/ and its fetch was not updated. The 404
    was caught, `t()` fell back to the key, and every label became its own lookup
    key: `console.title`, `tab.billing`, `bill.plan`. It renders as a plausible
    naming scheme, so nothing looked broken -- the page was fully functional and
    completely unreadable.

    The static test pins the path. This pins the result.
    """

    #: Keys look like `area.name`. Anything matching this on screen is a dictionary
    #: that did not load.
    RAW_KEY = re.compile(r"(?:console|tab|bill|acc|common|a11y|ex|ins)\.[a-zA-Z]+")

    def _open(self, page: Page) -> None:
        _sign_in(page)  # the console shares the app's session cookie
        page.goto(f"{BASE_URL}/admin/", wait_until="networkidle")
        page.wait_for_timeout(1500)

    def test_it_renders_translations_not_keys(self, page: Page):
        self._open(page)
        found = sorted(set(self.RAW_KEY.findall(page.locator("body").inner_text())))
        assert not found, f"the console is rendering raw i18n keys: {found[:8]}"

    def test_the_tabs_are_readable(self, page: Page):
        self._open(page)
        labels = page.locator("nav#tabs [role='tab']").all_inner_texts()
        assert labels, "no tabs rendered"
        assert not any(self.RAW_KEY.match(label) for label in labels), labels
        assert "Workspaces" in labels

    def test_the_header_is_one_row(self, page: Page):
        """`select { width: 100% }` applied to the header language picker.

        It took the whole row, so the picker wrapped onto its own line and pushed
        the reload button onto a third -- a header three times its intended height.
        """
        self._open(page)
        height = page.evaluate(
            "document.querySelector('header').getBoundingClientRect().height"
        )
        assert height < 90, f"the header wrapped: {height}px tall"

    def test_the_language_picker_does_not_fill_the_row(self, page: Page):
        self._open(page)
        ratio = page.evaluate(
            """() => {
                const select = document.getElementById('locale-btn');
                const header = document.querySelector('header');
                return select.getBoundingClientRect().width /
                       header.getBoundingClientRect().width;
            }"""
        )
        assert ratio < 0.3, f"the language picker takes {ratio:.0%} of the header"

    def test_it_loads_without_console_errors(self, page: Page):
        _sign_in(page)
        _console.clear()
        _errors.clear()
        page.goto(f"{BASE_URL}/admin/", wait_until="networkidle")
        page.wait_for_timeout(2000)
        assert not _errors, f"JavaScript errors in the console: {_errors}"

    def test_switching_language_keeps_it_readable(self, page: Page):
        """Arabic has its own dictionary; a missing one degrades the same way."""
        self._open(page)
        page.select_option("#locale-btn", "ar")
        page.wait_for_timeout(1500)

        assert page.evaluate("document.documentElement.getAttribute('dir')") == "rtl"
        found = sorted(set(self.RAW_KEY.findall(page.locator("body").inner_text())))
        assert not found, f"Arabic is rendering raw keys: {found[:8]}"


# ----------------------------------------------------------------------
# Accessibility
# ----------------------------------------------------------------------


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestAccessibility:
    def test_the_skip_link_is_first_and_focusable(self, page: Page):
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.keyboard.press("Tab")
        focused = page.evaluate("document.activeElement.className")
        assert "skip-link" in focused, f"first tab stop was {focused!r}"

    def test_the_live_regions_exist(self, page: Page):
        """Streamed answers are announced through these, or not at all."""
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        expect(page.locator("#a11y-status")).to_have_attribute("aria-live", "polite")
        expect(page.locator("#a11y-alerts")).to_have_attribute("aria-live", "assertive")

    def test_the_navigation_is_a_tablist(self, page: Page):
        _sign_in(page)
        tabs = page.locator("nav.side [role='tab']")
        assert tabs.count() >= 5
        # Exactly one tab is in the tab order; arrows move within the group.
        in_order = page.locator("nav.side [role='tab'][tabindex='0']")
        assert in_order.count() == 1

    def test_arrow_keys_move_between_tabs(self, page: Page):
        _sign_in(page)
        page.locator("nav.side [role='tab'][tabindex='0']").focus()
        before = page.evaluate("document.activeElement.dataset.view")
        page.keyboard.press("ArrowDown")
        after = page.evaluate("document.activeElement.dataset.view")
        assert before != after, "arrow keys did not move focus within the tablist"

    def test_a_dialog_traps_focus_and_escape_closes_it(self, page: Page):
        _sign_in(page)
        page.click("#tenant-pill")

        sheet = page.locator("#sheet")
        expect(sheet).to_be_visible()
        expect(sheet).to_have_attribute("aria-modal", "true")
        # Focus moved into the dialog rather than staying on the page behind it.
        assert page.evaluate("document.getElementById('sheet').contains(document.activeElement)")

        page.keyboard.press("Escape")
        expect(page.locator("#overlay")).not_to_have_class(re.compile(r"\bon\b"))
        # And focus came back to what opened it.
        assert page.evaluate("document.activeElement.id") == "tenant-pill"

    def test_every_interactive_control_has_an_accessible_name(self, page: Page):
        _sign_in(page)
        unnamed = page.evaluate(
            """() => {
                const name = (el) =>
                    (el.getAttribute('aria-label') || '').trim() ||
                    (el.getAttribute('title') || '').trim() ||
                    (el.innerText || '').trim() ||
                    (el.labels && el.labels.length ? el.labels[0].innerText.trim() : '');
                return [...document.querySelectorAll(
                    'button:not([hidden]), a[href]:not([hidden]), select:not([hidden])'
                )]
                  .filter((el) => el.offsetParent !== null && !name(el))
                  .map((el) => el.outerHTML.slice(0, 90));
            }"""
        )
        assert not unnamed, f"controls with no accessible name: {unnamed}"
