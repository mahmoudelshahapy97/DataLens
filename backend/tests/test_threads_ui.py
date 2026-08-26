"""The conversation rail, driven in a browser.

The rail already had rename and delete before this pass; what it did not have was
a way to reach either without a mouse, a label a screen reader could read, a way
to find a thread in a long list, or any way at all to see past the newest page.
Those are the things asserted here, because none of them are visible to a unit
test: they are properties of the rendered page.

Same arrangement as ``test_ask_ui.py`` -- the real ``frontend/public`` is served
as the container serves it and only the API is stubbed, so what is under test is
the file that ships.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from test_ask_ui import (  # noqa: E402 - harness reuse; tests/ is not a package
    ME,
    ONE_DATABASE,
    TWO_DATABASES,
    browser,  # noqa: F401 - pytest fixture
    server,  # noqa: F401 - pytest fixture
)

PAGE = 30  # must match THREAD_PAGE in app.js


def a_thread(index, *, source="postgresql://wh/chinook"):
    return {
        "id": f"c{index}",
        "title": f"Thread {index}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "message_count": 2,
        "data_source_id": source,
    }


class Api:
    """Stateful enough to page, rename and delete for real."""

    def __init__(self, threads, *, sources=ONE_DATABASE, persisted=True):
        self.threads = list(threads)
        self.sources = sources
        self.persisted = persisted
        self.requests = []  # (method, path-and-query)

    def install(self, page):
        def handle(route, request):
            url = request.url
            self.requests.append((request.method, url.split("/api/", 1)[-1]))

            if "/v2/me" in url:
                return self._json(route, ME)
            if "/v2/datasources" in url:
                return self._json(route, {"data_sources": self.sources})
            if "/v2/tenants" in url:
                return self._json(
                    route,
                    {"tenants": [{"id": "acme", "name": "Acme"}], "control_plane": True},
                )
            if "/v2/starters" in url:
                return self._json(route, {"starters": []})
            if "/v2/cubes" in url:
                return self._json(route, {"cubes": []})
            if "/v2/conversations" in url:
                return self._conversations(route, request)
            return self._json(route, {})

        page.route("**/api/**", handle)

    def _conversations(self, route, request):
        if not self.persisted:
            return self._json(route, {"conversations": [], "persisted": False})

        path, _, query = request.url.split("/api/", 1)[-1].partition("?")
        tail = path.rsplit("/v2/conversations", 1)[-1].lstrip("/")

        if tail:
            if request.method == "DELETE":
                self.threads = [item for item in self.threads if item["id"] != tail]
                return self._json(route, {"deleted": True})
            if request.method == "PATCH":
                title = json.loads(request.post_data or "{}").get("title", "")
                for item in self.threads:
                    if item["id"] == tail:
                        item["title"] = title
                return self._json(route, {"renamed": True})
            return self._json(route, {"id": tail, "messages": []})

        args = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        start = int(args.get("offset", 0))
        limit = int(args.get("limit", 50))
        return self._json(
            route,
            {"conversations": self.threads[start : start + limit], "persisted": True},
        )

    @staticmethod
    def _json(route, payload):
        return route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        )


def open_app(server, browser, api):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    api.install(page)
    page.add_init_script(
        "localStorage.setItem('vanna.identity',"
        " JSON.stringify({tenant:'acme', email:'ada@acme.test'}));"
    )
    page.goto(f"{server}/index.html")
    page.wait_for_selector("#app.ready", timeout=15_000)
    page.wait_for_selector("#thread-list .thread, #thread-list p", timeout=5_000)
    return page, errors


class TestTheRailIsReachableWithoutAMouse:
    def test_each_row_is_a_labelled_control(self, server, browser):
        """Rows used to be divs with an onclick inside a ``role=list``.

        Nothing announced them, nothing focused them, and the icon buttons were
        titled in hard-coded English.
        """
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        row = page.locator("#thread-list .thread").first
        assert row.get_attribute("role") == "listitem"
        assert row.locator("[data-open]").count() == 1
        assert row.locator("[data-rename]").get_attribute("aria-label") == (
            "Rename Thread 1"
        )
        assert row.locator("[data-del]").get_attribute("aria-label") == "Delete Thread 1"
        assert errors == []
        page.close()

    def test_arrows_move_between_rows(self, server, browser):
        """``roveFocus`` gives the rail one tab stop, as the view tablist has."""
        page, errors = open_app(
            server, browser, Api([a_thread(1), a_thread(2), a_thread(3)])
        )

        page.locator('#thread-list [data-open="c1"]').focus()
        page.keyboard.press("ArrowDown")
        assert page.evaluate("document.activeElement.dataset.open") == "c2"
        page.keyboard.press("ArrowDown")
        assert page.evaluate("document.activeElement.dataset.open") == "c3"
        assert errors == []
        page.close()

    def test_the_row_tools_are_visible_when_focused(self, server, browser):
        """They are opacity:0 until hover, which hides them from keyboard users."""
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        page.locator('#thread-list [data-del="c1"]').focus()
        opacity = page.evaluate(
            "getComputedStyle(document.querySelector('[data-del=' + JSON.stringify('c1')"
            " + ']')).opacity"
        )
        assert opacity == "1"
        assert errors == []
        page.close()


class TestFindingAThread:
    def test_search_narrows_the_loaded_list(self, server, browser):
        page, errors = open_app(
            server, browser, Api([a_thread(1), a_thread(2), a_thread(3)])
        )

        page.fill("#thread-search", "Thread 2")
        page.wait_for_function(
            "document.querySelectorAll('#thread-list .thread').length === 1"
        )
        assert (
            page.locator("#thread-list .thread").first.get_attribute("data-id") == "c2"
        )
        assert errors == []
        page.close()

    def test_no_match_says_so_rather_than_looking_empty(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        page.fill("#thread-search", "nothing like this")
        page.wait_for_function(
            "document.getElementById('thread-list').textContent"
            ".includes('No conversations match')"
        )
        assert errors == []
        page.close()


class TestPaging:
    def test_a_full_page_offers_more(self, server, browser):
        """``list_conversations(limit, offset)`` existed in the store with no
        route and no caller; the rail showed the newest page and stopped."""
        api = Api([a_thread(i) for i in range(PAGE + 5)])
        page, errors = open_app(server, browser, api)

        assert page.locator("#thread-list .thread").count() == PAGE
        page.click("#thread-more")
        page.wait_for_function(
            f"document.querySelectorAll('#thread-list .thread').length === {PAGE + 5}"
        )
        assert page.locator("#thread-more").is_hidden()
        assert any("offset=30" in path for _, path in api.requests)
        assert errors == []
        page.close()

    def test_a_short_page_does_not(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))
        assert page.locator("#thread-more").is_hidden()
        assert errors == []
        page.close()


class TestTheDatabaseBadge:
    def test_shown_only_when_there_is_more_than_one_database(self, server, browser):
        """A thread is pinned to a database; with two registered, which one a
        thread belongs to should be visible without opening it."""
        page, errors = open_app(server, browser, Api([a_thread(1)], sources=ONE_DATABASE))
        assert page.locator("#thread-list .thread .chip").count() == 0
        assert errors == []
        page.close()

        page, errors = open_app(server, browser, Api([a_thread(1)], sources=TWO_DATABASES))
        # The chip is text-transform: uppercase, so compare on the case-folded text.
        chip = page.locator("#thread-list .thread .chip").first.inner_text()
        assert chip.lower() == "chinook"
        assert errors == []
        page.close()


class TestDestructiveActionsUseTheAppsOwnDialog:
    def test_delete_names_the_thread_and_can_be_cancelled(self, server, browser):
        """``window.confirm`` cannot be translated, styled or mirrored for RTL --
        and it cannot name what is about to be destroyed."""
        api = Api([a_thread(1), a_thread(2)])
        page, errors = open_app(server, browser, api)

        page.click('#thread-list [data-del="c1"]')
        page.wait_for_selector("#overlay.on")
        assert "Delete Thread 1" in page.locator("#sheet").inner_text()

        page.click("#dlg-no")
        page.wait_for_selector("#overlay.on", state="hidden")
        assert len(api.threads) == 2

        page.click('#thread-list [data-del="c1"]')
        page.wait_for_selector("#overlay.on")
        page.click("#dlg-yes")
        page.wait_for_function(
            "document.querySelectorAll('#thread-list .thread').length === 1"
        )
        assert [item["id"] for item in api.threads] == ["c2"]
        assert errors == []
        page.close()

    def test_escape_cancels_rather_than_confirming(self, server, browser):
        api = Api([a_thread(1)])
        page, errors = open_app(server, browser, api)

        page.click('#thread-list [data-del="c1"]')
        page.wait_for_selector("#overlay.on")
        page.keyboard.press("Escape")
        page.wait_for_selector("#overlay.on", state="hidden")
        assert len(api.threads) == 1
        assert errors == []
        page.close()

    def test_rename_prefills_and_saves(self, server, browser):
        api = Api([a_thread(1)])
        page, errors = open_app(server, browser, api)

        page.click('#thread-list [data-rename="c1"]')
        page.wait_for_selector("#overlay.on")
        assert page.locator("#dlg-input").input_value() == "Thread 1"

        page.fill("#dlg-input", "Quarterly revenue")
        page.click("#dlg-ok")
        page.wait_for_function(
            "document.getElementById('thread-list').textContent"
            ".includes('Quarterly revenue')"
        )
        assert api.threads[0]["title"] == "Quarterly revenue"
        assert errors == []
        page.close()


class TestAnEmptyRailExplainsItself:
    def test_without_a_control_plane_it_says_why(self, server, browser):
        """It used to hide the whole rail, taking New chat with it -- which reads
        as a broken page rather than a deployment without a database."""
        page, errors = open_app(server, browser, Api([], persisted=False))

        assert "not saved in this deployment" in page.locator("#thread-list").inner_text()
        assert page.locator("#new-chat").is_visible()
        assert page.locator("#thread-search").is_hidden()
        assert errors == []
        page.close()

    def test_with_a_control_plane_and_no_threads_it_says_that_instead(
        self, server, browser
    ):
        page, errors = open_app(server, browser, Api([]))

        assert "No conversations yet" in page.locator("#thread-list").inner_text()
        assert errors == []
        page.close()


class TestTheRailIsActuallyLegible:
    """Layout, not markup.

    Every assertion above passed while the rail rendered *blank*: the row became
    three buttons, `nav.side button { width: 100% }` applied to all three, and the
    two icon buttons -- which shrink from 100% -- squeezed the name, whose basis is
    0, to nothing. Correct text, correct colour, zero width. So these check the
    pixels a person actually sees.
    """

    def test_the_conversation_name_has_room(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        box = page.locator('#thread-list [data-open="c1"]').bounding_box()
        row = page.locator("#thread-list .thread").first.bounding_box()
        assert box["width"] > row["width"] * 0.6, (
            f"the name got {box['width']}px of {row['width']}px"
        )
        assert errors == []
        page.close()

    def test_the_row_tools_stay_small(self, server, browser):
        """They are 26px squares; at any more than that they are eating the name."""
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        for selector in ('[data-rename="c1"]', '[data-del="c1"]'):
            box = page.locator(f"#thread-list {selector}").bounding_box()
            assert box["width"] <= 32, f"{selector} is {box['width']}px wide"
        assert errors == []
        page.close()

    def test_the_name_is_drawn_in_the_ink_of_its_surroundings(self, server, browser):
        """Not the accent: the rail's accent belongs to the section tabs, and a
        conversation is selected, not linked."""
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        colours = page.evaluate(
            """() => {
                 const label = document.querySelector('#thread-list .label');
                 const nav = document.querySelector('nav.side');
                 return {
                   name: getComputedStyle(label).color,
                   body: getComputedStyle(document.body).color,
                   rail: getComputedStyle(nav).backgroundColor,
                   page: getComputedStyle(document.body).backgroundColor,
                 };
               }"""
        )
        assert colours["name"] == colours["body"]
        # And the rail is its own surface, not the same sheet as the content.
        assert colours["rail"] != colours["page"]
        assert errors == []
        page.close()

    def test_the_heading_appears_only_over_a_list(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))
        assert page.locator("#thread-heading").is_visible()
        page.close()
        assert errors == []

        page, errors = open_app(server, browser, Api([]))
        assert page.locator("#thread-heading").is_hidden()
        assert errors == []
        page.close()


class TestCollapsingTheRail:
    """Collapsed is a narrower rail, not an absent one.

    Hiding the navigation outright means every section change costs a round trip
    through the toggle. The icons stay and carry their names in `aria-label`; the
    conversation list is the part that goes, because a 60px column cannot show a
    title and a stack of unreadable stubs is worse than nothing.
    """

    def test_it_narrows_and_restores(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        wide = page.locator("nav.side").bounding_box()["width"]
        page.click("#side-toggle")
        narrow = page.locator("nav.side").bounding_box()["width"]
        assert narrow < wide / 2, f"{wide}px -> {narrow}px is not a collapse"

        page.click("#side-toggle")
        assert page.locator("nav.side").bounding_box()["width"] == wide
        assert errors == []
        page.close()

    def test_the_sections_stay_reachable(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        page.click("#side-toggle")
        assert page.locator('nav.side button[data-view="history"]').is_visible()
        page.click('nav.side button[data-view="history"]')
        page.wait_for_selector("#h-body", timeout=5_000)
        assert errors == []
        page.close()

    def test_every_icon_still_says_what_it_is(self, server, browser):
        """An icon with no accessible name announces itself as "button"."""
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        page.click("#side-toggle")
        names = page.eval_on_selector_all(
            "nav.side button[data-view], nav.side .new-chat",
            "els => els.map((el) => el.getAttribute('aria-label'))",
        )
        assert names and all(name and name.strip() for name in names), names
        assert errors == []
        page.close()

    def test_the_conversation_list_goes(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        page.click("#side-toggle")
        assert page.locator("#threads").is_hidden()
        assert page.locator("#thread-search").is_hidden()
        assert errors == []
        page.close()

    def test_the_button_says_which_way_it_goes(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        toggle = page.locator("#side-toggle")
        assert toggle.get_attribute("aria-expanded") == "true"
        assert toggle.get_attribute("aria-controls") == "side"
        assert "Hide" in toggle.get_attribute("aria-label")

        page.click("#side-toggle")
        assert toggle.get_attribute("aria-expanded") == "false"
        assert "Show" in toggle.get_attribute("aria-label")
        assert errors == []
        page.close()

    def test_the_choice_survives_a_reload(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        page.click("#side-toggle")
        page.reload()
        page.wait_for_selector("#app.ready", timeout=15_000)
        assert page.locator("#threads").is_hidden()
        assert page.locator("#side-toggle").get_attribute("aria-expanded") == "false"
        assert errors == []
        page.close()


class TestTheRailReadsTopToBottom:
    def test_actions_come_before_history(self, server, browser):
        """New chat, then the sections, then what you have already asked. The
        reverse -- history first -- puts the least actionable thing at eye level."""
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        order = page.eval_on_selector_all(
            "nav.side > *", "els => els.map((el) => el.id || el.className)"
        )
        assert order.index("new-chat") < order.index("views")
        assert order.index("views") < order.index("threads")
        assert errors == []
        page.close()

    def test_every_row_carries_an_icon(self, server, browser):
        page, errors = open_app(server, browser, Api([a_thread(1)]))

        missing = page.eval_on_selector_all(
            "nav.side button[data-view], nav.side .new-chat",
            "els => els.filter((el) => !el.querySelector('svg.ico'))"
            "        .map((el) => el.dataset.view || el.id)",
        )
        assert missing == []
        assert errors == []
        page.close()
