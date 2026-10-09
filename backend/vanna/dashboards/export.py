"""Render an executed dashboard as one self-contained HTML file.

The point of this file is that it survives leaving the building. A dashboard in
this system is rows in Postgres behind a session cookie, which is right for the
people in the workspace and useless for the person who asked them for the number.
An export is the answer: one file, openable from a `file://` path, on a laptop
with no network and no account.

Four decisions follow from that, and each rules out something that would
otherwise seem obvious.

**It is a snapshot, not a live view.** No connection string, no API base URL, no
credentials -- there is nothing in the file that could be used to reach the
warehouse, because the data is already in it. The cost is that it goes stale, so
the header says who exported it and when in plain words. An undated number is how
a stale figure ends up in a board pack.

**Charts are real Plotly, drawn by the same code as the screen.** This used to be
a set of hand-written SVG renderers living here -- and they were a *second*
implementation of "draw this tile", with their own axis-picking rules. They
ignored `sort_by`, `limit` and `color_by`, and drew a bar chart when the tile
asked for a heatmap. So the file somebody circulated showed a different chart than
the screen it was taken from, which is the one thing a snapshot must never do.
The figure now comes from `assets/shared/tile-figure.js`, inlined, which is the
module the dashboard page itself imports.

**Plotly is inlined, not fetched.** A `<script src="https://cdn.plot.ly/...">`
would keep this file at 15 KB and make it blank on a plane, which defeats the
purpose. The bundle in `vendor/` is a custom build carrying only the six trace
types a tile can produce -- 1.2 MB rather than the 4.9 MB full distribution. See
`tools/plotly_export_bundle/entry.js`.

**Everything is escaped.** Column names, cell values, titles and warnings all
come from a database or an LLM, and the output is HTML opened by someone who
trusts the sender. Row data goes into a JSON island with `<` escaped, so no cell
value can close the script that carries it.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import Dashboard, Tile, TileKind, TileResult

#: The vendored browser code. Committed rather than built here: the backend image
#: has no Node in it, and an export must not depend on a build step happening
#: somewhere else. `make plotly-bundle` regenerates both.
VENDOR = Path(__file__).resolve().parent / "vendor"
PLOTLY_JS = VENDOR / "plotly-export.min.js"
PLOTLY_CSS = VENDOR / "plotly-export.min.css"
TILE_FIGURE_JS = VENDOR / "tile-figure.js"


class ExportAssetsMissing(RuntimeError):
    """The vendored browser assets are not in the image.

    Raised rather than degraded. An export that silently omits its charts is a
    document that looks complete and is not, and it would be discovered by
    whoever received it rather than by whoever built the image.
    """


def _esc(value: Any) -> str:
    """HTML-escape, including quotes, for text and attribute contexts alike."""
    return html.escape("" if value is None else str(value), quote=True)


def _json_island(payload: Any) -> str:
    """JSON that cannot escape the ``<script>`` element carrying it.

    Escaping ``<`` is what does it: a cell containing ``</script>`` would
    otherwise end the element and put the rest of the row into the document as
    markup. Escaping the quote characters instead would not help -- the parser
    looks for the literal string, not for balanced quotes.
    """
    return json.dumps(payload, default=str).replace("<", "\\u003c")


def _assets() -> Dict[str, str]:
    """The vendored JS and CSS, read once per export.

    ``tile-figure.js`` is an ES module, and a module script cannot load from a
    ``file://`` URL -- the browser refuses it as a cross-origin request, which
    would make every chart in a downloaded file silently absent. So the ``export``
    keywords are stripped and it is inlined as a classic script. The stripping is
    asserted in the tests, because a module that quietly failed to define
    ``tileFigure`` would produce an export with empty boxes.
    """
    missing = [p.name for p in (PLOTLY_JS, TILE_FIGURE_JS) if not p.is_file()]
    if missing:
        raise ExportAssetsMissing(
            f"{', '.join(missing)} is not in {VENDOR}. Run `make plotly-bundle`; "
            "without it an export would have no charts in it."
        )

    figure_source = TILE_FIGURE_JS.read_text(encoding="utf-8")
    figure_source = "\n".join(
        line[len("export ") :] if line.startswith("export ") else line
        for line in figure_source.splitlines()
    )

    return {
        "plotly_js": PLOTLY_JS.read_text(encoding="utf-8"),
        "plotly_css": PLOTLY_CSS.read_text(encoding="utf-8") if PLOTLY_CSS.is_file() else "",
        "figure_js": figure_source,
    }


# ----------------------------------------------------------------------
# Tiles
# ----------------------------------------------------------------------


def _tile_payload(tile: Tile, result: Optional[TileResult]) -> Optional[Dict[str, Any]]:
    """What the browser code needs to draw this tile, or None if it draws nothing.

    The tile is passed through in the shape ``tileFigure`` expects -- the same
    shape the API hands the dashboard page -- so the two callers cannot drift
    apart in what they supply either.
    """
    if tile.kind == TileKind.TEXT or result is None or result.error:
        return None
    if not result.columns or not result.rows:
        return None

    chart: Optional[Dict[str, Any]] = None
    if tile.chart is not None:
        chart = tile.chart.model_dump(mode="json", exclude_none=True)

    return {
        "id": tile.id,
        "tile": {"id": tile.id, "kind": tile.kind.value, "title": tile.title,
                 "chart": chart, "grid": {"height": tile.grid.height}},
        "result": {
            "columns": list(result.columns),
            "rows": [list(row) for row in result.rows],
            "row_count": result.row_count,
            "truncated": result.truncated,
        },
    }


def _render_tile(tile: Tile, result: Optional[TileResult]) -> str:
    title = _esc(tile.title or tile.id)
    description = (
        f'<p class="muted small">{_esc(tile.description)}</p>' if tile.description else ""
    )

    if tile.kind == TileKind.TEXT:
        # Markdown is *not* rendered: an export is a document someone opens
        # trusting the sender, and a markdown renderer is a second HTML-injection
        # surface for the sake of bold text.
        body = f'<p class="text-tile">{_esc(tile.text or "")}</p>'
        return f'<section class="tile"><h2>{title}</h2>{description}{body}</section>'

    warnings = "".join(
        f'<p class="warn">{_esc(w)}</p>' for w in (result.warnings if result else [])
    )
    truncated = (
        '<p class="warn">The result was truncated by the row limit, so totals '
        "here may be incomplete.</p>"
        if result is not None and result.truncated
        else ""
    )

    if result is None:
        body = '<p class="muted">This tile was not executed.</p>'
    elif result.error:
        body = f'<p class="error">{_esc(result.error)}</p>'
    elif not result.columns or not result.rows:
        body = '<p class="muted">No data.</p>'
    else:
        # A metric's indicator carries its own label, so a heading above it would
        # say the same thing twice.
        height = max(
            120 if tile.kind == TileKind.METRIC else 220,
            (tile.grid.height or 5) * 52,
        )
        body = (
            f'<div class="figure" data-tile="{_esc(tile.id)}" '
            f'style="height:{height}px"></div>'
            f'<p class="muted small note" data-note="{_esc(tile.id)}"></p>'
        )

    heading = "" if tile.kind == TileKind.METRIC and result and not result.error else f"<h2>{title}</h2>"
    return (
        f'<section class="tile">{heading}{description}'
        f"{warnings}{truncated}{body}</section>"
    )


# ----------------------------------------------------------------------
# Document
# ----------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark;
  --bg:#f8fafc; --card:#fff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
  --warn:#b45309; --bad:#dc2626; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0b1120; --card:#111827; --ink:#e5e7eb; --muted:#94a3b8; --line:#1f2937;
  --warn:#fbbf24; --bad:#f87171; } }
* { box-sizing:border-box; }
body { margin:0; padding:28px 20px 60px; background:var(--bg); color:var(--ink);
  font:15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:900px; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 6px; }
h2 { font-size:1rem; margin:0 0 10px; }
.provenance { color:var(--muted); font-size:.8125rem; margin:0 0 22px;
  padding-bottom:16px; border-bottom:1px solid var(--line); }
.tile { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px; margin-bottom:14px; }
.muted { color:var(--muted); } .small { font-size:.8125rem; }
.warn { color:var(--warn); font-size:.8125rem; margin:0 0 8px; }
.error { color:var(--bad); font-size:.875rem; margin:0; }
.text-tile { white-space:pre-wrap; margin:0; }
.figure { width:100%; }
.note { margin:8px 0 0; }
footer { color:var(--muted); font-size:.75rem; margin-top:26px;
  padding-top:14px; border-top:1px solid var(--line); }
"""

