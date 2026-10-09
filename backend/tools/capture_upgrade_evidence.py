#!/usr/bin/env python3
"""Screenshot the agent upgrade working, into ``artifacts/agent-upgrade/``.

``artifacts/`` is where this project keeps evidence that something worked --
screenshots from the UI sweeps, the demo recording, the audit export. A claim
in a report is worth less than the picture of the thing happening, which is why
these are captured from the real browser against the running stack rather than
drawn from the API responses.

Four things are worth a picture, and they are the four that were either broken
or newly built:

1. **A multi-hop join answered correctly.** ``suggest_joins`` picking
   ``invoice_line -> track -> album -> artist``, a path no single table's schema
   shows, and the numbers that come out of it.
2. **A clarification card.** The agent declining to guess at an ambiguous
   question, with the options it offered.
3. **A second engine.** The same chat answering against MySQL, to show the
   multi-database wiring is real and not Postgres-only.
4. **The console's database list.** Four data sources across three engines,
   including the Oracle one that could not be registered at all before today.

Run it with the stack up::

    python tools/capture_upgrade_evidence.py
    python tools/capture_upgrade_evidence.py --url http://localhost:3000

Each question costs a real model call, so this is not a test-suite fixture --
it is run deliberately, when the evidence needs refreshing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover - the tool cannot run without it
    print(
        "playwright is required: pip install playwright && playwright install chromium"
    )
    raise SystemExit(1)


#: Wide enough that a chart and a table are not stacked into a phone layout,
#: and tall enough that a four-option clarification card fits without scrolling.
VIEWPORT = {"width": 1500, "height": 1000}

#: Generous: a multi-hop question runs four tools and several model calls, and
#: a cold workspace rebuilds its runtime first.
ANSWER_TIMEOUT_S = 240

#: Text the UI shows only while a turn is in flight. Absence of all of
#: them is what "finished" means here.
_WORKING_MARKERS = ("Processing your request", "Analyzing query")


def _sign_in(page: Page, base_url: str, email: str, password: str, tenant: str) -> None:
    """Sign in, picking the workspace when the account belongs to several.

    The picker only appears for multi-workspace accounts, which is why it is
    conditional rather than a fixed step -- and it is a Radix combobox, not a
    native <select>, so it is clicked rather than selected.
    """
    page.goto(f"{base_url}/", wait_until="networkidle")
    page.fill("#login-email", email)
    page.fill("#login-password", password)
    page.click("button[type=submit]")
    page.wait_for_timeout(4000)

    if page.locator("#login-workspace").count():
        page.click("#login-workspace")
        page.wait_for_timeout(800)
        page.locator("[role=option]").filter(has_text=tenant).first.click()
        page.wait_for_timeout(600)
        page.click("button[type=submit]")

    page.wait_for_url("**/ask", timeout=30_000)
    page.wait_for_timeout(3000)


def _ask(page: Page, question: str) -> None:
    box = page.locator("input[placeholder*='Ask'], textarea[placeholder*='Ask']").first
    box.wait_for(state="visible", timeout=60_000)
    box.click()
    box.fill(question)
    box.press("Enter")


def _wait_for_settled(page: Page, timeout_s: int = ANSWER_TIMEOUT_S) -> bool:
    """Wait until the agent stops working on the turn.

    Not the composer's enabled state, which was the obvious choice and the
    wrong one: the input stays enabled while the answer streams, so screenshots
    taken on that signal caught "Processing your request..." every time.

    The status strip is the honest signal. Its absence is checked rather than
    an answer's presence, because a turn may end with a card and no prose --
    which is exactly what the clarification case does.
    """
    deadline = time.time() + timeout_s
    # A beat first: the strip takes a moment to appear, and polling before it
    # does would read "settled" on a turn that has not started.
    page.wait_for_timeout(3000)

    while time.time() < deadline:
        # Locators, not inner_text("body"). The chat is a web component and its
        # status strip lives in a shadow root, which page-level text extraction
        # cannot see -- so every poll read "finished" and every screenshot
        # caught the spinner. Playwright's text engine pierces shadow DOM.
        try:
            busy = any(
                page.locator(f"text={marker}").count() for marker in _WORKING_MARKERS
            )
        except Exception:
            busy = True
        if not busy:
            page.wait_for_timeout(2500)  # let the last component paint
            return True
        page.wait_for_timeout(2000)
    return False


def _shoot(page: Page, out: Path, name: str) -> Path:
    path = out / name
    page.screenshot(path=str(path), full_page=False)
    print(f"  saved {path.name}")
    return path


def _switch_database(page: Page, label_fragment: str) -> bool:
    """Pick another database from whatever control the workspace offers.

    Returns False rather than raising when the picker is not found: a missing
    control should cost one screenshot, not the whole run.
    """
    for selector in ("#data-source", "select[name='data_source']", "#sheet select"):
        control = page.locator(selector).first
        try:
            if control.is_visible():
                for option in control.locator("option").all():
                    text = option.text_content() or ""
                    if label_fragment.lower() in text.lower():
                        control.select_option(label=text)
                        page.wait_for_timeout(2000)
                        return True
        except Exception:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="demo@example.com")
    parser.add_argument("--password", default="vanna-admin-2026!")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument(
        "--out",
        default=str(
            Path(__file__).resolve().parents[2] / "artifacts" / "agent-upgrade"
        ),
    )
    parser.add_argument("--headed", action="store_true", help="watch it work")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport=VIEWPORT,
            extra_http_headers={"X-Tenant-Id": args.tenant},
        )
        page = context.new_page()

        try:
            print("signing in...")
            _sign_in(page, args.url, args.email, args.password, args.tenant)
            _shoot(page, out, "01-workspace.png")

            shots = [
                (
                    "02-multi-hop-join.png",
                    "Which 3 artists earned the most revenue? Give me names and amounts.",
                    "suggest_joins finds invoice_line -> track -> album -> artist",
                ),
                (
                    "03-clarification.png",
                    "Show me our best customers",
                    "request_clarification: the agent asks instead of guessing",
                ),
                (
                    "04-trend.png",
                    "Is our monthly revenue growing or shrinking over time?",
                    "analyze_timeseries computes the trend rather than eyeballing rows",
                ),
            ]

            for name, question, caption in shots:
                print(f"asking: {question}")
                page.goto(f"{args.url}/ask", wait_until="networkidle")
                page.wait_for_timeout(2500)
                _ask(page, question)
                settled = _wait_for_settled(page)
                _shoot(page, out, name)
                captured.append(
                    {
                        "file": name,
                        "question": question,
                        "shows": caption,
                        "settled": settled,
                    }
                )

            # A second engine, to show multi-database is real.
            print("switching to MySQL...")
            page.goto(f"{args.url}/ask", wait_until="networkidle")
            page.wait_for_timeout(2500)
            if _switch_database(page, "sakila"):
                _ask(page, "Which 3 actors appear in the most films?")
                settled = _wait_for_settled(page)
                _shoot(page, out, "05-mysql-sakila.png")
                captured.append(
                    {
                        "file": "05-mysql-sakila.png",
                        "question": "Which 3 actors appear in the most films?",
                        "shows": "the same chat answering against MySQL, not Postgres",
                        "settled": settled,
                    }
                )
            else:
                print("  (no database picker found; skipping the MySQL shot)")

            # The console's database list: four sources, three engines.
            print("capturing the console...")
            # /console/workspaces, from app/routes.tsx. Not /console, which is
            # not a route at all and renders "There is nothing at this address".
            page.goto(f"{args.url}/console/workspaces", wait_until="networkidle")
            page.wait_for_timeout(4000)
            _shoot(page, out, "06-databases.png")
            captured.append(
                {
                    "file": "06-databases.png",
                    "question": None,
                    "shows": "four data sources across Postgres, MySQL and Oracle",
                }
            )

        finally:
            (out / "captures.json").write_text(
                json.dumps(captured, indent=2), encoding="utf-8"
            )
            context.close()
            browser.close()

    print(f"\n{len(captured)} screenshot(s) in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
