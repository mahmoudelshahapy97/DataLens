"""Dashboard tiles on screen: every one of them is a Plotly figure.

A tile used to be rendered three ways -- a chart through Plotly, a metric as a
large `<div>`, a table as an HTML `<table>` -- and the exported file drew its own
SVG on top of that. Four renderers for one concept.

Now there is one: `assets/shared/tile-figure.js` turns a tile and its result into
`{ traces, layout, config }`, and both the page and the export call it. These
tests are about what the *page* does with it; `test_dashboard_export_ui.py` checks
that Plotly can actually draw the result, in a real browser, from a file:// URL.

The `<plotly-chart>` element is stubbed here (see `test_ask_ui.COMPONENT_STUB`),
so what is asserted is the figure the page handed it -- which is the part this
code is responsible for.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from test_ask_ui import (  # noqa: E402 - harness reuse; tests/ is not a package
    ME,
    ONE_DATABASE,
    Api,
    browser,  # noqa: F401 - pytest fixture
    server,  # noqa: F401 - pytest fixture
)

MONTHS = [["Jan", 120.0], ["Feb", 90.5], ["Mar", 240.25]]

TILES = [
    {"id": "bar", "kind": "chart", "title": "Revenue by month",
     "chart": {"type": "bar"}, "grid": {"x": 0, "y": 0, "width": 6, "height": 5}},
    {"id": "pie", "kind": "chart", "title": "Share",
     "chart": {"type": "pie"}, "grid": {"x": 6, "y": 0, "width": 6, "height": 5}},
    {"id": "metric", "kind": "metric", "title": "Total revenue",
     "grid": {"x": 0, "y": 1, "width": 3, "height": 3}},
    {"id": "table", "kind": "table", "title": "Rows",
     "grid": {"x": 3, "y": 1, "width": 9, "height": 5}},
    {"id": "text", "kind": "text", "title": "Notes", "text": "## Context"},
]

RESULTS = [
    {"tile_id": "bar", "columns": ["month", "revenue"], "rows": MONTHS, "row_count": 3},
    {"tile_id": "pie", "columns": ["month", "revenue"], "rows": MONTHS, "row_count": 3},
    {"tile_id": "metric", "columns": ["revenue"], "rows": [[450.75]], "row_count": 1},
    {"tile_id": "table", "columns": ["month", "revenue"], "rows": MONTHS,
     "row_count": 41, "truncated": False},
]


class DashboardApi(Api):
    """The stub, plus the two dashboard endpoints the sheet calls."""

    def __init__(self, sources, *, results=None, tiles=None):
        super().__init__(sources)
        self.results = results if results is not None else RESULTS
        self.tiles = tiles if tiles is not None else TILES

    def install(self, page):
        def handle(route, request):
            url = request.url
            self.calls.append(("/" + url.split("://", 1)[-1].split("/", 1)[-1]
                               .split("?")[0], dict(request.headers)))

            if "/v2/dashboards/" in url and url.endswith("/data"):
                return self._json(route, {"results": self.results})
            if url.rstrip("/").endswith("/v2/dashboards"):
                return self._json(route, {"dashboards": [{
                    "id": "d1", "title": "Q3 review",
                    "document": {"tiles": self.tiles},
                }]})
            if "/v2/me" in url:
                return self._json(route, ME)
            if "/v2/datasources" in url:
                return self._json(route, {"data_sources": self.sources})
            if "/v2/tenants" in url:
                return self._json(route, {"tenants": [{"id": "acme", "name": "Acme"}],
                                          "control_plane": True})
            if "/v2/starters" in url:
                return self._json(route, {"starters": []})
            if "/v2/conversations" in url:
                return self._json(route, {"conversations": []})
            if "/v2/cubes" in url:
                return self._json(route, {"cubes": []})
            return self._json(route, {})

        page.route("**/api/**", handle)


@pytest.fixture
def opened(server, browser):
    """The dashboard sheet, open, with its tiles mounted."""
    api = DashboardApi(ONE_DATABASE)
    page = browser.new_page(viewport={"width": 1400, "height": 1000})
    errors: list = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    api.install(page)

    page.goto(server, wait_until="networkidle")
    page.wait_for_selector("#app.ready", timeout=20_000)
    page.click('button[data-view="dashboards"]')
    page.wait_for_selector("#view-other [data-open]", timeout=20_000)
    page.locator("#view-other [data-open]").first.click()
    page.wait_for_selector(".tile-grid", timeout=20_000)
    page.wait_for_timeout(400)

    yield page, errors
    page.close()


def figures(page) -> dict:
    """`tile id -> the trace types the page handed <plotly-chart>`."""
    return page.evaluate(
        """() => Object.fromEntries(
            [...document.querySelectorAll('.tile-chart')].map((mount) => {
              const chart = mount.querySelector('plotly-chart');
              return [
                mount.dataset.tile,
                chart ? (chart.data || []).map((t) => t.type).join(',') : null,
              ];
            })
        )"""
    )


class TestEveryTileIsAFigure:
    def test_chart_metric_and_table_all_go_through_plotly(self, opened):
        page, _ = opened
        assert figures(page) == {
            "bar": "bar",
            "pie": "pie",
            "metric": "indicator",
            "table": "table",
        }

    def test_a_text_tile_has_no_figure(self, opened):
        """Prose is markup. A heading rendered as a chart of nothing would be the
        one tile kind that has no data to draw."""
        page, _ = opened
        assert page.locator('.tile-chart[data-tile="text"]').count() == 0
        assert "Context" in page.locator(".tile-grid").inner_text()

    def test_there_are_no_html_tables_left_in_a_tile(self, opened):
        """The change the user asked for: a table tile is a `table` trace now, not
        a `<table>`."""
        page, _ = opened
        assert page.locator(".tile-grid table").count() == 0

    def test_nothing_errored(self, opened):
        page, errors = opened
        assert errors == []


class TestWhatTheFigureCarries:
    def test_the_metric_keeps_its_decimals(self, opened):
        """Plotly's indicator rounds to whole numbers by default, so an average of
        450.75 rendered as 451 -- a tile quietly showing a different number than
        the query returned."""
        page, _ = opened
        trace = page.evaluate(
            """() => document.querySelector('.tile-chart[data-tile="metric"] plotly-chart')
                     .data[0]"""
        )
        assert trace["value"] == 450.75
        assert trace["number"]["valueformat"] == ",.10~r"

    def test_the_table_is_column_major(self, opened):
        """Plotly's table takes one array per column. Handing it rows transposes
        the data into nonsense that still renders."""
        page, _ = opened
        cells = page.evaluate(
            """() => document.querySelector('.tile-chart[data-tile="table"] plotly-chart')
                     .data[0].cells.values"""
        )
        assert cells == [["Jan", "Feb", "Mar"], ["120", "90.5", "240.25"]]

    def test_the_table_says_it_is_showing_part_of_the_result(self, opened):
        """41 rows in the result, three on screen. A table that shows a slice
        without saying so is a sample presented as an answer."""
        page, _ = opened
        note = page.locator('.tile-note[data-note="table"]').inner_text()
        assert "3 of 41" in note

    def test_a_tile_gets_the_page_theme(self, opened):
        page, _ = opened
        colour = page.evaluate(
            """() => document.querySelector('.tile-chart[data-tile="bar"] plotly-chart')
                     .layout.font.color"""
        )
        dark = page.evaluate(
            """() => document.documentElement.getAttribute('data-theme') === 'dark'"""
        )
        assert colour == ("#e5e7eb" if dark else "#0f172a")

    def test_charts_get_a_toolbar_and_numbers_do_not(self, opened):
        """A dashboard chart is explored -- zoom into the spike, read it off. There
        is nothing to zoom into on one number or a table of text."""
        page, _ = opened
        modebars = page.evaluate(
            """() => Object.fromEntries(
                [...document.querySelectorAll('.tile-chart')].map((mount) => {
                  const chart = mount.querySelector('plotly-chart');
                  return [mount.dataset.tile, chart.config.displayModeBar];
                })
            )"""
        )
        assert modebars == {
            "bar": "hover", "pie": "hover", "metric": False, "table": False,
        }


class TestFailureStaysInItsOwnTile:
    def test_a_failed_tile_does_not_stop_the_others(self, server, browser):
        api = DashboardApi(
            ONE_DATABASE,
            results=[
                {"tile_id": "bar", "error": "permission denied for table salaries"},
                RESULTS[1], RESULTS[2], RESULTS[3],
            ],
        )
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        api.install(page)
        page.goto(server, wait_until="networkidle")
        page.wait_for_selector("#app.ready", timeout=20_000)
        page.click('button[data-view="dashboards"]')
        page.wait_for_selector("#view-other [data-open]", timeout=20_000)
        page.locator("#view-other [data-open]").first.click()
        page.wait_for_selector(".tile-grid", timeout=20_000)
        page.wait_for_timeout(400)

        assert "permission denied" in page.locator(".tile-grid").inner_text()
        drawn = {k: v for k, v in figures(page).items() if v}
        assert drawn == {"pie": "pie", "metric": "indicator", "table": "table"}
        page.close()

    def test_an_empty_result_says_so_rather_than_drawing_nothing(self, server, browser):
        api = DashboardApi(
            ONE_DATABASE,
            results=[{"tile_id": "bar", "columns": ["month"], "rows": [],
                      "row_count": 0}],
            tiles=[TILES[0]],
        )
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        api.install(page)
        page.goto(server, wait_until="networkidle")
        page.wait_for_selector("#app.ready", timeout=20_000)
        page.click('button[data-view="dashboards"]')
        page.wait_for_selector("#view-other [data-open]", timeout=20_000)
        page.locator("#view-other [data-open]").first.click()
        page.wait_for_selector(".tile-grid", timeout=20_000)
        page.wait_for_timeout(300)

        assert page.locator('.tile-chart[data-tile="bar"] plotly-chart').count() == 0
        assert page.locator(".tile-chart .empty").count() == 1
        page.close()


class TestTheBuilderIsShared:
    def test_the_page_and_the_export_load_the_same_module(self):
        """Not a browser assertion -- a file one, and the reason the rest of this
        file is trustworthy: the export inlines this exact source."""
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        page_copy = repo / "frontend/public/assets/shared/tile-figure.js"
        export_copy = repo / "backend/vanna/dashboards/vendor/tile-figure.js"
        assert page_copy.read_text(encoding="utf-8") == export_copy.read_text(
            encoding="utf-8"
        ), "run `make plotly-bundle`"

    def test_the_component_adopts_plotly_css_through_the_cssom(self):
        """The trap this pins is specific and expensive.

        Plotly injects its stylesheet into `document.head`; a shadow root does not
        inherit document styles, so without a copy the plot area is inert -- no
        hover, no drag-zoom -- while the modebar still works and the figure is
        correct. And the obvious fix does not work: Plotly builds the sheet with
        `insertRule`, so the element's `textContent` is empty and cloning the node
        copies nothing at all. The rules have to be read off `sheet.cssRules`.

        Behaviour is covered by `tests/e2e/test_charts_in_browser.py`, which needs
        a running stack. This is the part that can fail on a laptop.
        """
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "frontend/src/components/plotly-chart.ts").read_text(
            encoding="utf-8"
        )
        assert "_adoptPlotlyStyles" in source
        assert "cssRules" in source, (
            "the adopted stylesheet is being copied from textContent, which Plotly "
            "leaves empty -- the charts will be inert"
        )
        assert 'style[id^="plotly.js-style"]' in source

    def test_app_js_has_no_figure_building_of_its_own(self):
        """It had 130 lines of it, which the export then reimplemented in Python.
        If this comes back, so does the drift."""
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1]
                  / "frontend/public/assets/app.js").read_text(encoding="utf-8")
        for gone in ("function plot(", "colorscale", "'tozeroy'", "spec.sort_by"):
            assert gone not in source, f"{gone!r} is back in app.js"
        assert "from './shared/tile-figure.js'" in source


def test_the_results_fixture_matches_the_api_shape():
    """A guard on the fixture itself: these keys are what `/data` returns, and a
    test built on a shape the server does not produce proves nothing."""
    assert json.loads(json.dumps(RESULTS))[0]["tile_id"] == "bar"
    assert {"tile_id", "columns", "rows", "row_count"} <= set(RESULTS[0])
