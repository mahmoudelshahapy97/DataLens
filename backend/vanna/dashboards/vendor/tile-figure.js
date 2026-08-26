/**
 * One dashboard tile, as a Plotly figure. The only place that decision is made.
 *
 * There used to be two implementations of "draw this tile": this logic, in the
 * browser, and a hand-written SVG renderer in `backend/vanna/dashboards/export.py`
 * for the exported file. They disagreed -- the SVG one had its own axis-picking
 * rules, ignored `sort_by`, `limit` and `color_by`, and drew a bar chart when the
 * tile asked for a heatmap. So the file somebody kept and circulated showed a
 * different chart than the screen it was taken from, which is the one thing a
 * snapshot must never do.
 *
 * Now the export inlines *this* file and calls the same function, so there is one
 * answer to what a tile looks like. That is why this module has no imports and
 * touches no DOM: it is loaded as an ES module by `app.js` and inlined verbatim
 * into a `file://` document, and the second of those cannot resolve an import or
 * assume a page around it.
 *
 * Every tile kind is a figure, including the ones that are not charts:
 *
 *   chart   bar / line / area / pie / scatter / heatmap, per ChartSpec
 *   metric  an `indicator` -- one number, large
 *   table   a `table` trace
 *
 * `text` is the exception and has no figure; it is prose, and prose is markup.
 *
 * TWO COPIES, DELIBERATELY, AND THEY MUST MATCH BYTE FOR BYTE:
 *
 *   frontend/public/assets/shared/tile-figure.js   the original, loaded by app.js
 *   backend/vanna/dashboards/vendor/tile-figure.js the copy the export inlines
 *
 * The backend image carries no Node, so it cannot build this; it reads the copy at
 * `dashboards/export.py`. `backend/tests/test_dashboard_export.py` asserts the two
 * are identical, so editing one and not the other fails the suite rather than
 * shipping an export that draws a different chart than the screen. To resync after
 * editing the original:  make plotly-bundle
 */

/** Plotly's own palette is fine, but the first two colours must not be red/green. */
export const PALETTE = [
  '#4f46e5', '#ea7317', '#059669', '#dc2626',
  '#0891b2', '#7c3aed', '#a16207', '#db2777',
];

