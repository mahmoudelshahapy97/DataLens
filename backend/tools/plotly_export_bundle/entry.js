/**
 * The Plotly bundle that gets embedded in an exported dashboard.
 *
 * An export is one file, opened from a `file://` path, on a laptop with no
 * network and no account. That rules out a CDN tag -- a dashboard opened on a
 * plane would show empty boxes -- so Plotly has to be *inside* the file, and its
 * size is the file's size.
 *
 * `plotly.js-dist-min`, which the application uses, is 4.85 MB: every trace type
 * including 3D, maps and WebGL. An exported dashboard uses six. Registering only
 * those against `plotly.js/lib/core` gives 1.2 MB, which is a file people can
 * still email.
 *
 * The six are not a guess -- they are what the tile kinds can produce:
 *
 *   scatter    line, area and scatter tiles     (in core)
 *   bar        the most common chart tile by far
 *   pie        share-of-total tiles
 *   heatmap    ChartType.HEATMAP
 *   indicator  metric tiles: one number, large
 *   table      table tiles
 *
 * Adding a ChartType means adding its module here and rebuilding, and
 * `tests/test_dashboard_export.py` fails if a chart type has no trace behind it
 * -- so the two cannot drift silently.
 *
 * Rebuild with `make plotly-bundle`. The output is committed because the backend
 * image has no Node in it, and an export must not depend on a build step that
 * happens somewhere else.
 */

import Plotly from 'plotly.js/lib/core';

import bar from 'plotly.js/lib/bar';
import heatmap from 'plotly.js/lib/heatmap';
import indicator from 'plotly.js/lib/indicator';
import pie from 'plotly.js/lib/pie';
import table from 'plotly.js/lib/table';

Plotly.register([bar, pie, heatmap, indicator, table]);

// The export's own script calls `Plotly.newPlot`, so it has to be reachable as a
// global. The IIFE format esbuild produces would otherwise keep it private.
window.Plotly = Plotly;
