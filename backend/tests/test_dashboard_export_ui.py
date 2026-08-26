"""The exported dashboard, opened the way a recipient opens it.

Everything else about the export can be asserted on the string. This cannot: that
Plotly, inlined into one file, actually draws every tile kind when the document is
loaded from a `file://` path with no network at all.

That combination is where the plausible implementations fail. A CDN tag passes
every string assertion and renders nothing offline. An ES module -- which is what
`tile-figure.js` is in the application -- is refused by the browser as a
cross-origin request from `file://`, so the charts are silently absent. Neither
shows up anywhere except here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

from vanna.dashboards import export_html  # noqa: E402
from vanna.dashboards.models import (  # noqa: E402
    ChartSpec,
    ChartType,
    Dashboard,
    GridPosition,
    Tile,
    TileKind,
    TileResult,
)

MONTHS = [["Jan", 120.0], ["Feb", 90.5], ["Mar", 240.25]]


def a_dashboard() -> tuple:
    """One dashboard with every tile kind on it, and its results."""
    tiles = [
        Tile(id="bar", kind=TileKind.CHART, title="Revenue by month",
             chart=ChartSpec(type=ChartType.BAR), grid=GridPosition(height=5)),
        Tile(id="line", kind=TileKind.CHART, title="Trend",
             chart=ChartSpec(type=ChartType.LINE), grid=GridPosition(height=5)),
        Tile(id="pie", kind=TileKind.CHART, title="Share",
             chart=ChartSpec(type=ChartType.PIE), grid=GridPosition(height=5)),
        Tile(id="area", kind=TileKind.CHART, title="Cumulative",
             chart=ChartSpec(type=ChartType.AREA), grid=GridPosition(height=5)),
        Tile(id="scatter", kind=TileKind.CHART, title="Correlation",
             chart=ChartSpec(type=ChartType.SCATTER), grid=GridPosition(height=5)),
        Tile(id="heat", kind=TileKind.CHART, title="By month and region",
             chart=ChartSpec(type=ChartType.HEATMAP), grid=GridPosition(height=5)),
        Tile(id="metric", kind=TileKind.METRIC, title="Total revenue"),
        Tile(id="table", kind=TileKind.TABLE, title="Rows"),
        Tile(id="text", kind=TileKind.TEXT, title="Notes", text="## Context"),
    ]
    results = [
        TileResult(tile_id=t, columns=["month", "revenue"], rows=MONTHS, row_count=3)
        for t in ("bar", "line", "pie", "area", "scatter", "table")
    ]
    results.append(TileResult(
        tile_id="heat",
        columns=["month", "region", "revenue"],
        rows=[["Jan", "EU", 10.0], ["Jan", "US", 20.0],
              ["Feb", "EU", 30.0], ["Feb", "US", 40.0]],
        row_count=4,
    ))
    results.append(TileResult(
        tile_id="metric", columns=["revenue"], rows=[[450.75]], row_count=1
    ))
    return Dashboard(id="d1", tenant_id="acme", title="Q3 review", tiles=tiles), results


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """The export, written to disk and opened from a file:// URL, offline."""
    dashboard, results = a_dashboard()
    html = export_html(
        dashboard, results, exported_by="ana@acme.test", workspace="Acme"
    )
    path = tmp_path_factory.mktemp("export") / "dashboard.html"
    path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(offline=True)
        page = context.new_page()
        errors: list = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # Any request at all would be a network dependency; there should be none
        # beyond the document itself.
        requests: list = []
        page.on("request", lambda r: requests.append(r.url))

        page.goto(path.as_uri())
        page.wait_for_selector(".js-plotly-plot", timeout=30_000)
        page.wait_for_timeout(1500)

        yield {
            "page": page,
            "errors": errors,
            "requests": [u for u in requests if not u.startswith("file:")],
            "size": len(html),
        }
        browser.close()


