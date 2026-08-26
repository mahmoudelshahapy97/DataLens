"""The same workspace, photographed as four different people.

Every screenshot in `artifacts/` before this one was taken as the platform admin, who
can see everything. That is the least informative view of a product sold on the
promise that different people see different things -- and it means the interesting
half of the authorisation model had no visual record at all.

So this signs in as four accounts and saves what each one actually gets:

    artifacts/admin/      workspace admin  -- the console, members, permissions
    artifacts/analysis/   analyst          -- can save work, no console
    artifacts/viewer/     viewer           -- reads everything, writes nothing
    artifacts/user/       outsider         -- a real user of another workspace

Run `tools/provision_role_accounts.py` first; it creates the accounts and grants the
roles. Without them every test here skips rather than fails, because a missing fixture
is a setup problem and should not read as a broken product.

**The assertions are the point, not the images.** A screenshot proves a page rendered;
it cannot prove a viewer was *refused*. So each role also probes the boundary it is
supposed to sit behind, and the two refusal codes are deliberately different:

* a **viewer** writing gets **403** with a message naming their role -- they are a
  member, they already know the resource exists, and hiding it buys nothing
* a **member who is not an admin** gets **404** from the admin routes, so they never
  learn the member roster exists

An outsider is refused too, but at the *identity* layer rather than the route layer,
and that layer answers 403 with a message that names the workspace. That contradicts
the property the README states, and `test_a_non_member_cannot_tell_which_workspaces_exist`
below pins it as an expected failure rather than quietly asserting the behaviour we have.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Page, expect  # noqa: E402

BASE_URL = os.getenv("VANNA_E2E_URL", "").rstrip("/")

#: Set by tools/provision_role_accounts.py. Overridable, but there is no default
#: password in the product to fall back on.
ROLE_PASSWORD = os.getenv("VANNA_E2E_ROLE_PASSWORD", "Harbour-Lantern-2026!")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="VANNA_E2E_URL is not set"),
]

ARTIFACTS = Path(
    os.getenv("VANNA_E2E_ARTIFACTS") or Path(__file__).resolve().parents[3] / "artifacts"
)

#: The workspace being photographed.
TARGET = "chinook"


class Role:
    """One account, the folder its screenshots go in, and what it may do."""

    def __init__(
        self,
        folder: str,
        email: str,
        workspace: str,
        *,
        may_write: bool,
        may_administer: bool,
        is_member: bool,
    ) -> None:
        self.folder = folder
        self.email = email
        self.workspace = workspace  # the workspace this account actually belongs to
        self.may_write = may_write
        self.may_administer = may_administer
        self.is_member = is_member  # ...of TARGET

    def __repr__(self) -> str:  # what pytest -v prints
        return self.folder


#: `analysis` and `user` are the folder names asked for; the roles behind them are
#: `analyst` and "belongs to another workspace" respectively.
ROLES: List[Role] = [
    Role("admin", "qa.admin@example.com", TARGET,
         may_write=True, may_administer=True, is_member=True),
    Role("analysis", "qa.analyst@example.com", TARGET,
         may_write=True, may_administer=False, is_member=True),
    Role("viewer", "qa.viewer@example.com", TARGET,
         may_write=False, may_administer=False, is_member=True),
    Role("user", "qa.outsider@example.com", "world",
         may_write=False, may_administer=False, is_member=False),
]

#: Sidebar views to photograph. `ask` is the landing view and needs no click.
VIEWS = ["schema", "history", "saved", "dashboards", "account"]


# ----------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------


@pytest.fixture
def page(browser):
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, device_scale_factor=2
    )
    page = context.new_page()
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    yield page
    try:
        page.close()
    except Exception:
        pass  # a crashed page cannot be closed, and that is fine
    context.close()


def _shot(page: Page, folder: str, name: str) -> Path:
    directory = ARTIFACTS / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0, f"{path} was written empty"
    return path


def _sign_in(page: Page, role: Role, *, workspace: str = "") -> None:
    """Sign in, then point the app at a workspace.

    Skips rather than fails when the credentials are rejected: that means the QA
    accounts were never provisioned, which is a setup gap and not a defect.
    """
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    page.fill("#si-email", role.email)
    page.fill("#si-password", ROLE_PASSWORD)
    page.click("#si-go")

    try:
        page.wait_for_selector("#app.ready", timeout=25_000)
    except Exception:
        error = ""
        if page.locator("#si-error").count():
            error = page.locator("#si-error").inner_text()
        pytest.skip(
            f"{role.email} could not sign in ({error or 'no #app.ready'}). "
            "Run tools/provision_role_accounts.py first."
        )

    if workspace:
        page.evaluate(
            "(id) => localStorage.setItem('vanna.identity', JSON.stringify({tenant: id}))",
            workspace,
        )
        page.reload(wait_until="domcontentloaded")
        # An outsider pointed at a workspace they cannot see may never reach
        # `#app.ready`; that is the thing being photographed, so do not insist on it.
        try:
            page.wait_for_selector("#app.ready", timeout=60_000)
        except Exception:
            page.wait_for_timeout(2000)


def _api(page: Page, method: str, path: str, tenant: str, body: Any = None) -> Tuple[int, str]:
    """Call the API from inside the page, as this signed-in user.

    Through the browser rather than with `page.request`, because the CSRF token lives
    in a cookie the shared fetch helper echoes as a header -- and `page.request` does
    not run the page's JavaScript, so it would omit it and every write would 403 for
    the wrong reason.
    """
    return tuple(  # type: ignore[return-value]
        page.evaluate(
            """async ([method, path, tenant, body]) => {
                const token = document.cookie.match(/vanna_csrf=([^;]*)/);
                const headers = {
                    'X-Tenant-Id': tenant,
                    'X-CSRF-Token': token ? decodeURIComponent(token[1]) : '',
                };
                if (body) headers['Content-Type'] = 'application/json';
                const r = await fetch(path, {
                    method, credentials: 'include', headers,
                    body: body ? JSON.stringify(body) : undefined,
                });
                return [r.status, (await r.text()).slice(0, 300)];
            }""",
            [method, path, tenant, body],
        )
    )


def _open_view(page: Page, view: str) -> bool:
    button = page.locator(f"nav.side button[data-view='{view}']")
    if not button.count() or button.is_hidden():
        return False
    button.click()
    panel = page.locator("#view-other")
    if panel.count():
        try:
            expect(panel).to_have_attribute("aria-busy", "false", timeout=20_000)
        except AssertionError:
            pass  # a refused view never clears aria-busy; photograph it anyway
    page.wait_for_timeout(400)
    return True


# ----------------------------------------------------------------------
# What each role sees
# ----------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES, ids=[r.folder for r in ROLES])
class TestEachRole:
    def test_the_sign_in_screen(self, page: Page, role: Role):
        """The same screen for everybody -- captured per folder so each set stands alone."""
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        expect(page.locator("#si-form")).to_be_visible()
        _shot(page, role.folder, "01-sign-in")

    def test_the_workspace(self, page: Page, role: Role):
        _sign_in(page, role, workspace=TARGET)
        page.wait_for_timeout(2500)  # the chat element mounts
        _shot(page, role.folder, "02-workspace")

    @pytest.mark.parametrize("view", VIEWS)
    def test_each_view(self, page: Page, role: Role, view: str):
        _sign_in(page, role, workspace=TARGET)
        if not _open_view(page, view):
            pytest.skip(f"the {view!r} view is not offered to {role.folder}")
        page.set_viewport_size({"width": 1440, "height": 3200})
        page.wait_for_timeout(500)
        _shot(page, role.folder, f"03-{view}")

# ----------------------------------------------------------------------
# The boundary each role sits behind
# ----------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES, ids=[r.folder for r in ROLES])
class TestTheBoundary:
    def test_reading_the_workspace(self, page: Page, role: Role):
        """A member reads; an outsider gets 404 rather than 403."""
        _sign_in(page, role)
        status, body = _api(page, "GET", "/api/vanna/v2/history?limit=5", TARGET)

        if role.is_member:
            assert status == 200, f"{role.folder} cannot read {TARGET}: {status} {body}"
        else:
            # 403 or 404 -- see TestWorkspaceEnumeration for why it is currently 403.
            # What matters here is that no row came back.
            assert status in (403, 404), f"an outsider read {TARGET}: {status} {body}"
            assert '"history"' not in body, f"an outsider was handed data: {body}"

    def test_writing_a_saved_query(self, page: Page, role: Role):
        """Three different outcomes, and the difference between them is the design."""
        _sign_in(page, role)
        status, body = _api(
            page, "POST", "/api/vanna/v2/saved-queries", TARGET,
            {"title": f"role probe {role.folder}", "sql": "SELECT 1", "question": ""},
        )

        if role.may_write:
            assert status == 200, f"{role.folder} should be able to save: {status} {body}"
            # Leave nothing behind -- a probe that litters the saved list is a probe
            # that changes what the next screenshot run photographs.
            saved_id = ""
            try:
                import json as _json

                saved_id = (_json.loads(body).get("saved") or {}).get("id", "")
            except Exception:
                pass
            if saved_id:
                _api(page, "DELETE", f"/api/vanna/v2/saved-queries/{saved_id}", TARGET)
        elif role.is_member:
            assert status == 403, (
                f"a viewer should get 403 with a reason, not {status}: {body}"
            )
            assert "viewer" in body.lower(), (
                f"the refusal should name the role so it is actionable: {body}"
            )
        else:
            assert status in (403, 404), (
                f"an outsider should be refused, got {status}: {body}"
            )

    def test_administering_the_workspace(self, page: Page, role: Role):
        """The member roster is admin-only, and a non-admin must not learn it exists."""
        _sign_in(page, role)
        status, body = _api(
            page, "GET", f"/api/vanna/v2/admin/tenants/{TARGET}/users", TARGET
        )

        if role.may_administer:
            assert status == 200, f"{role.folder} cannot see members: {status} {body}"
        elif role.is_member:
            # A member who is not an admin must not learn the roster exists, and the
            # route guards get this right.
            assert status == 404, (
                f"{role.folder} got {status} for the member roster, not 404: {body}"
            )
        else:
            assert status in (403, 404), f"an outsider saw the roster: {status} {body}"
            assert '"users"' not in body, f"an outsider was handed the roster: {body}"

    def test_the_outsider_still_has_their_own_workspace(self, page: Page, role: Role):
        """Isolation is not the same as being locked out.

        Worth asserting separately: a test that only checks the outsider is refused
        would also pass if their account were simply broken.
        """
        if role.is_member:
            pytest.skip("only meaningful for the outsider")

        _sign_in(page, role, workspace=role.workspace)
        status, body = _api(page, "GET", "/api/vanna/v2/history?limit=5", role.workspace)
        assert status == 200, (
            f"the outsider cannot read their own workspace {role.workspace}: "
            f"{status} {body}"
        )
        _shot(page, role.folder, "05-own-workspace")


# ----------------------------------------------------------------------
# A stated property that does not currently hold
# ----------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "identity.py refuses a non-member with 403 and two distinguishable messages, "
        "so any authenticated user can enumerate which workspaces exist. The route "
        "guards get this right; tenant resolution runs before them and does not."
    ),
)
def test_a_non_member_cannot_tell_which_workspaces_exist(page: Page):
    """The README's promise, asserted directly.

    > Refusals are `404`, never `403`: a `403` confirms the resource exists to
    > somebody who has no business knowing that it does.

    That holds for the admin routes. It does not hold for workspace resolution, which
    happens first: `backend/vanna_app/identity.py:205-216` raises one message for a
    workspace that exists and a different one for a workspace that does not, both as
    403. Comparing the two is a membership-free directory of every workspace on the
    deployment.

    Marked strict, so the day the behaviour is fixed this test fails and asks to have
    the marker removed rather than sitting here as permanently-expected breakage.
    """
    outsider = ROLES[-1]
    assert not outsider.is_member, "the last role is meant to be the outsider"
    _sign_in(page, outsider)

    real, real_body = _api(page, "GET", "/api/vanna/v2/history?limit=1", TARGET)
    fake, fake_body = _api(
        page, "GET", "/api/vanna/v2/history?limit=1", "no-such-workspace-a9f3c1"
    )

    assert real == fake, (
        f"a real workspace answers {real} and an imaginary one {fake}, so the status "
        "alone reveals which exist"
    )
    assert real_body == fake_body, (
        "the refusal bodies differ, so they reveal which workspaces exist -- "
        f"real: {real_body} / fake: {fake_body}"
    )


# ----------------------------------------------------------------------
# The admin console, tab by tab
# ----------------------------------------------------------------------

#: Every tab `tabsFor()` in console.js can build, in its own order. The first two are
#: gated on `me.is_platform_admin`, which is the distinction this section exists to
#: make visible: a *workspace* admin runs the console without them.
CONSOLE_TABS = [
    "tenants",
    "accounts",
    "members",
    "permissions",
    "billing",
    "review",
    "verified",
    "domains",
    "rules",
    "library",
    "starters",
    "new",
]

#: Offered only to a platform admin -- an address in VANNA_ADMIN_EMAILS.
PLATFORM_ONLY = {"tenants", "accounts"}

#: The workspace admin among the four; the only role that can use the console.
ADMIN_ROLE = ROLES[0]


def _open_console(page: Page, role: Role) -> None:
    _sign_in(page, role, workspace=TARGET)
    page.goto(f"{BASE_URL}/admin/", wait_until="networkidle")
    page.wait_for_timeout(2000)


class TestTheConsoleTabStrip:
    """Which tabs each role is offered, which is a permission decision made in the UI.

    Worth asserting separately from the API guards. `tabsFor()` hides the two
    platform-admin tabs client-side, and a client-side hide is a courtesy rather than
    a control -- the API refuses regardless. But if the *hide* regresses, a workspace
    admin sees a Workspaces tab that 404s on every click, which reads as a broken
    product rather than as a boundary.
    """

    @pytest.mark.parametrize("role", ROLES, ids=[r.folder for r in ROLES])
    def test_the_tabs_on_offer(self, page: Page, role: Role):
        _open_console(page, role)
        offered = set(
            page.eval_on_selector_all(
                "#tabs button", "els => els.map(e => e.dataset.tab).filter(Boolean)"
            )
        )
        _shot(page, role.folder, "04-admin-console")

        if not role.may_administer:
            # A non-admin is refused by the API behind the console. Whatever the tab
            # strip does, none of these panels may carry data -- recorded as an image
            # and asserted below in test_a_non_admin_gets_nothing_from_the_console.
            return

        assert offered, f"{role.folder} is an admin but was offered no tabs"
        assert not (offered & PLATFORM_ONLY), (
            f"a workspace admin was offered platform-admin tabs {offered & PLATFORM_ONLY}; "
            "every one of them 404s, so offering them reads as breakage"
        )
        expected = set(CONSOLE_TABS) - PLATFORM_ONLY
        missing = expected - offered
        assert not missing, f"the console is missing tabs a workspace admin needs: {missing}"

    @pytest.mark.parametrize(
        "role", [r for r in ROLES if not r.may_administer],
        ids=[r.folder for r in ROLES if not r.may_administer],
    )
    def test_a_non_admin_gets_nothing_from_the_console(self, page: Page, role: Role):
        """The console is a static asset; the refusal is in the API behind it.

        So the interesting assertion is not "the page did not load" -- it did -- but
        that every admin endpoint it calls refuses.
        """
        _sign_in(page, role)
        for path in (
            f"/api/vanna/v2/admin/tenants/{TARGET}/users",
            f"/api/vanna/v2/admin/tenants/{TARGET}/starters",
            f"/api/vanna/v2/admin/tenants/{TARGET}/instructions",
        ):
            status, body = _api(page, "GET", path, TARGET)
            assert status in (403, 404), (
                f"{role.folder} reached {path}: {status} {body}"
            )


@pytest.mark.parametrize("tab", CONSOLE_TABS)
def test_each_console_tab_as_a_workspace_admin(page: Page, tab: str):
    """One image per tab, for the role that actually operates the console.

    Only the workspace admin gets the per-tab treatment. The other three cannot use
    the console at all, so twelve images of the same refused screen would be twelve
    files nobody opens -- they get one, from the tab-strip test above.
    """
    _open_console(page, ADMIN_ROLE)

    button = page.locator(f"#tabs button[data-tab='{tab}']")
    if not button.count():
        if tab in PLATFORM_ONLY:
            pytest.skip(f"{tab!r} is platform-admin only, and this is a workspace admin")
        pytest.fail(f"the console offers no {tab!r} tab")

    button.click()
    page.wait_for_timeout(1500)  # each tab fetches its own rows
    page.set_viewport_size({"width": 1440, "height": 3200})
    page.wait_for_timeout(500)

    _shot(page, ADMIN_ROLE.folder, f"06-console-{tab}")
    assert not page._errors, f"the {tab} tab raised: {page._errors}"
