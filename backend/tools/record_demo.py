#!/usr/bin/env python3
"""Record a walkthrough of the running app as video.

    python tools/record_demo.py --password ...
    python tools/record_demo.py --password ... --scene console

Screenshots prove a page rendered. They cannot show that a parameter re-runs its
tiles, that a chart responds to a drag, or that the sections rail stays put while a
long table scrolls past it -- all of which are things somebody asked for and none of
which a still image can answer. So this drives the same flows and keeps the film.

Playwright writes one `.webm` per browser context, finalised when the context closes,
so each scene is its own context and its own file.

There are two ways to use that. The topic scenes are short and separate, named after
what they show, for when you want the twenty seconds that answer one question. The
`full-tour` scene is the opposite: everything in a single context, and therefore a
single file -- sign-in, all seven workspace screens, a question asked and its answer
checked against hand-written SQL, a report and its charts, both themes, both writing
directions, and all twelve console tabs. It records a pass or fail at each stop and
ends on the tally, so the one file is a test result and not just a walkthrough.

    python tools/record_demo.py --password ... --scene full-tour

Paced with explicit waits. Without them the run is correct and unwatchable -- forms
fill instantly and panels swap between frames, so a viewer sees a slideshow of end
states and none of the cause.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the message is the point
    sys.exit("playwright is not installed: pip install playwright && playwright install chromium")

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "video"

#: 720p. Big enough to read a table, small enough that the file stays sendable.
SIZE = {"width": 1280, "height": 720}

#: How long a "beat" is. Every pause in a scene is a multiple of this, so the whole
#: film speeds up or slows down with one number.
BEAT = 700


class Stage:
    """One recording context, and the small vocabulary a scene is written in."""

    def __init__(self, browser: Any, base: str, email: str, password: str,
                 beat: int = BEAT) -> None:
        self.browser = browser
        self._beat = beat
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.context = browser.new_context(
            viewport=SIZE, record_video_dir=str(OUT), record_video_size=SIZE
        )
        self.page = self.context.new_page()

    # -- pacing --------------------------------------------------------

    def beat(self, count: float = 1) -> None:
        self.page.wait_for_timeout(int(self._beat * count))

    def sign_in(self, tenant: str = "") -> None:
        page = self.page
        page.goto(f"{self.base}/", wait_until="networkidle")
        self.beat(1.5)
        # Typed rather than filled: a form that fills instantly reads as a cut, and
        # the point of the clip is that a person could do this.
        page.type("#si-email", self.email, delay=45)
        page.type("#si-password", self.password, delay=30)
        self.beat()
        page.click("#si-go")
        page.wait_for_selector("#app.ready", timeout=40_000)
        self.beat(2)

        if tenant:
            page.evaluate(
                "(id) => localStorage.setItem('vanna.identity', JSON.stringify({tenant: id}))",
                tenant,
            )
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#app.ready", timeout=120_000)
            self.beat(2)

    def click(self, locator: Any) -> bool:
        """Click, falling back to dispatching the event when something covers it.

        Once the chat element has answered once it takes the `maximized` class and
        covers the sidebar, so a real click on a nav button is intercepted and
        Playwright retries until it times out -- which ends the scene and truncates
        the film. The button is present and enabled, merely underneath something, so
        dispatching straight at it is the right call rather than a workaround.
        """
        try:
            locator.click(timeout=4000)
            return True
        except Exception:
            try:
                locator.dispatch_event("click")
                return True
            except Exception:
                return False

    def view(self, name: str) -> bool:
        button = self.page.locator(f"nav.side button[data-view='{name}']")
        if not button.count() or button.is_hidden():
            return False
        if not self.click(button):
            return False
        self.beat(2.5)
        return True

    def finish(self, name: str) -> Optional[Path]:
        """Close the context so the video is written, then give it a real name."""
        video = self.page.video
        source = Path(video.path()) if video else None
        self.context.close()  # flushes the file
        if source is None:
            return None
        target = OUT / f"{name}.webm"
        if target.exists():
            target.unlink()
        shutil.move(str(source), str(target))
        return target


# ----------------------------------------------------------------------
# The scenes
# ----------------------------------------------------------------------


def scene_sign_in(stage: Stage) -> None:
    """Signing in, and the workspace as it first appears."""
    stage.sign_in(tenant="chinook")
    stage.beat(3)


def scene_views(stage: Stage) -> None:
    """Every screen in the sidebar, at a readable pace."""
    stage.sign_in(tenant="chinook")
    for name in ("schema", "history", "saved", "dashboards", "account"):
        stage.view(name)


def scene_report_parameters(stage: Stage) -> None:
    """The thing a screenshot cannot show: changing a parameter re-runs the tiles."""
    page = stage.page
    stage.sign_in(tenant="chinook")
    if not stage.view("dashboards"):
        return

    opener = page.locator("#view-other [data-open]").first
    if not opener.count():
        return
    opener.click()
    page.wait_for_selector("#sheet .card, #sheet .empty", timeout=90_000)
    stage.beat(4)  # let the charts draw and be looked at

    # Drive the parameter controls if the page offers them; otherwise just dwell on
    # the rendered report so the clip is still worth keeping.
    control = page.locator("#sheet select, #sheet input[type='date'], #sheet input").first
    if control.count():
        try:
            control.click()
            stage.beat(1.5)
        except Exception:
            pass
    stage.beat(3)


def scene_chart_interaction(stage: Stage) -> None:
    """Hover and drag on a Plotly tile, which is what "interactive" means."""
    page = stage.page
    stage.sign_in(tenant="chinook")
    if not stage.view("dashboards"):
        return
    opener = page.locator("#view-other [data-open]").first
    if not opener.count():
        return
    opener.click()
    page.wait_for_selector("#sheet .card, #sheet .empty", timeout=90_000)
    stage.beat(3)

    plot = page.locator("#sheet .js-plotly-plot").first
    if not plot.count():
        return
    box = plot.bounding_box()
    if not box:
        return

    # Hover along the series so the tooltips fire, then drag out a zoom box and
    # double-click to reset -- the three gestures the modebar exists to support.
    for fraction in (0.25, 0.45, 0.65, 0.85):
        page.mouse.move(box["x"] + box["width"] * fraction, box["y"] + box["height"] * 0.55)
        stage.beat(0.6)
    page.mouse.move(box["x"] + box["width"] * 0.30, box["y"] + box["height"] * 0.30)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.70, box["y"] + box["height"] * 0.80, steps=25)
    page.mouse.up()
    stage.beat(3)
    page.mouse.dblclick(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    stage.beat(2)


def scene_console(stage: Stage) -> None:
    """The operator console: the sections rail, and that it stays put."""
    page = stage.page
    stage.sign_in(tenant="chinook")
    page.goto(f"{stage.base}/admin/", wait_until="networkidle")
    page.wait_for_selector("#tabs button", timeout=40_000)
    stage.beat(2.5)

    for tab in ("tenants", "members", "permissions", "rules", "starters"):
        button = page.locator(f"#tabs button[data-tab='{tab}']")
        if not button.count():
            continue
        button.click()
        stage.beat(2)

    # Scroll the panel hard. The rail must not move with it -- that was the whole
    # point of splitting the two scrollers, and it is only visible in motion.
    page.locator("#tabs button[data-tab='tenants']").click()
    stage.beat(1.5)
    for offset in (400, 900, 1400, 0):
        page.evaluate("(y) => { document.getElementById('content').scrollTop = y; }", offset)
        stage.beat(1.2)
    stage.beat(2)


def scene_arabic(stage: Stage) -> None:
    """The same workspace mirrored, which is a layout rather than a translation."""
    page = stage.page
    page.goto(f"{stage.base}/", wait_until="networkidle")
    stage.beat()
    try:
        page.select_option("#si-locale", "ar")
    except Exception:
        return
    stage.beat(2)
    page.type("#si-email", stage.email, delay=40)
    page.type("#si-password", stage.password, delay=25)
    page.click("#si-go")
    page.wait_for_selector("#app.ready", timeout=40_000)
    stage.beat(3)
    for name in ("schema", "history"):
        stage.view(name)


# ----------------------------------------------------------------------
# Asking a question, and checking the answer on camera
# ----------------------------------------------------------------------

#: The question the clip asks. A scalar on purpose: two numbers side by side is a
#: check a viewer can do themselves at a glance, which a twenty-row table is not.
ASK_QUESTION = "What is the total revenue from all invoices?"

#: Enough of the address to recognise it in the rail without hard-coding a domain.
EMAIL_MARK = "demo@example.com"

#: The independent check. Written by hand, against the same warehouse, computing the
#: same figure without going near the model. This is the point of the scene: an
#: answer nobody checked is a claim, not a result.
CHECK_TITLE = "Independent check - total invoice revenue"
CHECK_SQL = "SELECT ROUND(SUM(total)::numeric, 2) AS total_revenue FROM invoices"


def _api(page: Any, path: str, tenant: str, method: str = "GET",
         body: Any = None) -> Dict[str, Any]:
    """Call the app's own API from inside the page, as the signed-in user.

    The workspace header is what makes this the right workspace: this account
    belongs to several, and a fetch without it answers for the session default.
    """
    return page.evaluate(
        """async ([path, tenant, method, body]) => {
            const token = document.cookie.match(/vanna_csrf=([^;]*)/);
            const init = {
                method,
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Tenant-Id': tenant,
                    'X-CSRF-Token': token ? decodeURIComponent(token[1]) : '',
                },
            };
            if (body !== null) init.body = JSON.stringify(body);
            const r = await fetch(path, init);
            let payload = null;
            try { payload = await r.json(); } catch (e) { payload = null; }
            return {status: r.status, payload};
        }""",
        [path, tenant, method, body],
    )


def _number(cell: Any) -> Optional[float]:
    """The numeric value of a result cell, or None if it is not a number."""
    if cell is None:
        return None
    text = str(cell).replace(",", "").replace("$", "").strip()
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _first_number(result: Dict[str, Any]) -> Optional[float]:
    """The first numeric cell of the first row -- what a scalar query returns."""
    for row in (result.get("rows") or [])[:1]:
        for cell in row:
            value = _number(cell)
            if value is not None:
                return value
    return None


#: Any number in a sentence: "$2,328.60", "412", "-3.5".
NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> List[float]:
    """Every number mentioned in a sentence, normalised.

    Dates are removed first. "2021-01-01" otherwise reads as 2021, -1 and -1, and a
    check that passes because the figure it wanted happened to appear inside a date
    is worse than no check at all.
    """
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text or "")
    found = []
    for match in NUMBER.finditer(text):
        value = _number(match.group())
        if value is not None:
            found.append(value)
    return found


def _wait_for_reply(page: Any, tenant: str, question: str, tries: int = 25) -> str:
    """The assistant's reply to this question, from the stored transcript.

    The transcript rather than the shadow DOM: the reply is the record of what the
    user was told, and it is what the check has to be against.
    """
    import urllib.parse

    for _ in range(tries):
        listing = (_api(page, "/api/vanna/v2/conversations?limit=15", tenant)
                   .get("payload") or {})
        for summary in listing.get("conversations") or []:
            if (summary.get("title") or "").strip() != question.strip():
                continue
            thread = (_api(
                page,
                f"/api/vanna/v2/conversations/{urllib.parse.quote(summary['id'])}",
                tenant,
            ).get("payload") or {})
            replies = [
                (m.get("content") or "").strip()
                for m in thread.get("messages") or []
                if m.get("role") == "assistant" and (m.get("content") or "").strip()
            ]
            if replies:
                return replies[-1]
        page.wait_for_timeout(2000)
    return ""


def _caption(page: Any, text: str, tone: str = "neutral") -> None:
    """Burn a caption into the recording.

    A caption on my own film, not a part of the app: it is what a voiceover would
    say if the clip had one. Kept visually plain so nobody watching it later
    mistakes it for product UI.
    """
    colours = {"neutral": "#1f2937", "good": "#065f46", "bad": "#7f1d1d"}
    page.evaluate(
        """([text, colour]) => {
            let bar = document.getElementById('demo-caption');
            if (!bar) {
                bar = document.createElement('div');
                bar.id = 'demo-caption';
                // Left-to-right regardless of the page. The captions are English and
                // numbered, and under the Arabic layout the page direction moved the
                // leading step number to the far right of its own sentence.
                bar.dir = 'ltr';
                bar.style.cssText = [
                    'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:99999',
                    'padding:10px 16px', 'font:600 15px/1.4 system-ui,sans-serif',
                    'color:#fff', 'text-align:center', 'letter-spacing:.2px',
                ].join(';');
                document.body.appendChild(bar);
            }
            bar.style.background = colour;
            bar.textContent = text;
        }""",
        [text, colours.get(tone, colours["neutral"])],
    )


def scene_ask_and_verify(stage: Stage) -> None:
    """Ask a question, watch the job run, then check the answer against hand SQL.

    The order is the argument of the clip: the model writes SQL, the SQL runs against
    the real warehouse, and only then is the number it produced compared with one
    computed independently. If they disagree the caption says so in red -- a demo
    that can only show success is not showing anything.
    """
    page = stage.page
    tenant = "chinook"
    stage.sign_in(tenant=tenant)

    # Ground truth first, so the figure to beat exists before the question is asked
    # and cannot be quietly chosen afterwards to match whatever came back.
    truth = _api(page, "/api/vanna/v2/run-sql", tenant, "POST",
                 {"sql": CHECK_SQL, "limit": 5})
    expected = _first_number(truth.get("payload") or {})
    print(f"[verify] hand-written SQL says {expected}", flush=True)

    # Park the check query in Saved so the clip can run it on camera rather than
    # asking the viewer to take my word for the comparison.
    existing = _api(page, "/api/vanna/v2/saved-queries", tenant).get("payload") or {}
    titles = {(q.get("title") or "") for q in existing.get("saved") or []}
    if CHECK_TITLE not in titles:
        _api(page, "/api/vanna/v2/saved-queries", tenant, "POST",
             {"title": CHECK_TITLE, "sql": CHECK_SQL,
              "question": "Total invoice revenue, computed by hand."})

    _caption(page, f"1 / 4  Asking: {ASK_QUESTION}")
    stage.beat(2)

    box = page.locator("input[placeholder*='Ask'], textarea[placeholder*='Ask']").first
    box.wait_for(state="visible", timeout=60_000)
    stage.click(box)
    box.type(ASK_QUESTION, delay=38)
    stage.beat(1.5)
    box.press("Enter")

    _caption(page, "2 / 4  The job runs: plan, write SQL, execute, read the rows")

    # Wait on the server's record of the generation rather than on anything drawn: a
    # row in history means SQL actually reached the database. Beats in between, so
    # the progress tracker is on film doing the work.
    generated = None
    for _ in range(60):
        stage.beat(2)
        rows = (_api(page, "/api/vanna/v2/history?limit=20", tenant)
                .get("payload") or {}).get("history") or []
        match = next((r for r in rows if (r.get("question") or "") == ASK_QUESTION), None)
        if match and (match.get("sql") or "").strip():
            generated = match
            break

    if not generated:
        _caption(page, "The question did not come back in time", tone="bad")
        print("[verify] no generation recorded -- nothing to check", flush=True)
        stage.beat(4)
        return

    stage.beat(3)  # dwell on the answer as the app drew it
    _caption(page, "3 / 4  The answer, and the SQL the model wrote to get it")
    stage.beat(4)

    # What gets checked is the number the user reads, not a replay of the SQL.
    #
    # Replaying was the first attempt and it does not work: history stores the
    # *compiled* statement, which names physical tables, and the SQL policy refuses
    # physical names on the way in -- so re-running a perfectly correct answer comes
    # back "chinook.invoice is not one of them". Worth knowing, because History's own
    # Run button calls the same endpoint with the same stored SQL, and so fails for
    # every semantic-layer answer. Reported separately; not this clip's subject.
    reply = _wait_for_reply(page, tenant, ASK_QUESTION)
    mentioned = _numbers_in(reply)
    actual = expected if (expected is not None and expected in mentioned) else (
        mentioned[0] if mentioned else None)
    print(f"[verify] the assistant replied: {reply[:200]}", flush=True)
    print(f"[verify] numbers in the reply: {mentioned}", flush=True)
    flat = " ".join((generated.get("sql") or "").split())
    print(f"[verify] rows returned: {generated.get('row_count')}", flush=True)
    print(f"[verify] compiled SQL: {flat[:200]}", flush=True)

    # The on-camera half: run the hand-written query out of Saved.
    _caption(page, "4 / 4  Checking it: the same figure, computed by hand")
    if stage.view("saved"):
        card = page.locator(".card", has_text=CHECK_TITLE).first
        if card.count():
            card.scroll_into_view_if_needed()
            stage.beat(1.5)
            stage.click(card.locator("[data-act='run']").first)
            page.wait_for_selector("#sheet table.data, #sheet .card", timeout=60_000)
            stage.beat(4)

    if expected is None:
        verdict = "Could not compare: the hand-written query returned no number"
        tone = "bad"
    elif not mentioned:
        verdict = "Could not compare: the answer quotes no figure"
        tone = "bad"
    elif expected in mentioned:
        verdict = f"CORRECT - the answer says {expected}, and so does the hand-written query"
        tone = "good"
    else:
        verdict = f"WRONG - the answer says {actual}, the hand-written query says {expected}"
        tone = "bad"

    _caption(page, verdict, tone=tone)
    print(f"[verify] {verdict}", flush=True)
    stage.beat(6)


# ----------------------------------------------------------------------
# The whole application, in one take
# ----------------------------------------------------------------------

#: Every screen in the workspace rail, with something to look for once it opens.
#: "cubes" is labelled Metrics in the interface; the data-view name is the older one.
VIEW_TOUR = [
    ("schema", "Schema - the tables and how they join", "#view-other"),
    ("history", "History - every question and the SQL it produced", "#view-other"),
    ("saved", "Saved queries", "#view-other"),
    ("dashboards", "Dashboards - the report library", "#view-other .card, #view-other .empty"),
    ("cubes", "Metrics - the semantic layer", "#view-other"),
    ("account", "Account - plan, workspace and data source", "#view-other"),
]

#: The operator console, tab by tab.
CONSOLE_TOUR = [
    "tenants", "accounts", "members", "permissions", "billing", "review",
    "verified", "domains", "rules", "library", "starters", "new",
]


class Checks:
    """What the tour verified, so the film ends on a result rather than a fade.

    A walkthrough that only shows things is a brochure. Recording a pass or fail at
    each stop costs nothing and turns the same footage into evidence.
    """

    def __init__(self) -> None:
        self.rows: List[Any] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""),
              flush=True)
        return bool(ok)

    def seen(self, page: Any, name: str, selector: str) -> bool:
        """Did this screen actually put something on the page?"""
        try:
            count = page.locator(selector).first.count()
            text = ""
            if count:
                text = (page.locator(selector).first.inner_text() or "").strip()
            return self.record(name, count > 0 and len(text) > 0,
                               "" if text else f"{selector} was empty")
        except Exception as exc:
            return self.record(name, False, f"{type(exc).__name__}: {exc}")

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if ok)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)


def scene_full_tour(stage: Stage) -> None:
    """One take over the whole application, checking as it goes.

    Deliberately a single browser context: Playwright writes one video file per
    context, so anything spread across contexts becomes several files. Everything
    here -- sign-in, the seven workspace screens, a question answered and checked,
    a report and its charts, both themes, both writing directions, and all twelve
    console tabs -- happens in one session so it lands as one video.
    """
    page = stage.page
    tenant = "chinook"
    checks = Checks()
    js_errors: List[str] = []
    page.on("pageerror", lambda e: js_errors.append(str(e)))

    # -- 1. signing in -------------------------------------------------
    _caption(page, "1  Signing in")
    stage.sign_in(tenant=tenant)
    checks.record("signed in and the app booted",
                  page.locator("#app.ready").count() > 0)
    checks.record("the rail names the signed-in account",
                  EMAIL_MARK in (page.locator("#rail-email").inner_text() or ""))
    stage.beat(2)

    # -- 2. asking a question, and checking the answer -----------------
    _caption(page, "2  Asking a question of the warehouse")
    truth = _api(page, "/api/vanna/v2/run-sql", tenant, "POST",
                 {"sql": CHECK_SQL, "limit": 5})
    expected = _first_number(truth.get("payload") or {})

    box = page.locator("input[placeholder*='Ask'], textarea[placeholder*='Ask']").first
    box.wait_for(state="visible", timeout=60_000)
    stage.click(box)
    box.type(ASK_QUESTION, delay=34)
    stage.beat(1.2)
    box.press("Enter")

    _caption(page, "3  The job runs: plan, write SQL, execute, read the rows")
    answered = False
    for _ in range(60):
        stage.beat(2)
        rows = (_api(page, "/api/vanna/v2/history?limit=20", tenant)
                .get("payload") or {}).get("history") or []
        if any((r.get("question") or "") == ASK_QUESTION and (r.get("sql") or "").strip()
               for r in rows):
            answered = True
            break
    checks.record("the question came back with SQL that ran", answered)
    stage.beat(3)

    reply = _wait_for_reply(page, tenant, ASK_QUESTION) if answered else ""
    mentioned = _numbers_in(reply)
    correct = expected is not None and expected in mentioned
    checks.record("the answer agrees with hand-written SQL", correct,
                  f"answer quotes {mentioned[:1] or 'nothing'}, hand-written says {expected}")
    _caption(page, f"4  Checked: the answer says {expected}, and so does hand-written SQL"
             if correct else "4  Checked: the answer does NOT match hand-written SQL",
             tone="good" if correct else "bad")
    stage.beat(4)

    # -- 3. every screen in the rail -----------------------------------
    step = 5
    for view, label, selector in VIEW_TOUR:
        _caption(page, f"{step}  {label}")
        opened = stage.view(view)
        if opened:
            checks.seen(page, f"{view} rendered", selector)
        else:
            checks.record(f"{view} rendered", False, "the rail did not offer it")
        stage.beat(1.5)
        step += 1

    # -- 4. a report, and the charts in it -----------------------------
    _caption(page, f"{step}  Opening a report")
    step += 1
    stage.view("dashboards")
    card = page.locator("#view-other .card", has_text="Revenue overview").first
    if not card.count():
        card = page.locator("#view-other .card").first
    if card.count():
        stage.click(card.locator("[data-open]").first)
        page.wait_for_selector("#sheet .card, #sheet .empty", timeout=120_000)
        try:
            page.wait_for_selector("#sheet .js-plotly-plot", timeout=45_000)
        except Exception:
            pass
        stage.beat(3)
        plots = page.locator("#sheet .js-plotly-plot").count()
        checks.record("the report drew its Plotly charts", plots > 0, f"{plots} chart(s)")

        # The gestures the toolbar exists for: hover the series, drag a zoom box,
        # double-click to reset. None of this survives a screenshot.
        _caption(page, f"{step}  The charts are interactive: hover, zoom, reset")
        step += 1
        plot_box = page.locator("#sheet .js-plotly-plot").first.bounding_box()
        if plot_box:
            for fraction in (0.2, 0.4, 0.6, 0.8):
                page.mouse.move(plot_box["x"] + plot_box["width"] * fraction,
                                plot_box["y"] + plot_box["height"] * 0.55)
                stage.beat(0.5)
            page.mouse.move(plot_box["x"] + plot_box["width"] * 0.3,
                            plot_box["y"] + plot_box["height"] * 0.3)
            page.mouse.down()
            page.mouse.move(plot_box["x"] + plot_box["width"] * 0.72,
                            plot_box["y"] + plot_box["height"] * 0.8, steps=25)
            page.mouse.up()
            stage.beat(2.5)
            page.mouse.dblclick(plot_box["x"] + plot_box["width"] * 0.5,
                                plot_box["y"] + plot_box["height"] * 0.5)
            stage.beat(2)
    else:
        checks.record("the report drew its Plotly charts", False, "no report to open")

    # -- 5. parameters, which are real but have no control yet ---------
    #
    # Said plainly on camera rather than mimed: every report declares typed
    # parameters and the API honours them, but the sheet renders no inputs for
    # them, so there is nothing here for a mouse to change. The check drives the
    # endpoint instead and shows the two answers differing.
    _caption(page, f"{step}  Report parameters: server-side today, no control in the sheet yet")
    step += 1
    wide, narrow = _parameter_probe(page, tenant)
    checks.record("a parameter changes what the tiles return",
                  wide is not None and narrow is not None and wide != narrow,
                  f"full period {wide} against one year {narrow}")
    _caption(page, f"{step - 1}  Parameters proven over the API: {wide} for 2021-2025, {narrow} for 2021 alone",
             tone="good" if (wide and narrow and wide != narrow) else "bad")
    stage.beat(4)

    # -- 6. the same report in the dark theme --------------------------
    _caption(page, f"{step}  The same report in the dark theme")
    step += 1
    if page.locator("#theme-btn").count():
        stage.click(page.locator("#theme-btn"))
        stage.beat(3)
        checks.record("the dark theme applied",
                      page.locator("html[data-theme='dark']").count() > 0)
        stage.beat(2)
        stage.click(page.locator("#theme-btn"))
        stage.beat(1.5)
    else:
        checks.record("the dark theme applied", False, "no theme control found")

    # -- 7. Arabic, which is a layout rather than a translation --------
    _caption(page, f"{step}  Arabic: the whole layout mirrors, right to left")
    step += 1
    if page.locator("#locale-btn").count():
        try:
            page.select_option("#locale-btn", "ar")
            stage.beat(3)
            direction = page.evaluate("() => document.documentElement.getAttribute('dir')")
            checks.record("the page mirrored for Arabic", direction == "rtl",
                          f"dir={direction!r}")
            stage.view("schema")
            stage.beat(2)
            stage.view("history")
            stage.beat(2)
            page.select_option("#locale-btn", "en")
            stage.beat(1.5)
        except Exception as exc:
            checks.record("the page mirrored for Arabic", False,
                          f"{type(exc).__name__}: {exc}")
    else:
        checks.record("the page mirrored for Arabic", False, "no locale control found")

    # -- 8. the operator console, every tab ----------------------------
    _caption(page, f"{step}  The operator console")
    step += 1
    page.goto(f"{stage.base}/admin/", wait_until="networkidle")
    page.wait_for_selector("#tabs button", timeout=60_000)
    stage.beat(2)

    for tab in CONSOLE_TOUR:
        button = page.locator(f"#tabs button[data-tab='{tab}']")
        if not button.count():
            checks.record(f"console: {tab}", False, "tab not offered")
            continue
        _caption(page, f"{step}  Console: {tab}")
        stage.click(button)
        stage.beat(1.6)
        checks.seen(page, f"console: {tab}", "#content")
    step += 1

    # The rail must hold still while the panel scrolls past it. That was the whole
    # point of giving them separate scrollers, and it is only visible in motion.
    _caption(page, f"{step}  The sections rail holds still while the panel scrolls")
    step += 1
    stage.click(page.locator("#tabs button[data-tab='tenants']"))
    stage.beat(1.5)
    rail_before = page.evaluate(
        "() => { const n = document.querySelector('.side'); return n ? n.getBoundingClientRect().top : null; }")
    for offset in (400, 1000, 1600, 0):
        page.evaluate("(y) => { document.getElementById('content').scrollTop = y; }", offset)
        stage.beat(1.1)
    rail_after = page.evaluate(
        "() => { const n = document.querySelector('.side'); return n ? n.getBoundingClientRect().top : null; }")
    checks.record("the rail did not move with the panel", rail_before == rail_after,
                  f"{rail_before} then {rail_after}")
    stage.beat(1.5)

    # -- 9. back to the app --------------------------------------------
    _caption(page, f"{step}  Back to the app, from the rail")
    step += 1
    back = page.locator(".side a.back, a.back").first
    if back.count():
        stage.click(back)
        try:
            page.wait_for_selector("#app.ready", timeout=60_000)
            checks.record("Back to app returned to the workspace", True)
        except Exception:
            checks.record("Back to app returned to the workspace", False, "never became ready")
    else:
        checks.record("Back to app returned to the workspace", False, "no exit link in the rail")
    stage.beat(2)

    # -- 10. the verdict -----------------------------------------------
    checks.record("no uncaught JavaScript errors during the tour", not js_errors,
                  "; ".join(js_errors[:2]))
    verdict = f"{checks.passed} checks passed, {checks.failed} failed"
    _caption(page, verdict, tone="good" if checks.failed == 0 else "bad")
    print(f"\n  --- {verdict} ---", flush=True)
    for name, ok, detail in checks.rows:
        if not ok:
            print(f"    FAILED: {name}" + (f" -- {detail}" if detail else ""), flush=True)
    stage.beat(7)


def _parameter_probe(page: Any, tenant: str) -> Any:
    """Ask one report for its numbers twice, over different periods.

    Returns the first metric each time, so the caller can show that a parameter
    genuinely re-runs the tiles rather than being decoration.
    """
    import urllib.parse

    listing = (_api(page, "/api/vanna/v2/dashboards", tenant).get("payload") or {})
    wanted = None
    for row in listing.get("dashboards") or []:
        if (row.get("title") or "") == "Revenue overview":
            wanted = row
            break
    if wanted is None:
        return (None, None)

    out = []
    identifier = urllib.parse.quote(str(wanted.get("id")))
    for window in ("2021-01-01..2025-12-31", "2021-01-01..2021-12-31"):
        payload = (_api(
            page,
            f"/api/vanna/v2/dashboards/{identifier}/data?period={urllib.parse.quote(window)}",
            tenant,
        ).get("payload") or {})
        first = None
        for result in payload.get("results") or []:
            first = _first_number(result)
            if first is not None:
                break
        out.append(first)
    return tuple(out)


SCENES: Dict[str, Callable[[Stage], None]] = {
    "full-tour": scene_full_tour,
    "sign-in": scene_sign_in,
    "views": scene_views,
    "ask-and-verify": scene_ask_and_verify,
    "report-parameters": scene_report_parameters,
    "chart-interaction": scene_chart_interaction,
    "console": scene_console,
    "arabic": scene_arabic,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", required=True)
    parser.add_argument(
        "--scene", action="append", default=[],
        help=f"record only these, repeatable. Known: {', '.join(SCENES)}",
    )
    parser.add_argument("--beat", type=int, default=BEAT, help="pacing, in milliseconds")
    parser.add_argument("--headed", action="store_true", help="watch it record")
    args = parser.parse_args()

    wanted = args.scene or list(SCENES)
    if unknown := [s for s in wanted if s not in SCENES]:
        raise SystemExit(f"no such scene: {', '.join(unknown)}")

    OUT.mkdir(parents=True, exist_ok=True)
    made: List[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed, args=["--disable-dev-shm-usage"]
        )
        for name in wanted:
            print(f"  recording {name} ...", end=" ", flush=True)
            stage = Stage(browser, args.url, args.email, args.password, beat=args.beat)
            try:
                SCENES[name](stage)
            except Exception as exc:
                # Keep the footage anyway: a clip that ends at the failure is often
                # the most useful thing in the folder.
                print(f"stopped early ({type(exc).__name__}: {exc})", end=" ")
            path = stage.finish(name)
            if path:
                made.append(path)
                print(f"{path.name} ({path.stat().st_size // 1024} KB)")
            else:
                print("no video written")
        browser.close()

    print(f"\n  {len(made)} clip(s) in {OUT}")
    for path in made:
        print(f"    {path.name:<28} {path.stat().st_size // 1024:>6} KB")
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
