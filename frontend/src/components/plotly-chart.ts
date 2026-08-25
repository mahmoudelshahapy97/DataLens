import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { vannaDesignTokens } from '../styles/vanna-design-tokens.js';
/**
 * Plotly is loaded on first use, not at import time.
 *
 * `plotly.js-dist-min` is several megabytes and was in the main bundle, so every
 * user downloaded a full charting library on page load whether or not a chart ever
 * appeared -- and most conversations never produce one. A dynamic import moves that
 * cost to the first chart, where it is at least paying for something.
 *
 * The promise is memoised rather than the module: two charts rendering in the same
 * tick would otherwise each start their own download.
 */
type PlotlyModule = typeof import('plotly.js-dist-min');

let plotlyPromise: Promise<PlotlyModule> | null = null;

function loadPlotly(): Promise<PlotlyModule> {
  if (!plotlyPromise) {
    plotlyPromise = import('plotly.js-dist-min').then((m) => (m as any).default ?? m);
  }
  return plotlyPromise;
}

export interface PlotlyData {
  x?: any[];
  y?: any[];
  type?: any;
  mode?: any;
  name?: string;
  marker?: any;
  line?: any;
  [key: string]: any;
}

export interface PlotlyLayout {
  title?: any;
  xaxis?: any;
  yaxis?: any;
  font?: any;
  paper_bgcolor?: string;
  plot_bgcolor?: string;
  margin?: any;
  showlegend?: boolean;
  height?: number;
  width?: number;
  modebar?: any;
  [key: string]: any;
}

@customElement('plotly-chart')
export class PlotlyChart extends LitElement {
  static styles = [
    vannaDesignTokens,
    css`
      :host {
        display: block;
        font-family: var(--vanna-font-family-default);
        width: 100%;
        height: 100%;
      }

      .plotly-div {
        width: 100%;
        /* 400px is the right default for a chat answer, and the wrong one for a
           dashboard tile: min-height beats an explicit height, so a tile 312px
           tall drew its plot inside a 400px box and overflowed its own card. The
           host overrides the variable when it knows the height it wants. */
        min-height: var(--plotly-min-height, 400px);
      }

      /* Plotly layering fix for Shadow DOM */
      .plotly-div,
      .plotly-div .js-plotly-plot,
      .plotly-div .plot-container,
      .plotly-div .svg-container {
        position: relative;
        width: 100%;
        height: 100%;
      }

      .plotly-div svg.main-svg {
        position: absolute;
        top: 0;
        left: 0;
      }

      .plotly-div .hoverlayer {
        pointer-events: none;
      }

      .error-message {
        padding: var(--vanna-space-4);
        color: var(--vanna-accent-negative-default);
        text-align: center;
        font-style: italic;
      }

      .loading-message {
        padding: var(--vanna-space-4);
        color: var(--vanna-foreground-dimmer);
        text-align: center;
        font-style: italic;
      }
    `
  ];

  @property({ type: Array }) data: PlotlyData[] = [];
  @property({ type: Object }) layout: PlotlyLayout = {};
  @property({ type: Object }) config = {};
  @property({ type: Boolean }) loading = false;
  @property() error = '';
  @property() theme: 'light' | 'dark' = 'dark';

  private plotlyDiv?: HTMLElement;
  private resizeObserver?: ResizeObserver;

  firstUpdated() {
    this.plotlyDiv = this.shadowRoot?.querySelector('.plotly-div') as HTMLElement;
    this._renderChart();
    this._setupResizeObserver();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this.resizeObserver?.disconnect();
  }

  private _setupResizeObserver() {
    if (!this.plotlyDiv) return;

    this.resizeObserver = new ResizeObserver(() => {
      if (this.plotlyDiv && this.data.length > 0 && plotlyPromise) {
        const width = this.plotlyDiv.offsetWidth;
        // Already resolved by the time a chart exists to resize; awaited rather
        // than assumed so a resize can never race the first load.
        plotlyPromise.then((Plotly) => Plotly.relayout(this.plotlyDiv!, { width }));
      }
    });

    this.resizeObserver.observe(this.plotlyDiv);
  }

  updated(changedProperties: Map<string | number | symbol, unknown>) {
    if (changedProperties.has('data') || changedProperties.has('layout') || changedProperties.has('theme')) {
      this._renderChart();
    }
  }

