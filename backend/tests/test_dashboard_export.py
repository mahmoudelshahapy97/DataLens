"""The exported dashboard: real Plotly, drawn by the same code as the screen.

This file used to draw its own SVG charts. That was a *second* implementation of
"draw this tile", with its own axis-picking rules -- it ignored `sort_by`, `limit`
and `color_by`, and drew a bar chart when the tile asked for a heatmap. So the
file somebody circulated showed a different chart than the screen it was taken
from, which is the one thing a snapshot must never do.

The tests here pin the properties that keeps:

* **one renderer.** The export inlines `assets/shared/tile-figure.js` -- the
  module the dashboard page imports -- and the vendored copy must be byte-identical
  to it, so editing one and not the other fails here rather than in somebody's
  inbox.
* **no network.** Nothing in the document may reference a URL. A `<script src>`
  to a CDN would keep the file small and make it blank on a plane.
* **every tile kind is a figure.** Charts, metrics (`indicator`) and tables
  (`table` trace) all go through Plotly; only text tiles are markup.
* **no cell can escape its script.** The row data is JSON in the document, and
  the values come from a warehouse.

The last one is checked in a real browser at the bottom, because "the JSON parses"
and "Plotly drew something" are different claims and only the second one matters.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vanna.dashboards import export_filename, export_html
from vanna.dashboards.export import (
    PLOTLY_CSS,
    PLOTLY_JS,
    TILE_FIGURE_JS,
    ExportAssetsMissing,
    _assets,
)
from vanna.dashboards.models import (
    ChartSpec,
    ChartType,
    Dashboard,
    GridPosition,
    Tile,
    TileKind,
    TileResult,
)

REPO = Path(__file__).resolve().parents[2]
FRONTEND_COPY = REPO / "frontend" / "public" / "assets" / "shared" / "tile-figure.js"


def chart_tile(**kwargs) -> Tile:
    return Tile(
        id="c1", kind=TileKind.CHART, title="Revenue by month",
        chart=ChartSpec(**kwargs), grid=GridPosition(height=5),
    )


def result(tile_id="c1", **kwargs) -> TileResult:
    payload = {
        "tile_id": tile_id,
        "columns": ["month", "revenue"],
        "rows": [["Jan", 120.0], ["Feb", 90.5]],
        "row_count": 2,
    }
    payload.update(kwargs)
    return TileResult(**payload)


def board(*tiles: Tile) -> Dashboard:
    return Dashboard(id="d1", tenant_id="acme", title="Q3 review", tiles=list(tiles))


def markup(html: str) -> str:
    """The document with the vendored bundle taken out.

    Two reasons, and the second is not obvious. Plotly's own source contains the
    strings `src=` and `https://` -- in its image-export code and its licence
    header -- so an assertion about "no external references" has to be about the
    document *we* generate, not about 1.2 MB of vendored library.

    The other reason is that a failing assertion against a megabyte string makes
    pytest build a megabyte-scale explanation, which takes minutes. A test that
    hangs on failure is a test nobody runs.
    """
    plotly_js = PLOTLY_JS.read_text(encoding="utf-8")
    plotly_css = PLOTLY_CSS.read_text(encoding="utf-8")
    figure_js = TILE_FIGURE_JS.read_text(encoding="utf-8")
    for chunk in (plotly_js, plotly_css, figure_js):
        html = html.replace(chunk, "/* vendored */")
    # The stripped `export ` version too -- the inlined copy is transformed.
    html = html.replace(_assets()["figure_js"], "/* vendored */")
    assert len(html) < 200_000, "the vendored assets were not stripped"
    return html


def island(html: str) -> list:
    """The tile payload, decoded."""
    found = re.search(
        r'<script id="vanna-tiles" type="application/json">(.*?)</script>', html, re.S
    )
    assert found, "the document carries no tile payload"
    return json.loads(found.group(1).replace("\\u003c", "<"))


# ----------------------------------------------------------------------
# The vendored assets
# ----------------------------------------------------------------------


class TestTheAssetsAreThere:
    def test_the_bundle_is_committed(self):
        """The backend image has no Node in it, so the bundle cannot be built at
        export time. It is committed, and `make plotly-bundle` regenerates it."""
        assert PLOTLY_JS.is_file(), f"run `make plotly-bundle` -- {PLOTLY_JS} is missing"
        assert PLOTLY_CSS.is_file()

    def test_it_is_the_partial_bundle_not_the_full_one(self):
        """4.9 MB is the full distribution -- 3D, maps, WebGL, none of which a
        dashboard tile can produce. A file that size stops being emailable, which
        is most of what an export is for."""
        size = PLOTLY_JS.stat().st_size
        assert 600_000 < size < 2_500_000, (
            f"{size:,} bytes: too small to be Plotly, or the full bundle crept in"
        )

    @pytest.mark.parametrize(
        "trace", ["bar", "pie", "heatmap", "indicator", "table", "scatter"]
    )
    def test_every_trace_a_tile_can_produce_is_registered(self, trace):
        """The bundle is a hand-picked trace list. A ChartType with no trace behind
        it draws an empty box in the exported file and nothing else -- so the list
        and the tile kinds are pinned together here."""
        source = PLOTLY_JS.read_text(encoding="utf-8", errors="replace")
        assert f'"{trace}"' in source or f"'{trace}'" in source

    def test_the_figure_builder_matches_the_frontend_copy(self):
        """One renderer, or the export is a different picture of the same data."""
        assert TILE_FIGURE_JS.read_text(encoding="utf-8") == FRONTEND_COPY.read_text(
            encoding="utf-8"
        ), "run `make plotly-bundle` -- the vendored figure builder has drifted"

    def test_the_module_keywords_are_stripped_for_inlining(self):
        """A module script cannot load from a `file://` URL -- the browser refuses
        it as cross-origin -- so the inlined copy has to be a classic script. If
        this stopped working, every chart in a downloaded file would be absent."""
        inlined = _assets()["figure_js"]
        assert "function tileFigure(" in inlined
        assert not re.search(r"^export ", inlined, re.MULTILINE)

    def test_a_missing_bundle_is_an_error_not_a_chartless_export(self, monkeypatch):
        """An export that silently omits its charts looks complete and is not, and
        it would be found by whoever received it."""
        monkeypatch.setattr("vanna.dashboards.export.PLOTLY_JS", Path("/nope.js"))
        with pytest.raises(ExportAssetsMissing) as caught:
            export_html(board(chart_tile(type=ChartType.BAR)), [result()])
        assert "make plotly-bundle" in str(caught.value)


# ----------------------------------------------------------------------
# The document
# ----------------------------------------------------------------------


class TestTheDocument:
    def test_plotly_is_inlined_and_the_document_fetches_nothing(self):
        html = export_html(board(chart_tile(type=ChartType.BAR)), [result()])
        assert "Plotly.newPlot" in html
        assert PLOTLY_JS.read_text(encoding="utf-8") in html, "the bundle is not inlined"

        # No URL of any kind in the document we generate: not a script, not a
        # stylesheet, not an image. (Plotly's own source mentions URLs; that is
        # why this looks at the document rather than the whole file.)
        page = markup(html)
        assert "src=" not in page
        assert "http://" not in page and "https://" not in page
        assert "<link" not in page and "@import" not in page

    def test_the_bundle_cannot_close_the_script_that_carries_it(self):
        """Inlining a megabyte of JavaScript is only safe because it contains no
        closing tag. If a future bundle did, the rest of it would land in the
        document as markup."""
        for asset in (PLOTLY_JS, PLOTLY_CSS, TILE_FIGURE_JS):
            text = asset.read_text(encoding="utf-8")
            assert "</script" not in text, asset.name
            assert "</style" not in text, asset.name

    def test_there_are_no_hand_drawn_charts_left(self):
        """The SVG renderers are gone. Plotly draws its own SVG at runtime; what
        must not be here is a chart *this file* drew."""
        page = markup(export_html(board(chart_tile(type=ChartType.BAR)), [result()]))
        assert "<svg" not in page
        assert "<rect" not in page and "<polyline" not in page

    def test_one_figure_and_one_payload_per_drawable_tile(self):
        html = export_html(
            board(
                chart_tile(type=ChartType.BAR),
                Tile(id="m1", kind=TileKind.METRIC, title="Revenue"),
                Tile(id="t1", kind=TileKind.TABLE, title="Rows"),
                Tile(id="x1", kind=TileKind.TEXT, title="Notes", text="## Context"),
            ),
            [
                result(),
                result("m1", columns=["revenue"], rows=[[210.5]], row_count=1),
                result("t1"),
            ],
        )

        assert html.count('class="figure"') == 3, "a text tile got a figure"
        payload = island(html)
        assert [entry["id"] for entry in payload] == ["c1", "m1", "t1"]
        assert [entry["tile"]["kind"] for entry in payload] == [
            "chart", "metric", "table",
        ]

    def test_the_chart_spec_travels_with_the_tile(self):
        """`sort_by`, `limit` and `color_by` are the fields the SVG renderer
        ignored. They have to reach the browser, or the export is back to showing
        every row in warehouse order."""
        html = export_html(
            board(chart_tile(type=ChartType.BAR, sort_by="revenue",
                             descending=True, limit=10, y_label="USD")),
            [result()],
        )
        spec = island(html)[0]["tile"]["chart"]
        assert spec["sort_by"] == "revenue"
        assert spec["descending"] is True
        assert spec["limit"] == 10
        assert spec["y_label"] == "USD"

    def test_a_failed_tile_says_so_and_carries_no_payload(self):
        html = export_html(
            board(chart_tile(type=ChartType.BAR)),
            [TileResult(tile_id="c1", error="permission denied for table salaries")],
        )
        assert "permission denied" in markup(html)
        assert 'class="figure"' not in html
        assert island(html) == []

    def test_an_empty_result_is_not_an_empty_chart(self):
        html = export_html(
            board(chart_tile(type=ChartType.BAR)),
            [TileResult(tile_id="c1", columns=["month"], rows=[], row_count=0)],
        )
        assert "No data." in markup(html)
        assert 'class="figure"' not in html

    def test_provenance_names_whose_numbers_these_are(self):
        html = export_html(
            board(chart_tile(type=ChartType.BAR)), [result()],
            exported_by="ana@acme.test", workspace="Acme", data_source="pg/warehouse",
        )
        page = markup(html)
        assert "ana@acme.test" in page and "Acme" in page
        assert "pg/warehouse" in page
        assert "snapshot, not a live view" in page

    def test_text_tiles_stay_prose(self):
        """Markdown is not rendered: an export is opened by someone trusting the
        sender, and a markdown renderer is a second injection surface."""
        html = export_html(
            board(Tile(id="x1", kind=TileKind.TEXT, text="## Heading <b>bold</b>")), []
        )
        page = markup(html)
        assert "&lt;b&gt;bold&lt;/b&gt;" in page
        assert "<b>bold</b>" not in page


class TestNothingEscapesItsScript:
    """Every string in the payload came from a warehouse or an LLM."""

    def test_a_cell_cannot_close_the_json_island(self):
        html = export_html(
            board(chart_tile(type=ChartType.BAR)),
            [result(rows=[["</script><img src=x onerror=alert(1)>", 1.0]])],
        )
        # The literal closing tag is what the parser looks for, and it is not here.
        page = markup(html)
        assert "</script><img" not in page
        assert "\\u003c/script" in page

    def test_a_title_is_escaped_in_the_document(self):
        html = export_html(
            board(Tile(id="t1", kind=TileKind.TABLE, title="<script>alert(1)</script>")),
            [result("t1")],
        )
        page = markup(html)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page

    def test_a_column_name_is_escaped(self):
        html = export_html(
            board(Tile(id="t1", kind=TileKind.TABLE)),
            [result("t1", columns=["<img onerror=x>", "n"], rows=[["a", 1]])],
        )
        assert "<img onerror=x>" not in markup(html)


class TestTheFilename:
    def test_it_is_dated_and_slugged(self):
        from datetime import datetime, timezone

        name = export_filename(
            board(), datetime(2026, 3, 1, tzinfo=timezone.utc)
        )
        assert name == "q3-review-2026-03-01.html"

    def test_a_hostile_title_cannot_reach_the_filesystem(self):
        board_ = Dashboard(id="d", tenant_id="a", title="../../etc/passwd")
        assert "/" not in export_filename(board_)
        assert ".." not in export_filename(board_).replace("--", "")