/** Coerce a cell to a number, or null. Dates, labels and NULLs all land here. */
export function asNumber(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const parsed = Number(String(value).replace(/,/g, '').trim());
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * `{ traces, layout, config }` for a tile, or null when there is nothing to draw.
 *
 * @param tile    The tile definition: `kind`, `chart` (a ChartSpec), `title`.
 * @param result  The executed result: `columns`, `rows`, `row_count`, `truncated`.
 * @param options `{ height, dark, otherLabel }`. The export passes a fixed
 *                height because it has no layout to measure; the browser passes
 *                the tile's own box. `otherLabel` is the gathered-tail slice of a
 *                pie -- the page passes its translation, since this module cannot
 *                reach the dictionary and the export has no locale at all.
 */
export function tileFigure(tile, result, options = {}) {
  const columns = (result && result.columns) || [];
  const rows = (result && result.rows) || [];
  const dark = !!options.dark;
  const height = options.height || 300;
  const otherLabel = options.otherLabel || 'Other';

  if (!columns.length || !rows.length) return null;

  if (tile.kind === 'metric') return metricFigure(columns, rows, { dark, height });
  if (tile.kind === 'table') {
    return tableFigure(columns, rows, result, { dark, height });
  }
  return chartFigure(tile, columns, rows, { dark, height, otherLabel });
}

// ----------------------------------------------------------------------
// Metric
// ----------------------------------------------------------------------

/**
 * One number, as an `indicator`.
 *
 * The *last* column of the first row, not the first: a metric query is commonly
 * `SELECT 'Revenue' AS label, SUM(total)`, and reading column zero shows the
 * label where the number should be.
 */
function metricFigure(columns, rows, { dark, height }) {
  const row = rows[0] || [];
  const at = row.length - 1;
  const value = asNumber(row[at]);
  const label = columns[at] || columns[0] || '';

  if (value === null) {
    // Not every metric is numeric -- "most recent invoice: 2024-03-01" is a
    // legitimate one. An indicator cannot show that, so it becomes a table of
    // one cell rather than a chart of nothing.
    return tableFigure(columns, rows, { columns, rows }, { dark, height });
  }

  return {
    traces: [{
      type: 'indicator',
      mode: 'number',
      value,
      number: {
        font: { size: Math.max(28, Math.min(56, Math.round(height / 3.2))) },
        // Plotly's default rounds to whole numbers, so an average order value of
        // 450.75 rendered as "451" -- a metric quietly showing a different number
        // than the query returned. Ten significant digits with the trailing zeros
        // trimmed matches what the plain-HTML metric used to print.
        valueformat: ',.10~r',
      },
      title: { text: label, font: { size: 13 } },
    }],
    layout: baseLayout({ dark, height, circular: true, showlegend: false }),
    config: chromeless(),
  };
}

// ----------------------------------------------------------------------
// Table
// ----------------------------------------------------------------------

/** Rows as a `table` trace. Capped, because a tile is not a scrollable grid. */
function tableFigure(columns, rows, result, { dark, height }) {
  const limit = 200;
  const shown = rows.slice(0, limit);
  const line = dark ? '#1f2937' : '#e2e8f0';
  const ink = dark ? '#e5e7eb' : '#0f172a';
  const muted = dark ? '#94a3b8' : '#64748b';

  return {
    traces: [{
      type: 'table',
      header: {
        values: columns.map((name) => `<b>${String(name)}</b>`),
        align: 'left',
        height: 26,
        fill: { color: 'rgba(0,0,0,0)' },
        font: { color: muted, size: 12 },
        line: { color: line, width: 1 },
      },
      cells: {
        // Column-major: Plotly's table takes a column per array, and handing it
        // rows silently transposes the data into nonsense.
        values: columns.map((_, index) => shown.map((row) => cell(row[index]))),
        align: 'left',
        height: 24,
        fill: { color: 'rgba(0,0,0,0)' },
        font: { color: ink, size: 12 },
        line: { color: line, width: 1 },
      },
    }],
    layout: baseLayout({ dark, height, circular: true, showlegend: false }),
    config: chromeless(),
    // The tile prints this under the figure. Reported rather than silently
    // dropped: a table showing 200 of 40,000 rows without saying so is a
    // sample presented as an answer.
    note: noteFor(result, shown.length),
  };
}

function cell(value) {
  if (value === null || value === undefined) return '';
  return String(value);
}

function noteFor(result, shownCount) {
  const total = (result && result.row_count) || 0;
  const parts = [];
  if (total > shownCount) parts.push(`Showing ${shownCount} of ${total} rows`);
  else if (total) parts.push(`${total} rows`);
  if (result && result.truncated) parts.push('capped by the row limit');
  return parts.join(', ');
}

// ----------------------------------------------------------------------
// Charts
// ----------------------------------------------------------------------

/**
 * A chart tile, honouring its ChartSpec.
 *
 * The spec is what the agent chose; the fallbacks here only apply where it said
 * nothing, which mirrors how the backend treats it.
 */
function chartFigure(tile, columns, rows, { dark, height, otherLabel }) {
  const spec = tile.chart || {};
  const column = (name) => columns.indexOf(name);

  const xName = spec.x && column(spec.x) >= 0 ? spec.x : columns[0];
  const yNames = (spec.y || []).filter((name) => column(name) >= 0);
  const colourName = spec.color_by && column(spec.color_by) >= 0 ? spec.color_by : null;
  const series = yNames.length
    ? yNames
    : columns.filter((c) => c !== xName && c !== colourName);

  // `sort_by`, `descending` and `limit` were declared on ChartSpec from the start
  // and read by nothing, so a tile asking for the top ten by revenue got every row
  // in whatever order the warehouse returned -- the same failure as the heatmap
  // branch below: a chart that looks like an answer.
  let ordered = rows;
  const sortName = spec.sort_by && column(spec.sort_by) >= 0 ? spec.sort_by : null;
  if (sortName) {
    const at = column(sortName);
    ordered = rows.slice().sort((left, right) => {
      const a = left[at], b = right[at];
      const numeric = asNumber(a), other = asNumber(b);
      const cmp = numeric !== null && other !== null
        ? numeric - other
        : String(a === null || a === undefined ? '' : a)
            .localeCompare(String(b === null || b === undefined ? '' : b));
      return spec.descending ? -cmp : cmp;
    });
  }

  // Top-N. Whether the remainder is gathered or dropped depends on what the chart
  // claims: a pie asserts that its slices are the whole, so dropping the tail there
  // makes the parts stop summing to it. A ranked bar chart claims no such thing --
  // and gathering the tail into one bar is actively wrong when the measure is not
  // additive. Summing the minutes of 3,488 remaining tracks produced an "Other" bar
  // of 3,000 next to fifteen bars of nine, which answers a question nobody asked.
  const type = spec.type || 'bar';
  const wholeOfParts = type === 'pie';
  if (spec.limit && spec.limit > 0 && ordered.length > spec.limit && !colourName) {
    const kept = ordered.slice(0, spec.limit);
    const rest = ordered.slice(spec.limit);
    if (rest.length && wholeOfParts) {
      kept.push(columns.map((name, index) => {
        if (index === column(xName)) return otherLabel;
        const total = rest.reduce((sum, row) => {
          const value = asNumber(row[index]);
          return value === null ? sum : sum + value;
        }, 0);
        return total || null;
      }));
    }
    ordered = kept;
  }
  const data = ordered;
  const x = data.map((row) => row[column(xName)]);

  // `heatmap` has been in `ChartType` since the spec was written and had no branch
  // in either renderer, so a tile asking for one silently got a bar chart.
  if (type === 'heatmap') {
    const yName = series[0];
    const valueName = series[1] || series[0];
    const xs = [...new Set(data.map((row) => row[column(xName)]))];
    const ys = [...new Set(data.map((row) => row[column(yName)]))];
    const grid = ys.map((yValue) => xs.map((xValue) => {
      const hit = data.find(
        (row) => row[column(xName)] === xValue && row[column(yName)] === yValue
      );
      return hit ? asNumber(hit[column(valueName)]) : null;
    }));
    return assemble(
      [{ type: 'heatmap', x: xs, y: ys, z: grid, colorscale: 'Blues' }],
      { spec, xName, yName: spec.y_label || yName, seriesCount: 1, dark, height },
    );
  }

  const shape = (name, xs, ys, index) => {
    const colour = PALETTE[index % PALETTE.length];
    if (type === 'pie') {
      return { type: 'pie', labels: xs, values: ys, name,
               marker: { colors: PALETTE } };
    }
    if (type === 'scatter') {
      return { type: 'scatter', mode: 'markers', x: xs, y: ys, name,
               marker: { color: colour } };
    }
    if (type === 'line' || type === 'area') {
      return {
        type: 'scatter', mode: 'lines+markers', x: xs, y: ys, name,
        line: { color: colour },
        marker: { color: colour },
        fill: type === 'area' ? 'tozeroy' : undefined,
      };
    }
    return { type: 'bar', x: xs, y: ys, name, marker: { color: colour } };
  };

  // `color_by` splits one measure into a trace per distinct value -- which is what
  // a legend *is*. Without it a "revenue by month, by country" tile drew a single
  // line and the country column was silently ignored.
  if (colourName) {
    const at = column(colourName);
    const measure = series[0];
    const groups = new Map();
    data.forEach((row) => {
      const key = String(row[at] === null || row[at] === undefined ? '' : row[at]);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    });
    let entries = [...groups.entries()];
    if (spec.limit && spec.limit > 0 && entries.length > spec.limit) {
      entries = entries.slice(0, spec.limit);  // too many series is unreadable, not wrong
    }
    const traces = entries.map(([key, group], index) => shape(
      key,
      group.map((row) => row[column(xName)]),
      group.map((row) => row[column(measure)]),
      index,
    ));
    return assemble(traces, {
      spec, xName, yName: spec.y_label || measure,
      seriesCount: traces.length, dark, height,
    });
  }

  const traces = series.map((name, index) => shape(
    name, x, data.map((row) => row[column(name)]), index,
  ));
  return assemble(traces, {
    spec, xName, yName: series.length === 1 ? series[0] : '',
    seriesCount: series.length, dark, height,
  });
}

function assemble(traces, { spec, xName, yName, seriesCount, dark, height }) {
  // A pie carries its own labels; axis titles on one are noise.
  const circular = traces.some((trace) => trace.type === 'pie');
  const layout = baseLayout({
    dark, height, circular,
    showlegend: seriesCount > 1 || circular,
  });
  layout.barmode = spec.stacked ? 'stack' : 'group';
  // Omitted, not set to `undefined`: Plotly's `cleanLayout` walks whatever axis
  // keys are present and dereferences them, so an explicit `xaxis: undefined`
  // throws before anything is drawn -- every pie tile rendered as an empty box
  // with a console error behind it.
  if (!circular) {
    layout.xaxis = { title: spec.x_label || xName };
    layout.yaxis = { title: spec.y_label || yName };
  }
  return { traces, layout, config: interactive() };
}

// ----------------------------------------------------------------------
// Layout and chrome
// ----------------------------------------------------------------------

function baseLayout({ dark, height, circular, showlegend }) {
  return {
    height,
    // Top margin leaves the modebar somewhere to sit. At t:10 it had to overlay the
    // plot, so hovering a tile hid the top of the very series being inspected.
    // A pie, an indicator and a table have no axes, so they get their space back.
    margin: circular
      ? { t: 28, r: 10, b: 10, l: 10 }
      : { t: 28, r: 10, b: 40, l: 56 },
    modebar: {
      // Stacked vertically against the corner and with no background of its own.
      // Plotly's default is a horizontal bar with an opaque fill, which spans the
      // full width of the plot and sits on top of the tallest bar in the chart --
      // exactly the one being looked at.
      orientation: 'v',
      bgcolor: 'rgba(0,0,0,0)',
      color: dark ? '#9aa4b2' : '#6b7280',
      activecolor: dark ? '#e5e7eb' : '#111827',
    },
    showlegend,
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: dark ? '#e5e7eb' : '#0f172a' },
    colorway: PALETTE,
  };
}

/**
 * The toolbar, for figures somebody explores.
 *
 * `<plotly-chart>` defaults to `displayModeBar: false`, which is right for the
 * chat -- an answer with a toolbar on it looks like a control panel. A dashboard
 * tile is the opposite case: the whole point of looking at it is to zoom into a
 * spike and read the numbers off.
 */
function interactive() {
  return {
    displayModeBar: 'hover',
    displaylogo: false,
    scrollZoom: true,
    // The buttons that only make sense in a notebook, and the lasso nobody uses.
    modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'],
    toImageButtonOptions: { format: 'png', scale: 2 },
    responsive: true,
  };
}

/** No toolbar: there is nothing to zoom into on a number or a table. */
function chromeless() {
  return { displayModeBar: false, displaylogo: false, responsive: true };
}