  private _getDefaultLayout(): PlotlyLayout {
    const isDark = this.theme === 'dark';

    // Start with layout from backend (which may include white background)
    const mergedLayout = {
      ...this.layout,
      // Only add font/modebar if not already set by backend
      font: this.layout.font || {
        family: 'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        color: isDark ? 'rgb(242, 244, 247)' : 'rgb(17, 24, 39)',
        size: 12
      },
      modebar: this.layout.modebar || {
        bgcolor: isDark ? 'rgba(21, 26, 38, 0.8)' : 'rgba(255, 255, 255, 0.8)',
        color: isDark ? 'rgb(177, 186, 196)' : 'rgb(75, 85, 99)',
        activecolor: isDark ? 'rgb(242, 244, 247)' : 'rgb(17, 24, 39)',
        orientation: 'h'
      },
      // Set explicit dimensions for Shadow DOM compatibility
      autosize: false,
      width: this.layout.width || undefined,
      height: this.layout.height || 400,
    };

    // If backend didn't set background colors, use transparent
    if (!this.layout.paper_bgcolor) {
      mergedLayout.paper_bgcolor = 'transparent';
    }
    if (!this.layout.plot_bgcolor) {
      mergedLayout.plot_bgcolor = 'transparent';
    }

    return mergedLayout;
  }

  private _getDefaultConfig() {
    return {
      responsive: true,
      displayModeBar: false,
      ...this.config
    };
  }

  private async _renderChart() {
    if (!this.plotlyDiv || this.loading || this.error || this.data.length === 0) {
      return;
    }

    try {
      const layout = this._getDefaultLayout();
      const config = this._getDefaultConfig();

      // Let an explicit height win over the 400px floor above.
      if (this.layout.height) {
        this.plotlyDiv.style.setProperty(
          '--plotly-min-height', `${this.layout.height}px`
        );
      }

      const Plotly = await loadPlotly();
      await Plotly.newPlot(this.plotlyDiv, this.data, layout, config);
      this._adoptPlotlyStyles();
    } catch (err) {
      this.error = err instanceof Error ? err.message : 'Failed to render chart';
      console.error('Plotly chart error:', err);
    }
  }

  /**
   * Copy Plotly's own stylesheet into this shadow root.
   *
   * Without this the plot area is *inert*: hovering a bar shows no tooltip and
   * dragging does not zoom, while the figure itself is perfectly correct and the
   * modebar buttons work. That last part is what makes it so confusing to look
   * at -- the toolbar responds, so the chart appears alive.
   *
   * Plotly injects its CSS into `document.head` at first use, and a shadow root
   * does not inherit document styles. The one rule everything depends on is
   *
   *     .js-plotly-plot .plotly .main-svg { pointer-events: none }
   *
   * because Plotly stacks several `main-svg` elements and re-enables pointer
   * events only on the pieces that need them -- `.draglayer { pointer-events:
   * all }`. Without it the topmost `main-svg` covers the plot and swallows every
   * pointer event before it reaches the drag layer underneath.
   *
   * **The rules have to be read through the CSSOM, not copied off the element.**
   * Plotly builds its stylesheet with `insertRule`, so the `<style>` element's
   * `textContent` is the empty string -- cloning the node clones nothing, which
   * looks like it works and changes not one thing.
   */
  private _adoptPlotlyStyles() {
    const root = this.shadowRoot;
    if (!root) return;

    document
      .querySelectorAll<HTMLStyleElement>('style[id^="plotly.js-style"]')
      .forEach((source) => {
        const marker = source.id;
        const existing = root.querySelector<HTMLStyleElement>(
          `style[data-plotly-style="${marker}"]`
        );

        let cssText = '';
        try {
          const sheet = source.sheet;
          // Same-origin, so `cssRules` is readable. Guarded anyway: a browser
          // that refuses should leave the chart drawn rather than throwing out
          // of `_renderChart`.
          cssText = sheet
            ? Array.from(sheet.cssRules, (rule) => rule.cssText).join('\n')
            : source.textContent || '';
        } catch {
          cssText = source.textContent || '';
        }
        if (!cssText) return;

        // Plotly adds per-plot rules as more charts appear, so an adopted copy
        // can go stale. Rewriting is cheap and idempotent.
        const target = existing ?? document.createElement('style');
        if (target.textContent !== cssText) target.textContent = cssText;
        if (!existing) {
          target.setAttribute('data-plotly-style', marker);
          root.appendChild(target);
        }
      });
  }

  render() {
    return html`
      ${this.loading ? html`
        <div class="loading-message">Loading chart...</div>
      ` : this.error ? html`
        <div class="error-message">Error: ${this.error}</div>
      ` : html`
        <div class="plotly-div"></div>
      `}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'plotly-chart': PlotlyChart;
  }
}