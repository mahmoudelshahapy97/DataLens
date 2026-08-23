"""Render an executed dashboard as one self-contained HTML file.

The point of this file is that it survives leaving the building. A dashboard in
this system is rows in Postgres behind a session cookie, which is right for the
people in the workspace and useless for the person who asked them for the number.
An export is the answer: one file, openable from a `file://` path, on a laptop
with no network and no account.

Three decisions follow from that and are worth stating, because each rules out
something that would otherwise seem obvious.

**It is a snapshot, not a live view.** No connection string, no API base URL, no
credentials -- there is nothing in the file that could be used to reach the
warehouse, because the data is already in it. The cost is that it goes stale, so
the header says who exported it and when in plain words. An undated number is
how a stale figure ends up in a board pack.

**Charts are drawn as inline SVG rather than by a charting library.** Inlining
Plotly turns an 80 KB report into something over 3 MB, and a file that cannot be
emailed fails at the one job it has. Bar, line and pie cover what dashboard
tiles actually use.

**Everything is escaped.** Column names, cell values, titles and warnings all
come from a database or an LLM, and the output is HTML opened by someone who
trusts the sender.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .models import ChartType, Dashboard, Tile, TileKind, TileResult

#: Categorical palette. Colour-blind-safe ordering (blue/orange first), because
#: the two most common series in any chart should never be red/green.
PALETTE = (
    "#4f46e5", "#ea7317", "#059669", "#dc2626",
    "#0891b2", "#7c3aed", "#a16207", "#db2777",
)


def _esc(value: Any) -> str:
    """HTML-escape, including quotes, for text and attribute contexts alike."""
    return html.escape("" if value is None else str(value), quote=True)


def _number(value: Any) -> Optional[float]:
    """Coerce a cell to a float, or None if it is not numeric.

    Dates, labels and NULLs all land here; returning None rather than raising
    lets a chart skip a bad point instead of failing the whole tile.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------


def _axis_pick(columns: Sequence[str], rows: Sequence[Sequence[Any]], chart) -> tuple:
    """Decide which column is the label and which are the values.

    Honours an explicit ChartSpec when the dashboard supplies one, and falls
    back to "first column labels, first numeric column values" -- the shape
    almost every ``GROUP BY`` produces.
    """
    if not columns:
        return None, []

    label_col = None
    if chart is not None and getattr(chart, "x", None) in columns:
        label_col = columns.index(chart.x)

    value_cols: List[int] = []
    if chart is not None and getattr(chart, "y", None):
        value_cols = [columns.index(c) for c in chart.y if c in columns]

    if label_col is None:
        label_col = 0
    if not value_cols:
        for index, _ in enumerate(columns):
            if index == label_col:
                continue
            if any(_number(row[index]) is not None for row in rows[:20] if len(row) > index):
                value_cols.append(index)
        value_cols = value_cols[:4]   # more than four series is unreadable anyway

    return label_col, value_cols


