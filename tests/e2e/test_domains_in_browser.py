"""Every provisioned workspace, driven through the real UI.

One workspace with one database proves the plumbing. Eight workspaces, each bound to
a *different* database with its own business rules, prove the thing the product is
sold on: that switching workspace switches the data, the schema, the knowledge and
the starter questions -- and that none of them leaks into another.

Two tiers, because they cost very different amounts:

* **structure** -- schema, starters, rules and isolation, per workspace. No LLM, so
  it is fast and deterministic and runs in full.
* **answers** -- an actual question through the agent. Costs an API call and tens of
  seconds each, so it runs for a sample unless ``VANNA_E2E_ALL_DOMAINS`` is set.

Provision first:

    docker compose exec api python -m vanna_app.domains provision
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
from playwright.sync_api import Page, expect  # noqa: E402

BASE_URL = os.getenv("VANNA_E2E_URL", "").rstrip("/")
EMAIL = os.getenv("VANNA_E2E_EMAIL", "demo@example.com")
PASSWORD = os.getenv("VANNA_E2E_PASSWORD", "")
ALL_DOMAINS = os.getenv("VANNA_E2E_ALL_DOMAINS", "").lower() in ("1", "true", "yes")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="VANNA_E2E_URL is not set"),
    pytest.mark.skipif(not PASSWORD, reason="VANNA_E2E_PASSWORD is not set"),
]

ROOT = Path(__file__).resolve().parents[2]


def _definitions() -> List[Dict[str, Any]]:
    import sys

    sys.path.insert(0, str(ROOT / "backend"))
    from vanna_app.domains import load_definitions

    return load_definitions(ROOT / "backend" / "domains" / "domains.yml")


DOMAINS = _definitions()
IDS = [d["id"] for d in DOMAINS]

#: Which workspaces get a real question asked. One small, one large, one with a
#: partitioned table -- the three shapes most likely to behave differently.
SAMPLED = IDS if ALL_DOMAINS else ["chinook", "world", "pagila"]


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    page._errors = []
    page.on("pageerror", lambda e: page._errors.append(str(e)))
    yield page
    # Close the page before the context. Eight workspaces in one browser process,
    # each rendering a result grid and a Plotly chart, exhausted the renderer part
    # way through the run -- the eighth test failed with "Page crashed" while
    # passing on its own. Closing the page explicitly releases the renderer
    # instead of leaving it to whenever the context teardown gets to it.
    try:
        page.close()
    except Exception:
        pass  # a page that already crashed cannot be closed, and that is fine
    context.close()


def _api(page: Page, path: str, tenant: str, *, timeout: float = 30_000) -> Any:
    """Call the API the way the page does.

    `page.request` shares the browser's cookies but *not* its JavaScript, so it does
    not go through the shared fetch helper and does not add `X-Tenant-Id`. Without
    that header the request resolves to the caller's default workspace -- which
    looked exactly like "the rules were never provisioned".
    """
    response = page.request.get(
        f"{BASE_URL}{path}", headers={"X-Tenant-Id": tenant}, timeout=timeout
    )
    assert response.status == 200, f"{path} -> {response.status} {response.text()[:200]}"
    return response.json()


def _sign_in(page: Page, tenant: str = "") -> None:
    """Sign in, then switch workspace by writing the stored selection.

    The switcher reloads the page, which is slower and flakier than setting the one
    value it persists. The server still checks membership on every request, so this
    is a shortcut through the UI, not around the authorisation.
    """
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    if page.locator("#si-email").is_visible():
        page.fill("#si-email", EMAIL)
        page.fill("#si-password", PASSWORD)
        page.click("#si-go")
        page.wait_for_selector("#app.ready", timeout=20_000)

    if tenant:
        page.evaluate(
            "(id) => localStorage.setItem('vanna.identity', JSON.stringify({tenant: id}))",
            tenant,
        )
        # `domcontentloaded`, not `networkidle`, and then wait on the app's own
        # signal. A cold workspace builds its runtime and scans its database on
        # first open -- employees has 3.9 million rows and pagila 71 tables -- so
        # the network is still busy well past the 30s navigation default, and the
        # reload failed on a workspace that was loading perfectly well. `#app.ready`
        # is what the page itself sets when it is usable; waiting for anything else
        # is guessing.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#app.ready", timeout=180_000)



#: Walks open shadow roots. `page.locator("body").inner_text()` does not, and the
#: chat transcript lives entirely inside one -- so any assertion written against the
#: light DOM is checking text that cannot be there.
#:
#: STYLE and SCRIPT are skipped deliberately. `textContent` includes the body of a
#: `<style>` tag, and the components ship their CSS inline: the first version of
#: this returned 46,000 characters of stylesheet for a page showing "Start a
#: conversation". A length check meant to prove the shadow root had been read was
#: satisfied entirely by CSS.
_DEEP_TEXT = """
() => {
  const parts = [];
  const walk = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const tag = node.parentElement && node.parentElement.tagName;
      if (tag === 'STYLE' || tag === 'SCRIPT') continue;
      const text = node.textContent.trim();
      if (text) parts.push(text);
    }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  return parts.join(' | ');
}
"""


def _transcript(page: Page) -> str:
    return page.evaluate(_DEEP_TEXT)


def _ask(page: Page, question: str) -> None:
    """Start a fresh conversation, then type a question and send it.

    The new chat is not politeness. Opening a workspace restores its most recent
    conversation, so the transcript on screen contains every earlier exchange --
    including failures from previous runs. Read that way, ``world`` and ``pagila``
    failed on a policy rejection recorded hours earlier while passing when run
    alone, which reads exactly like flakiness and is not.

    The input lives inside the component and appears only once it has initialised,
    which after a workspace switch takes noticeably longer than the page load.
    Waiting for it is the difference between asking a question and silently not.
    """
    page.click("#new-chat")

    box = page.locator("input[placeholder*='Ask'], textarea[placeholder*='Ask']").first
    box.wait_for(state="visible", timeout=60_000)
    box.fill(question)
    box.press("Enter")


def _newest_generation_id(page: Page, tenant: str) -> Optional[str]:
    """The id of the most recent generation, or None when there are none yet."""
    rows = _api(page, "/api/vanna/v2/history?limit=1", tenant)["history"]
    return rows[0]["id"] if rows else None


def _wait_for_generation(
    page: Page, tenant: str, *, newer_than: Optional[str], timeout: int = 300
) -> list:
    """Poll the history API until a generation newer than ``newer_than`` appears.

    Server-side truth. A generation row means SQL reached the database; its absence
    means the question did not, whatever the page happens to be showing.

    Identity, not arithmetic. This used to count the rows in ``?limit=50`` and wait
    for the count to grow, which silently stops working the moment a workspace has
    fifty generations: the page saturates, the count is 50 before and 50 after, and
    the poll runs its full five minutes and reports that "the question never reached
    the database" while the answer sits in the history screen. Seeding one workspace
    with demo data was enough to trip it, and any workspace anybody has actually used
    would have tripped it too -- the test got less able to see the truth the more the
    application had been used, which is the wrong direction for a test to fail in.

    Five minutes is not padding. Signing in warms the workspace on *one* of four
    uvicorn workers, and each worker keeps its own bounded runtime cache; the chat
    request is free to land on a different one, which then builds the runtime and
    rescans the schema before the first token. Against `employees` (3.9 million
    rows) that alone outran the previous 120s budget, and the failure read as "the
    question never reached the database" when it simply had not arrived yet.

    A poll that times out is treated as "not yet", not as a failure. While a
    `chat_sse` stream is in flight -- one was logged at 59.7s -- other requests to
    the same host queue behind it, and a poll that waited 30s and gave up says
    nothing whatsoever about whether the question ran. Only this function's own
    deadline ends the wait.
    """
    import time

    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = _api(page, "/api/vanna/v2/history?limit=50", tenant)["history"]
        except PlaywrightTimeout:
            continue
        if rows and rows[0].get("id") != newer_than:
            return rows
        page.wait_for_timeout(3000)
    return []


# ----------------------------------------------------------------------
# Structure
# ----------------------------------------------------------------------


@pytest.mark.parametrize("domain", DOMAINS, ids=IDS)
class TestEachWorkspace:
    def test_it_opens_and_names_its_own_database(self, page: Page, domain: Dict[str, Any]):
        _sign_in(page, domain["id"])

        # The workspace name and its data source used to sit in the header as loose
        # text; they now live in the account sheet behind the rail identity button.
        # Same two facts, one click further in, so open it and read them there.
        page.click("#rail-account")
        page.wait_for_selector("#sheet h3", timeout=10_000)
        sheet = page.locator("#sheet")
        expect(sheet).to_contain_text(domain["name"])
        source = sheet.inner_text()
        assert domain["database"] in source, f"{domain['id']} shows {source!r}"
        assert not page._errors, page._errors

    def test_its_starter_questions_are_its_own(self, page: Page, domain: Dict[str, Any]):
        _sign_in(page, domain["id"])
        page.wait_for_timeout(1200)

        shown = page.locator("#starters .starter").all_inner_texts()
        expected = [" ".join(q.split()) for q in domain.get("starters") or []]
        for question in expected:
            assert any(question in s for s in shown), (
                f"{domain['id']} is missing starter {question!r}; showing {shown}"
            )

    def test_the_schema_screen_shows_that_database_s_tables(
        self, page: Page, domain: Dict[str, Any]
    ):
        _sign_in(page, domain["id"])
        page.click("nav.side button[data-view='schema']")
        panel = page.locator("#view-other")
        expect(panel).to_have_attribute("aria-busy", "false", timeout=180_000)

        text = panel.inner_text()
        assert domain["database"] in text, (
            f"{domain['id']}'s schema screen does not mention {domain['database']}"
        )

    def test_the_usage_widget_reports_this_workspace(self, page: Page, domain: Dict[str, Any]):
        _sign_in(page, domain["id"])
        page.wait_for_timeout(1200)
        # Quota is per workspace, and the sidebar says so.
        assert "/" in page.locator("#usage-note").inner_text()


class TestKnowledgeIsPerWorkspace:
    """Business rules must not cross workspaces.

    Retrieval pulls instructions into the prompt. A rule leaking between workspaces
    would tell one customer's model about another customer's schema -- which is a
    disclosure, not just a wrong answer.
    """

    def _rules(self, page: Page, tenant: str) -> List[str]:
        """This workspace's rules, from the tenant-scoped endpoint.

        Not the library's `/admin/instructions`, which this used to call. That
        one is served no store now: its guard reads group membership with no
        tenant in the path to check against, so it refuses a platform admin
        working on another workspace and cannot express "this workspace's rules"
        at all -- which is the exact property being tested here.
        """
        _sign_in(page, tenant)
        payload = _api(
            page, f"/api/vanna/v2/admin/tenants/{tenant}/instructions", tenant
        )
        return [i["text"] for i in payload.get("instructions", [])]

    @pytest.mark.parametrize("domain", DOMAINS, ids=IDS)
    def test_each_workspace_has_its_own_rules(self, page: Page, domain: Dict[str, Any]):
        texts = self._rules(page, domain["id"])
        for rule in domain.get("instructions") or []:
            wanted = " ".join(str(rule["text"]).split())
            assert any(wanted in t for t in texts), (
                f"{domain['id']} is missing a rule it was provisioned with"
            )

    def test_a_rule_from_one_workspace_is_not_visible_in_another(self, page: Page):
        # A phrase that appears in exactly one domain's rules.
        pagila_rules = self._rules(page, "pagila")
        assert any("partitioned by month" in t for t in pagila_rules)

        world_rules = self._rules(page, "world")
        assert not any("partitioned by month" in t for t in world_rules), (
            "a Pagila rule is visible inside the World workspace"
        )

    def test_the_platform_baseline_is_shared_and_that_is_not_a_leak(self, page: Page):
        """Two rules in one list, owned by different people.

        Every workspace now sees the deployment-wide baseline alongside its own
        rules, and a reader comparing two workspaces will find identical text in
        both. That is the design, not the leak the test above is about -- so it
        is asserted here rather than left to be "fixed" by somebody tightening
        the isolation check.
        """
        pagila = self._rules(page, "pagila")
        world = self._rules(page, "world")

        shared = set(pagila) & set(world)
        assert shared, "the platform baseline reached neither workspace"
        assert all("schema context" in s or "unit of any number" in s
                   or "cut down the rows" in s or "explicit column list" in s
                   or "explicit JOIN" in s for s in shared), (
            f"workspaces share text that is not a platform rule: {sorted(shared)}"
        )

    def test_the_schema_of_one_workspace_is_not_visible_in_another(self, page: Page):
        _sign_in(page, "world")
        page.click("nav.side button[data-view='schema']")
        expect(page.locator("#view-other")).to_have_attribute(
            "aria-busy", "false", timeout=180_000
        )
        text = page.locator("#view-other").inner_text()

        assert "country" in text.lower(), "the World schema did not load"
        for stranger in ("invoice_line", "prescriptions", "cart_items"):
            assert stranger not in text.lower(), (
                f"{stranger} belongs to another workspace and is visible in World"
            )


# ----------------------------------------------------------------------
# Answers
# ----------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("domain", [d for d in DOMAINS if d["id"] in SAMPLED],
                         ids=[i for i in IDS if i in SAMPLED])
class TestItAnswers:
    """A real question, through the agent, against that workspace's database.

    Verified through the **API**, not the page.

    The first version of this read `body.inner_text()` and asserted that no error
    string appeared. <vanna-chat> renders into a shadow root, so that text was 443
    characters of page chrome and never contained the transcript at all -- the
    assertions could not fail. Two workspaces "passed" having recorded no
    generations, which is to say having done nothing.

    A negative assertion over text that cannot appear is worse than no test. The
    generation store is the authority: a question that ran leaves a row, with the
    SQL it executed and whether it worked.
    """

    def test_a_question_runs_and_is_recorded(self, page: Page, domain: Dict[str, Any]):
        """One question, checked twice: what the server recorded and what the user saw.

        Both assertions in one test because asking costs an LLM call and thirty to a
        hundred seconds. Splitting them would double that to check the same question.

        Note the order: the baseline is read *after* signing in. Reading it first sent
        an unauthenticated request and the helper failed on the 401 -- before the
        browser had ever loaded the workspace.
        """
        _sign_in(page, domain["id"])
        before = _newest_generation_id(page, domain["id"])

        _ask(page, domain["starters"][0])

        rows = _wait_for_generation(page, domain["id"], newer_than=before)
        assert rows, (
            f"{domain['id']}: no generation was recorded within the timeout, so the "
            "question never reached the database"
        )

        newest = rows[0]
        assert newest["status"] != "invalid", (
            f"{domain['id']}: the SQL failed -- {newest.get('error')!r} "
            f"for {newest.get('sql', '')[:300]!r}"
        )
        assert (newest.get("sql") or "").strip(), (
            f"{domain['id']}: a generation with no SQL"
        )

        # And the page, read through the shadow root where the transcript actually is.
        #
        # The question is the canary: it is rendered by the chat component, inside
        # the shadow root, so its presence proves the walker reached the transcript.
        # Without a positive check like this the negative ones below pass on any
        # string that fails to contain an error -- including the empty one.
        transcript = _transcript(page)
        assert domain["starters"][0] in transcript, (
            f"{domain['id']}: the question is not in the text read from the page, so "
            "the transcript was not reached and the checks below would be vacuous"
        )
        assert "Error Processing Message" not in transcript, (
            f"{domain['id']}: an unhandled exception reached the user"
        )

        # Not every policy rejection is a defect. Booking asked for a date spine,
        # the policy refused `generate_series` as a query source -- an unbounded
        # row generator is a deliberate denial-of-service guard -- and the agent
        # rewrote the query and answered. That is the policy working.
        #
        # These three are different. Each is the policy parsing something that is
        # not SQL and refusing it as though it were: a plain-English catalog
        # search that parses as an ALIAS, a column reference, or the semicolon
        # ending a perfectly good query. They are false positives by construction,
        # and the user cannot act on any of them.
        for false_positive in ("ALIAS statements", "COLUMN statements", "SEMICOLON statements"):
            assert false_positive not in transcript, (
                f"{domain['id']}: the policy refused {false_positive.split()[0]} -- "
                "that is prose or punctuation being parsed as SQL, not a real refusal"
            )