class TestItDrawsOffline:
    def test_every_tile_with_data_became_a_plotly_figure(self, rendered):
        page = rendered["page"]
        # Eight drawable tiles; the ninth is text.
        assert page.locator(".figure").count() == 8
        assert page.locator(".js-plotly-plot").count() == 8

    def test_no_page_or_console_errors(self, rendered):
        assert rendered["errors"] == []

    def test_nothing_was_fetched(self, rendered):
        """The context is offline, so a CDN tag would also fail loudly -- but this
        is the assertion that says *why* it works, rather than that it happened
        to."""
        assert rendered["requests"] == []

    def test_the_traces_are_the_ones_the_tiles_asked_for(self, rendered):
        """Not "a chart appeared" -- the *right* chart. The renderer this replaced
        drew a bar chart for a heatmap tile, which looks like an answer."""
        page = rendered["page"]
        kinds = page.evaluate(
            """() => Object.fromEntries(
                [...document.querySelectorAll('.figure')].map((node) => [
                  node.dataset.tile,
                  (node.data || []).map((trace) => trace.type).join(','),
                ])
            )"""
        )
        assert kinds["bar"] == "bar"
        assert kinds["pie"] == "pie"
        assert kinds["heat"] == "heatmap"
        assert kinds["metric"] == "indicator"
        assert kinds["table"] == "table"
        # line, area and scatter are all `scatter` traces; the mode is the
        # difference, and it is asserted below.
        assert kinds["line"] == "scatter"
        assert kinds["area"] == "scatter"
        assert kinds["scatter"] == "scatter"

    def test_line_area_and_scatter_are_not_the_same_chart(self, rendered):
        modes = rendered["page"].evaluate(
            """() => Object.fromEntries(
                ['line', 'area', 'scatter'].map((id) => {
                  const node = document.querySelector(`.figure[data-tile="${id}"]`);
                  const trace = (node.data || [])[0] || {};
                  return [id, `${trace.mode}|${trace.fill || 'none'}`];
                })
            )"""
        )
        assert modes["line"] == "lines+markers|none"
        assert modes["area"] == "lines+markers|tozeroy"
        assert modes["scatter"] == "markers|none"

    def test_the_metric_shows_its_number(self, rendered):
        """An `indicator`, not a heading: the number is drawn by Plotly."""
        page = rendered["page"]
        value = page.evaluate(
            """() => document.querySelector('.figure[data-tile="metric"]').data[0].value"""
        )
        assert value == 450.75
        assert "450.75" in page.locator('.figure[data-tile="metric"]').inner_text()

    def test_the_table_says_how_many_rows_it_is_showing(self, rendered):
        """A table showing part of a result without saying so is a sample presented
        as an answer."""
        note = rendered["page"].locator('.note[data-note="table"]').inner_text()
        assert "3 rows" in note

    def test_the_text_tile_is_prose_not_a_figure(self, rendered):
        page = rendered["page"]
        assert page.locator('.figure[data-tile="text"]').count() == 0
        assert "## Context" in page.content()

    def test_the_file_is_still_emailable(self, rendered):
        """The whole reason the bundle is a hand-picked trace list rather than the
        4.9 MB distribution."""
        megabytes = rendered["size"] / 1_000_000
        assert megabytes < 2.0, f"{megabytes:.1f} MB is too big to send"

    def test_hovering_a_bar_reads_the_number_off_it(self, rendered):
        """The point of real Plotly over a drawn picture, and it has to be checked
        with a pointer. Asserting that the node is a live graph passes on a chart
        nothing can reach -- which is exactly how the dashboard page shipped
        inert.

        Aimed at the middle of the tallest bar through Plotly's own axes: a
        fraction of the plot box lands in the empty space above a short bar on a
        ranked chart, where Plotly correctly shows nothing.
        """
        page = rendered["page"]
        target = page.evaluate(
            """() => {
            const gd = document.querySelector('.figure[data-tile="bar"]');
            const fl = gd._fullLayout;
            const trace = gd.data[0];
            let best = 0;
            trace.y.forEach((v, i) => {
              if (Math.abs(v) > Math.abs(trace.y[best])) best = i;
            });
            const box = gd.getBoundingClientRect();
            return {
              px: box.left + fl.xaxis._offset + fl.xaxis.d2p(trace.x[best]),
              py: box.top + fl.yaxis._offset + fl.yaxis.l2p(trace.y[best] / 2),
              label: String(trace.x[best]),
            };
        }"""
        )
        page.mouse.move(target["px"] - 30, target["py"] - 30)
        page.mouse.move(target["px"], target["py"], steps=8)
        page.wait_for_timeout(700)

        labels = page.evaluate(
            """() => [...document.querySelectorAll(
                '.figure[data-tile="bar"] .hovertext text')].map((n) => n.textContent)"""
        )
        assert labels, "hovering a bar in the exported file produced no tooltip"
        assert any(target["label"] in text for text in labels), labels

    def test_dragging_zooms_the_exported_chart(self, rendered):
        """A snapshot people explore, not a picture. This is what the inlined
        Plotly buys over the SVG renderer that used to be here."""
        page = rendered["page"]
        area = page.evaluate(
            """() => {
            const gd = document.querySelector('.figure[data-tile="bar"]');
            const fl = gd._fullLayout;
            const box = gd.getBoundingClientRect();
            return { left: box.left + fl.xaxis._offset,
                     top: box.top + fl.yaxis._offset,
                     width: fl.xaxis._length, height: fl.yaxis._length,
                     range: fl.xaxis.range.slice() };
        }"""
        )
        y = area["top"] + area["height"] * 0.5
        left = area["left"] + area["width"] * 0.3
        right = area["left"] + area["width"] * 0.7
        page.mouse.move(left, y)
        page.mouse.down()
        page.mouse.move(right, y, steps=10)
        page.mouse.up()
        page.wait_for_timeout(700)

        after = page.evaluate(
            """() => document.querySelector('.figure[data-tile="bar"]')
                     ._fullLayout.xaxis.range.slice()"""
        )
        assert after != area["range"], "dragging did not zoom the exported chart"
        page.mouse.dblclick((left + right) / 2, y)
        page.wait_for_timeout(600)