def _svg_bar_or_line(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    chart,
    *,
    line: bool,
) -> str:
    label_col, value_cols = _axis_pick(columns, rows, chart)
    if label_col is None or not value_cols:
        return '<p class="muted">Nothing numeric to plot.</p>'

    # Cap the points drawn. A 5,000-row series is unreadable at any width, and
    # the SVG for it is larger than the table it came from.
    data = list(rows)[:60]
    labels = [str(r[label_col]) if len(r) > label_col else "" for r in data]
    series = [
        [(_number(r[c]) if len(r) > c else None) or 0.0 for r in data]
        for c in value_cols
    ]
    if not any(any(s) for s in series):
        return '<p class="muted">All values are zero or empty.</p>'

    width, height = 720, 260
    pad_l, pad_b, pad_t, pad_r = 56, 46, 12, 12
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    high = max(max(s) for s in series)
    low = min(min(s) for s in series)
    low = min(0.0, low)
    span = (high - low) or 1.0

    def y_of(v: float) -> float:
        return pad_t + plot_h - ((v - low) / span) * plot_h

    parts: List[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" preserveAspectRatio="xMidYMid meet">'
    ]

    # Horizontal guides, with values, so the chart can be read without hovering.
    for step in range(5):
        v = low + span * step / 4
        y = y_of(v)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'class="grid" />'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="ax" text-anchor="end">'
            f"{_esc(f'{v:,.4g}')}</text>"
        )

    slot = plot_w / max(1, len(data))

    if line:
        for si, values in enumerate(series):
            points = " ".join(
                f"{pad_l + slot * (i + 0.5):.1f},{y_of(v):.1f}"
                for i, v in enumerate(values)
            )
            parts.append(
                f'<polyline points="{points}" fill="none" '
                f'stroke="{PALETTE[si % len(PALETTE)]}" stroke-width="2.5" '
                f'stroke-linejoin="round" />'
            )
    else:
        group = slot / (len(series) + 0.5)
        for si, values in enumerate(series):
            for i, v in enumerate(values):
                x = pad_l + slot * i + group * si + group * 0.25
                y = y_of(v)
                bar_h = abs(y_of(0) - y)
                parts.append(
                    f'<rect x="{x:.1f}" y="{min(y, y_of(0)):.1f}" '
                    f'width="{max(1.0, group * 0.8):.1f}" height="{max(1.0, bar_h):.1f}" '
                    f'fill="{PALETTE[si % len(PALETTE)]}" rx="2" />'
                )

    # Label every nth point so they never overlap.
    stride = max(1, len(labels) // 12)
    for i, label in enumerate(labels):
        if i % stride:
            continue
        x = pad_l + slot * (i + 0.5)
        text = label if len(label) <= 14 else label[:13] + "…"
        parts.append(
            f'<text x="{x:.1f}" y="{height - pad_b + 18}" class="ax" '
            f'text-anchor="middle">{_esc(text)}</text>'
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{y_of(0):.1f}" x2="{width - pad_r}" '
        f'y2="{y_of(0):.1f}" class="axis" />'
    )
    parts.append("</svg>")

    if len(series) > 1:
        legend = " ".join(
            f'<span class="key"><i style="background:{PALETTE[i % len(PALETTE)]}"></i>'
            f"{_esc(columns[c])}</span>"
            for i, c in enumerate(value_cols)
        )
        parts.append(f'<div class="legend">{legend}</div>')

    if len(rows) > len(data):
        parts.append(
            f'<p class="muted small">Showing the first {len(data)} of '
            f"{len(rows):,} rows.</p>"
        )
    return "".join(parts)


def _svg_pie(columns: Sequence[str], rows: Sequence[Sequence[Any]], chart) -> str:
    label_col, value_cols = _axis_pick(columns, rows, chart)
    if label_col is None or not value_cols:
        return '<p class="muted">Nothing numeric to plot.</p>'

    value_col = value_cols[0]
    pairs = [
        (str(r[label_col]), (_number(r[value_col]) or 0.0))
        for r in rows[:10]
        if len(r) > max(label_col, value_col)
    ]
    total = sum(v for _, v in pairs)
    if total <= 0:
        return '<p class="muted">All values are zero.</p>'

    import math

    cx, cy, radius = 130, 130, 110
    parts = ['<svg viewBox="0 0 470 260" width="100%" height="260" role="img">']
    angle = -math.pi / 2
    legend: List[str] = []

    for i, (label, value) in enumerate(pairs):
        sweep = 2 * math.pi * (value / total)
        x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        angle += sweep
        x2, y2 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        large = 1 if sweep > math.pi else 0
        colour = PALETTE[i % len(PALETTE)]
        parts.append(
            f'<path d="M {cx} {cy} L {x1:.1f} {y1:.1f} '
            f'A {radius} {radius} 0 {large} 1 {x2:.1f} {y2:.1f} Z" fill="{colour}" />'
        )
        legend.append(
            f'<span class="key"><i style="background:{colour}"></i>'
            f"{_esc(label)} &middot; {value / total * 100:.1f}%</span>"
        )

    parts.append("</svg>")
    parts.append(f'<div class="legend">{" ".join(legend)}</div>')
    return "".join(parts)


def _svg_scatter(columns: Sequence[str], rows: Sequence[Sequence[Any]], chart) -> str:
    """Points, not bars.

    Drawn rather than fudged into a bar chart: this file is what somebody keeps,
    and a chart of a different kind than the one on screen is not a copy of the
    dashboard, it is a different claim about the data.
    """
    label_col, value_cols = _axis_pick(columns, rows, chart)
    if label_col is None or not value_cols:
        return '<p class="muted">Nothing numeric to plot.</p>'

    value_col = value_cols[0]
    points = [
        (_number(r[label_col]), _number(r[value_col]))
        for r in rows[:400]
        if len(r) > max(label_col, value_col)
    ]
    points = [(x, y) for x, y in points if x is not None and y is not None]
    if not points:
        # A categorical x has no position of its own; fall back to the ordering.
        points = [
            (float(i), _number(r[value_col]) or 0.0)
            for i, r in enumerate(rows[:400])
            if len(r) > value_col
        ]
    if not points:
        return '<p class="muted">Nothing numeric to plot.</p>'

    width, height, pad = 640, 260, 34
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    x_lo, x_hi = min(xs), max(xs)
    y_lo, y_hi = min(min(ys), 0.0), max(ys)
    x_span = (x_hi - x_lo) or 1.0
    y_span = (y_hi - y_lo) or 1.0

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">',
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" '
        f'stroke="#cbd5e1" />',
    ]
    for x, y in points:
        cx = pad + (x - x_lo) / x_span * (width - 2 * pad)
        cy = (height - pad) - (y - y_lo) / y_span * (height - 2 * pad)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{PALETTE[0]}" '
                     f'fill-opacity="0.75" />')
    parts.append("</svg>")
    if len(rows) > len(points):
        parts.append(
            f'<p class="muted small">Showing {len(points)} of {len(rows):,} rows.</p>'
        )
    return "".join(parts)


