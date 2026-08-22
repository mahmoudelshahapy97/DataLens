"""Walk the running stack in a real browser and photograph every screen.

The rest of ``tests/e2e`` asserts on the DOM: a selector is present, an attribute
is cleared, a request was not rejected. That catches breakage it was told to look
for and nothing else. A screen can satisfy every assertion in this suite and still
be unusable -- a sidebar overlapping the content, a table running off the viewport,
an Arabic layout that never mirrored, a panel that renders empty because the view
it was meant to fill resolved to nothing.

So this module drives the same flows and saves a full-page image of each one under
``artifacts/``. The assertions here are deliberately thin -- the page loaded, the
view settled, nothing threw -- because the *image* is the artifact. A human (or a
diff against the last run) looks at the folder and sees what shipped.

    docker compose up -d
    VANNA_E2E_URL=http://localhost:3000 \
    VANNA_E2E_PASSWORD=... \
    pytest tests/e2e/test_screenshots.py -m e2e

Images land in ``artifacts/`` at the repository root, or wherever
``VANNA_E2E_ARTIFACTS`` points. Numbered by flow so the directory listing reads in
the order a person would meet the screens.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Page, expect  # noqa: E402

BASE_URL = os.getenv("VANNA_E2E_URL", "").rstrip("/")
EMAIL = os.getenv("VANNA_E2E_EMAIL", "demo@example.com")
PASSWORD = os.getenv("VANNA_E2E_PASSWORD", "")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="VANNA_E2E_URL is not set"),
]

#: Repository root is two levels up from this file: tests/e2e/x.py -> repo.
ARTIFACTS = Path(
    os.getenv("VANNA_E2E_ARTIFACTS") or Path(__file__).resolve().parents[2] / "artifacts"
)

#: Every view in the workspace sidebar, in the order the sidebar lists them.
#: ``ask`` is the landing view and needs no sidebar click of its own.
#:
#: ``cubes`` is here but is expected to be hidden on a workspace bound to the
#: physical layer: ``app.js`` unhides that button only when ``/api/vanna/v2/cubes``
#: comes back non-empty, which needs ``VANNA_PROJECTS_DIR`` and a built manifest
#: named for the tenant. A hidden button is the correct behaviour, so the test
#: skips rather than fails -- but it stays in the list, because the day a manifest
#: is configured the screen must still be photographed.
VIEWS = [
    "schema",
    "history",
    "saved",
    "dashboards",
    "cubes",
    "account",
]

#: Viewports worth photographing separately. A "mobile preview" is not one size --
#: a 390pt phone and a 768pt tablet fail in different ways, and the phone is where
#: a sidebar built for 1440 has nowhere to go.
DEVICES = [
    ("phone", 390, 844),  # iPhone 14/15 class
    ("phone-small", 360, 740),  # the narrowest Android worth supporting
    ("tablet", 820, 1180),  # iPad Air, portrait
    ("tablet-landscape", 1180, 820),
]

#: The views worth checking on a phone. Every one of these renders a wide table or
#: a grid, which is exactly what a narrow viewport breaks.
MOBILE_VIEWS = ["history", "saved", "dashboards", "schema"]

#: Every tab in the operator console. Listed here rather than discovered so a tab
#: that disappears is a visible skip rather than a silently smaller run.
TABS = [
    "tenants",
    "accounts",
    "members",
    "permissions",
    "billing",
    "review",
    "verified",
    "rules",
    "starters",
]


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

_errors: list = []
_failed: list = []


@pytest.fixture(scope="session", autouse=True)
def artifacts_dir():
    """One folder for the run, created once and reported so it is findable."""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    yield ARTIFACTS
    print(f"\nscreenshots: {ARTIFACTS}")


@pytest.fixture
def page(browser):
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
        device_scale_factor=2,  # legible text in the saved image, not a blurry 1x
    )
    page = context.new_page()

    page.on("pageerror", lambda e: _errors.append(str(e)))
    page.on("requestfailed", lambda r: _failed.append(f"{r.method} {r.url} {r.failure}"))
    _errors.clear()
    _failed.clear()

    yield page
    context.close()


def _shot(page: Page, name: str) -> Path:
    """Full-page image, so content below the fold is in the record too."""
    path = ARTIFACTS / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0, f"{path} was written empty"
    return path


def _grow(page: Page, height: int = 2200) -> None:
    """Make the viewport tall before photographing a workspace view.

    The app is a fixed-height layout: the shell is 100vh and each panel scrolls
    inside itself, so ``full_page`` had nothing to grow into and every image came
    out exactly one viewport tall -- a history screen with fifty rows was
    photographed showing six. Growing the viewport is what puts the rows in the
    file, and it costs a relayout rather than a scroll-and-stitch.
    """
    page.set_viewport_size({"width": 1440, "height": height})
    page.wait_for_timeout(600)


def _shot_of(page: Page, selector: str, name: str) -> Path:
    """Photograph one element rather than the page.

    ``full_page`` grows the *page* to fit the document, which does nothing for a
    panel that scrolls inside itself: an open dashboard is a fixed-height sheet
    with its own scrollbar, so the full-page image caught whatever the sheet
    happened to be scrolled to and cropped every tile above it. Framing the
    element instead, with the viewport made tall enough that the sheet does not
    need to scroll, is what puts the whole dashboard in one file.
    """
    path = ARTIFACTS / f"{name}.png"
    page.locator(selector).screenshot(path=str(path))
    assert path.stat().st_size > 0, f"{path} was written empty"
    return path


def _sign_in(page: Page) -> None:
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    page.fill("#si-email", EMAIL)
    page.fill("#si-password", PASSWORD)
    page.click("#si-go")
    page.wait_for_selector("#app.ready", timeout=20_000)


def _open_view(page: Page, view: str) -> None:
    """Click a sidebar view and wait for it to finish loading.

    Skips rather than fails when the button is hidden. ``cubes`` is hidden unless
    the workspace has a semantic manifest, and a suite that fails on a feature the
    deployment does not have teaches you to ignore red.
    """
    button = page.locator(f"nav.side button[data-view='{view}']")
    if not button.count():
        pytest.skip(f"this build has no {view!r} view")
    if button.is_hidden():
        pytest.skip(
            f"the {view!r} view is hidden for this workspace "
            "(no semantic manifest -- see VANNA_PROJECTS_DIR)"
        )
    button.click()
    _settled(page)


def _settled(page: Page) -> None:
    """Wait out the view's own fetch before photographing it.

    ``aria-busy`` is the panel's own signal that it has finished loading, so it is
    a better barrier than a fixed sleep: an image taken mid-fetch shows a spinner
    and records nothing about the screen.
    """
    panel = page.locator("#view-other")
    if panel.count():
        expect(panel).to_have_attribute("aria-busy", "false", timeout=20_000)
    page.wait_for_timeout(400)  # let the last paint land


# ----------------------------------------------------------------------
# Sign-in
# ----------------------------------------------------------------------


class TestSignIn:
    def test_the_sign_in_screen(self, page: Page):
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        expect(page.locator("#si-form")).to_be_visible()

        _shot(page, "01-sign-in")
        assert not _errors, f"uncaught JavaScript errors: {_errors}"

    def test_a_refusal_is_shown_to_the_user(self, page: Page):
        """The error path is a screen too, and it is the one nobody looks at."""
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.fill("#si-email", "nobody@example.com")
        page.fill("#si-password", "wrong-password-here")
        page.click("#si-go")

        expect(page.locator("#si-error")).to_be_visible(timeout=15_000)
        _shot(page, "02-sign-in-refused")


# ----------------------------------------------------------------------
# The workspace
# ----------------------------------------------------------------------


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestWorkspace:
    def test_the_landing_view(self, page: Page):
        _sign_in(page)
        expect(page.locator("#user-email")).to_contain_text(EMAIL)

        page.wait_for_timeout(3000)  # the chat element mounts and connects
        _grow(page)
        _shot(page, "03-workspace-ask")
        assert not _errors, f"JavaScript errors after sign-in: {_errors}"

    @pytest.mark.parametrize("view", VIEWS)
    def test_each_view(self, page: Page, view: str):
        _sign_in(page)
        _open_view(page, view)
        _grow(page)

        _shot(page, f"04-workspace-{view}")
        assert not _errors, f"{view} raised: {_errors}"

    def test_the_saved_queries_are_not_an_empty_state(self, page: Page):
        """The point of seeding: this list has rows, and they are real queries.

        An empty list is a legitimate screen but a useless screenshot, and it is
        also indistinguishable from a fetch that quietly failed.
        """
        _sign_in(page)
        _open_view(page, "saved")

        rows = page.locator("#view-other .card")
        expect(rows.first).to_be_visible(timeout=20_000)
        assert rows.count() >= 3, (
            f"only {rows.count()} saved queries on screen; "
            "run tools/seed_demo_data.py first"
        )


# ----------------------------------------------------------------------
# Dashboards, opened
# ----------------------------------------------------------------------


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestDashboards:
    """The list screen is not the dashboard.

    Opening one executes every tile through the tool registry, which is where a
    stored-fine-renders-broken document finally shows itself -- as a sheet full of
    error cards that no server-side test and no list-view screenshot would catch.
    """

    def _list(self, page: Page) -> None:
        _sign_in(page)
        _open_view(page, "dashboards")

    def test_the_list_has_dashboards(self, page: Page):
        self._list(page)
        cards = page.locator("#view-other [data-open]")
        assert cards.count() >= 1, (
            "no dashboards to open; run tools/seed_demo_data.py first"
        )

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_each_dashboard_renders_its_tiles(self, page: Page, index: int):
        self._list(page)

        opener = page.locator(f"#view-other [data-open='{index}']")
        if not opener.count():
            pytest.skip(f"the workspace has no dashboard at index {index}")

        title = (page.locator("#view-other .card strong").nth(index).inner_text() or "")

        # Tall enough that a three-row dashboard does not need to scroll inside its
        # own sheet, so one image holds every tile.
        page.set_viewport_size({"width": 1600, "height": 2400})
        opener.click()

        # The sheet paints "running tiles" first, then replaces itself with the
        # rendered grid. Waiting for a card is what distinguishes the two.
        expect(page.locator("#overlay")).to_have_class(re.compile(r"\bon\b"))
        page.wait_for_selector("#sheet .card, #sheet .empty", timeout=60_000)
        page.wait_for_timeout(3000)  # let Plotly draw

        slug = title.strip().lower().replace(" ", "-") or f"index-{index}"
        _shot_of(page, "#sheet", f"05-dashboard-{index}-{slug}")

        text = page.locator("#sheet").inner_text().lower()
        for phrase in ("does not render", "stored dashboard is invalid", "traceback"):
            assert phrase not in text, f"{title!r} rendered an error: {text[:400]}"

        # The charts are the reason the .mjs mime-type bug mattered, so assert they
        # actually drew rather than trusting the absence of an error string.
        plots = page.locator("#sheet .js-plotly-plot")
        assert plots.count() >= 1, (
            f"{title!r} rendered no Plotly charts; "
            "check that /assets/*.mjs is served as application/javascript"
        )
        assert not _errors, f"{title!r} raised: {_errors}"


# ----------------------------------------------------------------------
# The operator console
# ----------------------------------------------------------------------


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestAdminConsole:
    def _open(self, page: Page) -> None:
        _sign_in(page)  # the console shares the app's session cookie
        page.goto(f"{BASE_URL}/admin/", wait_until="networkidle")
        page.wait_for_selector("#tabs button", timeout=20_000)
        page.wait_for_timeout(1200)

    def test_the_console_lands(self, page: Page):
        self._open(page)
        _shot(page, "06-admin-console")
        assert not _errors, f"the console raised: {_errors}"

    @pytest.mark.parametrize("tab", TABS)
    def test_each_tab(self, page: Page, tab: str):
        self._open(page)

        button = page.locator(f"#tabs button[data-tab='{tab}']")
        if not button.count():
            pytest.skip(f"the console has no {tab!r} tab")
        button.click()
        page.wait_for_timeout(1500)  # each tab fetches its own rows

        _shot(page, f"07-admin-{tab}")
        assert not _errors, f"the {tab} tab raised: {_errors}"


# ----------------------------------------------------------------------
# The other language
# ----------------------------------------------------------------------


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestArabic:
    """Arabic is not a translation, it is a mirrored layout.

    Every rule about which side a thing sits on inverts, and a hard-coded
    ``margin-left`` survives every server-side test. This is only visible in a
    picture.
    """

    def test_the_sign_in_screen_in_arabic(self, page: Page):
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.select_option("#si-locale", "ar")
        page.wait_for_timeout(900)

        _shot(page, "08-sign-in-arabic")

    def test_the_workspace_in_arabic(self, page: Page):
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        page.select_option("#si-locale", "ar")
        page.wait_for_timeout(600)
        page.fill("#si-email", EMAIL)
        page.fill("#si-password", PASSWORD)
        page.click("#si-go")
        page.wait_for_selector("#app.ready", timeout=20_000)
        page.wait_for_timeout(2500)

        direction = page.evaluate("document.documentElement.dir")
        _shot(page, "09-workspace-arabic")
        assert direction == "rtl", f"Arabic rendered with dir={direction!r}, not rtl"


# ----------------------------------------------------------------------
# Mobile
# ----------------------------------------------------------------------


@pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set")
class TestMobilePreview:
    """The same screens on the sizes people actually hold.

    Photographed with a touch-capable mobile context rather than a narrowed desktop
    one, because the layout branches on more than width: a hover-only affordance is
    unreachable, and ``100vh`` is not the visible height once the browser chrome is
    accounted for. Narrowing a desktop viewport hides both problems.

    The assertion that matters here is horizontal overflow. A page wider than its
    own viewport is the single most common mobile defect and it is invisible in a
    full-page screenshot -- the image just gets wider -- so it is measured, not
    eyeballed.
    """

    @staticmethod
    def _context(browser, width: int, height: int):
        return browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=3,  # phones are 3x; text is unreadable at 1x
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148"
            ),
        )

    @staticmethod
    def _overflow(page: Page) -> int:
        """How far the document extends past the viewport, in CSS pixels."""
        return page.evaluate(
            "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)"
        )

    @pytest.mark.parametrize("name,width,height", DEVICES)
    def test_the_sign_in_screen(self, browser, name: str, width: int, height: int):
        context = self._context(browser, width, height)
        page = context.new_page()
        try:
            page.goto(f"{BASE_URL}/", wait_until="networkidle")
            expect(page.locator("#si-form")).to_be_visible()
            page.wait_for_timeout(400)

            _shot(page, f"10-mobile-{name}-sign-in")
            overflow = self._overflow(page)
            assert overflow == 0, (
                f"the sign-in screen overflows by {overflow}px at {width}x{height}"
            )
        finally:
            context.close()

    @pytest.mark.parametrize("name,width,height", DEVICES)
    def test_the_workspace(self, browser, name: str, width: int, height: int):
        context = self._context(browser, width, height)
        page = context.new_page()
        try:
            _sign_in(page)
            page.wait_for_timeout(3000)  # the chat element mounts

            _shot(page, f"11-mobile-{name}-workspace")
            overflow = self._overflow(page)
            assert overflow == 0, (
                f"the workspace overflows by {overflow}px at {width}x{height}"
            )
        finally:
            context.close()

    @pytest.mark.parametrize("view", MOBILE_VIEWS)
    def test_each_view_on_a_phone(self, browser, view: str):
        """One phone size, every data-heavy view.

        A wide table has to do *something* at 390px -- scroll inside its own box,
        or reflow. What it must not do is widen the page and take the navigation
        off-screen with it.
        """
        context = self._context(browser, 390, 844)
        page = context.new_page()
        try:
            _sign_in(page)
            _open_view(page, view)

            _shot(page, f"12-mobile-phone-{view}")
            overflow = self._overflow(page)
            assert overflow == 0, (
                f"the {view} view overflows the phone viewport by {overflow}px"
            )
        finally:
            context.close()

    def test_the_admin_console_on_a_phone(self, browser):
        """The console is a desktop tool, which is not a reason for it to be broken.

        Its tab strip is the widest fixed row in the product.
        """
        context = self._context(browser, 390, 844)
        page = context.new_page()
        try:
            _sign_in(page)
            page.goto(f"{BASE_URL}/admin/", wait_until="networkidle")
            page.wait_for_selector("#tabs button", timeout=20_000)
            page.wait_for_timeout(1500)

            _shot(page, "13-mobile-phone-admin")
            overflow = self._overflow(page)
            assert overflow == 0, (
                f"the console overflows the phone viewport by {overflow}px"
            )
        finally:
            context.close()

    def test_a_dashboard_on_a_phone(self, browser):
        """Charts in a sheet, on the narrowest thing that will ever open one."""
        context = self._context(browser, 390, 844)
        page = context.new_page()
        try:
            _sign_in(page)
            _open_view(page, "dashboards")

            opener = page.locator("#view-other [data-open='0']")
            if not opener.count():
                pytest.skip("no dashboards; run tools/seed_demo_data.py first")
            opener.click()
            page.wait_for_selector("#sheet .card, #sheet .empty", timeout=60_000)
            page.wait_for_timeout(2500)

            _shot(page, "14-mobile-phone-dashboard")
            overflow = self._overflow(page)
            assert overflow == 0, (
                f"an open dashboard overflows the phone viewport by {overflow}px"
            )
        finally:
            context.close()
