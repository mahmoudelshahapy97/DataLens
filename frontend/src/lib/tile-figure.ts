/**
 * The one piece of code that decides what a tile looks like.
 *
 * ## Why the import reaches outside `src/`
 *
 * `tile-figure.js` lives at `frontend/public/assets/shared/` and is *mirrored*
 * into `backend/vanna/dashboards/vendor/tile-figure.js`, which the offline
 * export inlines. `backend/tests/test_dashboard_export.py` pins that exact path
 * and fails if the two copies drift -- so moving it into `src/` would either
 * break that test or, worse, require editing it, which is how the two copies
 * quietly diverge and an exported dashboard starts drawing a different chart
 * than the screen it came from.
 *
 * So the file stays where the test expects and this module reaches across to it.
 * Vite bundles it happily; the awkwardness is confined to these two lines and is
 * cheaper than a second figure builder.
 *
 * The export used to draw its own SVG approximations with their own axis-picking
 * rules -- it ignored `sort_by`, `limit` and `color_by`, and drew a bar chart
 * when the tile asked for a heatmap. Sharing the builder is the point.
 */

// @ts-expect-error -- a plain ES module with no type declarations of its own.
import { PALETTE as RAW_PALETTE, tileFigure as rawTileFigure } from '../../public/assets/shared/tile-figure.js';

import type { Tile, TileResult } from '@/types';

export interface Figure {
  traces: unknown[];
  layout: Record<string, unknown>;
  config: Record<string, unknown>;
}

export interface FigureOptions {
  dark?: boolean;
  height?: number;
  /** Label for the bucket a `limit` groups the tail into. */
  otherLabel?: string;
}

/**
 * Build a Plotly figure for one tile.
 *
 * Returns `null` when there is nothing to draw -- no columns or no rows. That is
 * a real state a dashboard reaches (a filter that matches nothing) and the
 * caller renders an empty note for it rather than an error.
 */
export function tileFigure(
  tile: Pick<Tile, 'kind' | 'chart' | 'title'>,
  result: Pick<TileResult, 'columns' | 'rows'> | null,
  options: FigureOptions = {},
): Figure | null {
  return rawTileFigure(tile, result, options) as Figure | null;
}

/** The categorical series palette. Red and green are deliberately not first. */
export const PALETTE = RAW_PALETTE as string[];