def _svg_heatmap(columns: Sequence[str], rows: Sequence[Sequence[Any]], chart) -> str:
    """A grid of cells: x across, y down, colour by the third column."""
    label_col, value_cols = _axis_pick(columns, rows, chart)
    if label_col is None or not value_cols:
        return '<p class="muted">Nothing numeric to plot.</p>'

    # x, y, value. With only one non-numeric column there is no second axis, so
    # this degrades to the bar chart rather than inventing one.
    y_name = getattr(chart, "color_by", None)
    y_col = columns.index(y_name) if y_name in columns else None
    if y_col is None:
        others = [i for i, _ in enumerate(columns) if i != label_col and i not in value_cols]
        y_col = others[0] if others else None
    if y_col is None:
        return _svg_bar_or_line(columns, rows, chart, line=False)

    value_col = value_cols[0]
    xs, ys, cells = [], [], {}
    for row in rows[:600]:
        if len(row) <= max(label_col, y_col, value_col):
            continue
        x, y = str(row[label_col]), str(row[y_col])
        if x not in xs:
            xs.append(x)
        if y not in ys:
            ys.append(y)
        cells[(x, y)] = _number(row[value_col]) or 0.0
    if not cells:
        return '<p class="muted">Nothing numeric to plot.</p>'

    xs, ys = xs[:24], ys[:16]
    top = max(cells.values()) or 1.0
    cell_w, cell_h, left, top_pad = 26, 18, 120, 10
    width = left + cell_w * len(xs) + 10
    height = top_pad + cell_h * len(ys) + 26

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img">']
    for row_index, y in enumerate(ys):
        parts.append(
            f'<text x="{left - 6}" y="{top_pad + row_index * cell_h + 13}" '
            f'text-anchor="end" font-size="11" fill="#64748b">{_esc(y[:18])}</text>'
        )
        for col_index, x in enumerate(xs):
            value = cells.get((x, y))
            shade = 0.08 + 0.92 * (value / top) if value else 0.06
            parts.append(
                f'<rect x="{left + col_index * cell_w}" '
                f'y="{top_pad + row_index * cell_h}" width="{cell_w - 2}" '
                f'height="{cell_h - 2}" rx="2" fill="{PALETTE[0]}" '
                f'fill-opacity="{shade:.2f}" />'
            )
    for col_index, x in enumerate(xs):
        parts.append(
            f'<text x="{left + col_index * cell_w + cell_w / 2}" y="{height - 8}" '
            f'text-anchor="middle" font-size="10" fill="#64748b">{_esc(x[:6])}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _render_chart(tile: Tile, result: TileResult) -> str:
    chart = tile.chart
    kind = getattr(chart, "type", None)
    if kind == ChartType.PIE:
        return _svg_pie(result.columns, result.rows, chart)
    if kind == ChartType.SCATTER:
        return _svg_scatter(result.columns, result.rows, chart)
    if kind == ChartType.HEATMAP:
        return _svg_heatmap(result.columns, result.rows, chart)
    line = kind in (ChartType.LINE, ChartType.AREA)
    return _svg_bar_or_line(result.columns, result.rows, chart, line=line)


# ----------------------------------------------------------------------
# Tiles
# ----------------------------------------------------------------------


def _render_table(result: TileResult, limit: int = 200) -> str:
    if not result.columns:
        return '<p class="muted">No columns.</p>'
    head = "".join(f"<th>{_esc(c)}</th>" for c in result.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
        for row in result.rows[:limit]
    )
    more = (
        f'<p class="muted small">Showing {limit:,} of {result.row_count:,} rows.</p>'
        if result.row_count > limit
        else ""
    )
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>{more}"
    )


