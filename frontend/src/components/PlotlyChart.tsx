import * as React from 'react';

import { cn } from '@/lib/utils';

// Registers <plotly-chart>. The Lit element stays: it carries
// `_adoptPlotlyStyles()`, which copies Plotly's stylesheet into the shadow root
// so `.main-svg { pointer-events: none }` exists there. Without it the topmost
// overlay swallows every pointer event -- the chart draws correctly and the
// modebar still works, so it reads as a styling quirk rather than a dead plot.
// `tests/e2e/test_charts_in_browser.py` hovers a bar and drags to zoom, and is
// the only test that catches it.
import './plotly-chart';

// React 19 removed the *global* JSX namespace; augmenting `react`'s own is the
// supported way to teach it about a custom element. `HTMLElementTagNameMap` is
// already declared by plotly-chart.ts, so this only adds the JSX half.
declare module 'react' {
  namespace JSX {
    interface IntrinsicElements {
      'plotly-chart': React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
    }
  }
}

export interface PlotlyFigure {
  traces: unknown[];
  layout: Record<string, unknown>;
  config: Record<string, unknown>;
}

/** The shape of the Lit element, as far as this file needs to know. */
type ChartElement = HTMLElement & {
  data: unknown[];
  layout: Record<string, unknown>;
  config: Record<string, unknown>;
  theme: 'light' | 'dark';
};

export interface PlotlyChartProps {
  figure: PlotlyFigure;
  theme?: 'light' | 'dark';
  className?: string;
  /** Announced to assistive technology; a chart is otherwise silent. */
  label?: string;
}

/**
 * A Plotly figure, as React sees it.
 *
 * The whole job of this file is *properties, not attributes*. React sets a
 * primitive prop on a custom element as an attribute, which stringifies it -- so
 * `data={[...]}` would arrive at the element as the literal `[object Object]`.
 * Lit reads `.data`, `.layout`, `.config` and `.theme` as properties, so they are
 * assigned through a ref instead. Doing it explicitly rather than relying on
 * React 19's custom-element handling keeps this working the same way under a
 * future React, and makes the reason visible at the point it matters.
 *
 * The figure itself is never built here. `tileFigure()` in
 * assets/shared/tile-figure.js is the one piece of code that decides what a tile
 * looks like, and it is mirrored into the backend so an offline export draws the
 * same chart as the screen it came from. A second figure builder in React is
 * exactly the divergence that module exists to prevent.
 *
 * The wrapper div carries the layout classes. The custom element itself takes
 * only a size, because Plotly measures its own container and a container that
 * also has padding measures the wrong thing.
 */
export const PlotlyChart = React.forwardRef<HTMLElement, PlotlyChartProps>(
  ({ figure, theme = 'light', className, label }, forwarded) => {
    const inner = React.useRef<ChartElement | null>(null);

    React.useImperativeHandle(forwarded, () => inner.current as HTMLElement);

    React.useEffect(() => {
      const element = inner.current;
      if (!element) return;
      element.data = figure.traces;
      element.layout = figure.layout;
      element.config = figure.config;
      element.theme = theme;
    }, [figure, theme]);

    return (
      <div className={cn('chart w-full min-w-0', className)}>
        <plotly-chart
          ref={inner as React.Ref<HTMLElement>}
          role="img"
          aria-label={label}
          style={{ display: 'block', width: '100%', height: '100%' }}
        />
      </div>
    );
  },
);
PlotlyChart.displayName = 'PlotlyChart';