#: Drawn after the document exists, one figure per tile, so a single bad tile
#: cannot stop the others. `matchMedia` rather than a stored flag: the file is
#: read on somebody else's laptop, and the theme that matters is theirs.
_DRAW_JS = """
(function () {
  var tiles = JSON.parse(document.getElementById('vanna-tiles').textContent);
  var dark = window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches;

  tiles.forEach(function (entry) {
    var mount = document.querySelector('.figure[data-tile="' + entry.id + '"]');
    if (!mount) return;
    var note = document.querySelector('.note[data-note="' + entry.id + '"]');
    try {
      var box = Math.round(mount.getBoundingClientRect().height) || 300;
      var figure = tileFigure(entry.tile, entry.result, { height: box, dark: dark });
      if (!figure) {
        mount.textContent = 'No data.';
        return;
      }
      Plotly.newPlot(mount, figure.traces, figure.layout, figure.config);
      if (note && figure.note) note.textContent = figure.note;
    } catch (error) {
      mount.textContent = 'This tile could not be drawn: ' + error.message;
    }
  });
})();
"""


def export_html(
    dashboard: Dashboard,
    results: Sequence[TileResult],
    *,
    exported_by: str = "",
    workspace: str = "",
    data_source: str = "",
    exported_at: Optional[datetime] = None,
) -> str:
    """Render a dashboard and its executed results as one HTML document.

    Args:
        dashboard: The dashboard definition.
        results: What ``render_dashboard`` returned, in any order.
        exported_by: Who ran it. Stamped into the document, because the numbers
            reflect *their* row- and column-level access and a reader has no
            other way to know whose view they are looking at.
        workspace: Workspace name, for the same reason.
        data_source: Credential-free data source label.
        exported_at: Defaults to now, UTC.

    Returns:
        A complete HTML document with no external references of any kind.

    Raises:
        ExportAssetsMissing: If the vendored Plotly bundle is not in the image.
    """
    assets = _assets()
    by_id: Dict[str, TileResult] = {r.tile_id: r for r in results}
    when = (exported_at or datetime.now(timezone.utc)).strftime("%d %B %Y at %H:%M UTC")

    tiles = "".join(_render_tile(tile, by_id.get(tile.id)) for tile in dashboard.tiles)

    payloads: List[Dict[str, Any]] = []
    for tile in dashboard.tiles:
        payload = _tile_payload(tile, by_id.get(tile.id))
        if payload is not None:
            payloads.append(payload)

    provenance_bits = [f"Snapshot taken {_esc(when)}"]
    if exported_by:
        provenance_bits.append(f"by {_esc(exported_by)}")
    if workspace:
        provenance_bits.append(f"in {_esc(workspace)}")
    provenance = ", ".join(provenance_bits) + "."

    source_line = f" Data source: {_esc(data_source)}." if data_source else ""

    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(dashboard.title or 'Dashboard')}</title>
<style>{_CSS}</style>
<style>{assets['plotly_css']}</style>
<main>
  <h1>{_esc(dashboard.title or 'Dashboard')}</h1>
  <p class="provenance">
    {provenance}{source_line}
    These are the figures visible to that person under this workspace's access
    rules; someone else may see different numbers. The data is embedded, so this
    file does not update &mdash; it is a snapshot, not a live view.
  </p>
  {tiles or '<section class="tile"><p class="muted">This dashboard has no tiles.</p></section>'}
  <footer>
    Exported from DataLens. No connection details are contained in this file.
  </footer>
</main>
<script id="vanna-tiles" type="application/json">{_json_island(payloads)}</script>
<script>{assets['plotly_js']}</script>
<script>{assets['figure_js']}
{_DRAW_JS}</script>
</html>
"""


def export_filename(dashboard: Dashboard, exported_at: Optional[datetime] = None) -> str:
    """A safe, dated filename. Never derived unescaped from a user-supplied title."""
    stamp = (exported_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    slug = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in (dashboard.title or "dashboard")
    ).strip("-").lower() or "dashboard"
    return f"{slug[:60]}-{stamp}.html"