def _render_metric(result: TileResult) -> str:
    """One number, large. The most-read tile kind, so it gets the most room."""
    if not result.rows or not result.rows[0]:
        return '<p class="muted">No value.</p>'
    value = result.rows[0][0]
    numeric = _number(value)
    shown = f"{numeric:,.10g}" if numeric is not None else str(value)
    label = result.columns[0] if result.columns else ""
    return f'<p class="metric">{_esc(shown)}</p><p class="muted">{_esc(label)}</p>'


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

    if result is None:
        body = '<p class="muted">This tile was not executed.</p>'
    elif result.error:
        body = f'<p class="error">{_esc(result.error)}</p>'
    elif tile.kind == TileKind.METRIC:
        body = _render_metric(result)
    elif tile.kind == TileKind.CHART:
        body = _render_chart(tile, result)
    else:
        body = _render_table(result)

    warnings = "".join(
        f'<p class="warn">{_esc(w)}</p>' for w in (result.warnings if result else [])
    )
    truncated = (
        '<p class="warn">The result was truncated by the row limit, so totals '
        "here may be incomplete.</p>"
        if result is not None and result.truncated
        else ""
    )
    return (
        f'<section class="tile"><h2>{title}</h2>{description}'
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
  --bg:#0b1120; --card:#111827; --ink:#e5e7eb; --muted:#94a3b8; --line:#1f2937; } }
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
.metric { font-size:2.4rem; font-weight:650; margin:6px 0 0; }
.muted { color:var(--muted); } .small { font-size:.8125rem; }
.warn { color:var(--warn); font-size:.8125rem; margin:0 0 8px; }
.error { color:var(--bad); font-size:.875rem; margin:0; }
.text-tile { white-space:pre-wrap; margin:0; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:.8125rem;
  font-variant-numeric:tabular-nums; direction:ltr; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
  white-space:nowrap; }
th { color:var(--muted); font-weight:600; }
tbody tr:last-child td { border-bottom:0; }
.grid { stroke:var(--line); stroke-width:1; }
.axis { stroke:var(--muted); stroke-width:1; }
.ax { fill:var(--muted); font-size:11px; }
.legend { margin-top:8px; font-size:.8125rem; color:var(--muted); }
.key { margin-right:14px; white-space:nowrap; }
.key i { display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:5px; vertical-align:baseline; }
footer { color:var(--muted); font-size:.75rem; margin-top:26px;
  padding-top:14px; border-top:1px solid var(--line); }
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
    """
    by_id: Dict[str, TileResult] = {r.tile_id: r for r in results}
    when = (exported_at or datetime.now(timezone.utc)).strftime("%d %B %Y at %H:%M UTC")

    tiles = "".join(_render_tile(tile, by_id.get(tile.id)) for tile in dashboard.tiles)

    provenance_bits = [f"Snapshot taken {_esc(when)}"]
    if exported_by:
        provenance_bits.append(f"by {_esc(exported_by)}")
    if workspace:
        provenance_bits.append(f"in {_esc(workspace)}")
    provenance = ", ".join(provenance_bits) + "."

    source_line = (
        f" Data source: {_esc(data_source)}." if data_source else ""
    )

    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(dashboard.title or 'Dashboard')}</title>
<style>{_CSS}</style>
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
</html>
"""


def export_filename(dashboard: Dashboard, exported_at: Optional[datetime] = None) -> str:
    """A safe, dated filename. Never derived unescaped from a user-supplied title."""
    stamp = (exported_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    slug = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in (dashboard.title or "dashboard")
    ).strip("-").lower() or "dashboard"
    return f"{slug[:60]}-{stamp}.html"
