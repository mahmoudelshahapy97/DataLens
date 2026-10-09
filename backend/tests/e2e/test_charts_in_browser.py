"""Dashboard charts, driven the way a person drives them, against the stack.

Every other test in this repository can tell you the figure was *built* right --
that a metric became an `indicator`, that a heatmap tile got a heatmap trace. None
of them can tell you the chart responds to a mouse, because that needs the real
`<plotly-chart>` component, the real Plotly bundle, and a real pointer.

It shipped broken, and this is the file that would have caught it. Plotly injects
its stylesheet into `document.head`, a shadow root does not inherit document
styles, and the one rule that matters is

    .js-plotly-plot .plotly .main-svg { pointer-events: none }

Plotly stacks several `main-svg` elements and re-enables pointer events only on
the drag layer. Without that rule the topmost one covers the plot and swallows
every pointer event, so hover shows nothing and dragging does not zoom -- while
the figure is correct, the numbers are right, and *the modebar buttons still
work*, which is what makes it read as a styling quirk rather than a dead chart.

Two things are asserted, and both are the user's actions rather than the
library's internals: hovering a bar shows its value, and dragging across the plot
changes the axis range.

    docker compose up -d
    VANNA_E2E_URL=http://localhost:3000 VANNA_E2E_PASSWORD=... \\
        pytest tests/e2e/test_charts_in_browser.py -m e2e
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

BASE_URL = os.getenv("VANNA_E2E_URL", "").rstrip("/")
EMAIL = os.getenv("VANNA_E2E_EMAIL", "demo@example.com")
PASSWORD = os.getenv("VANNA_E2E_PASSWORD", "")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not BASE_URL, reason="VANNA_E2E_URL is not set"),
]

#: Finds the first tile holding a bar chart and returns its graph div. Every probe
#: below starts here, so "which chart" is decided in one place.
GRAPH_DIV = """
() => {
  const host = [...document.querySelectorAll('.tile-chart plotly-chart')]
    .find((c) => (c.data || []).some((t) => t.type === 'bar'));
  return host ? host.shadowRoot.querySelector('.plotly-div') : null;
}
"""


@pytest.fixture(scope="module")
def dashboard(browser):
    """A dashboard open, with at least one bar chart drawn."""
    context = browser.new_context(viewport={"width": 1500, "height": 1000})
    page = context.new_page()
    page.goto(f"{BASE_URL}/", wait_until="networkidle")
    page.fill("#si-email", EMAIL)
    page.fill("#si-password", PASSWORD)
    page.click("#si-go")
    page.wait_for_selector("#app.ready", timeout=30_000)
    page.click('button[data-view="dashboards"]')
    page.wait_for_selector("#view-other [data-open]", timeout=30_000)

    opened = page.locator("#view-other [data-open]")
    for index in range(min(8, opened.count())):
        opened.nth(index).click()
        page.wait_for_selector(".tile-grid", timeout=30_000)
        page.wait_for_timeout(3000)
        if page.evaluate(f"() => !!({GRAPH_DIV})()"):
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    else:
        pytest.skip("no dashboard with a bar chart in this deployment")

    yield page
    context.close()


def plot_area(page) -> dict:
    """The drawn plot's box in page coordinates, from Plotly's own axes."""
    return page.evaluate(
        f"""() => {{
        const gd = ({GRAPH_DIV})();
        const fl = gd._fullLayout;
        const box = gd.getBoundingClientRect();
        return {{
          left: box.left + fl.xaxis._offset,
          top: box.top + fl.yaxis._offset,
          width: fl.xaxis._length,
          height: fl.yaxis._length,
        }};
    }}"""
    )


def x_range(page) -> list:
    return page.evaluate(f"() => ({GRAPH_DIV})()._fullLayout.xaxis.range.slice()")


class TestTheChartAnswersTheMouse:
    def test_the_pointer_reaches_the_plot(self, dashboard):
        """The property the bug broke, stated directly: Plotly's own stylesheet
        has to be inside the shadow root, or the plot area is inert."""
        pointer_events = dashboard.evaluate(
            f"""() => [...({GRAPH_DIV})().querySelectorAll('svg.main-svg')]
                      .map((svg) => getComputedStyle(svg).pointerEvents)"""
        )
        assert pointer_events, "no main-svg -- the chart did not draw"
        assert set(pointer_events) == {"none"}, (
            "an overlay svg is taking the pointer events the drag layer needs"
        )

    def test_hovering_a_bar_shows_its_value(self, dashboard):
        """Aimed at the middle of the *tallest* bar, computed through Plotly's
        axes. Aiming at a fraction of the plot box instead lands in the empty
        space above a short bar on any ranked chart, where Plotly correctly shows
        nothing -- a false failure that looks exactly like this bug."""
        target = dashboard.evaluate(
            f"""() => {{
            const gd = ({GRAPH_DIV})();
            const fl = gd._fullLayout;
            const trace = gd.data.find((t) => t.type === 'bar');
            let best = 0;
            trace.y.forEach((v, i) => {{
              if (Math.abs(v) > Math.abs(trace.y[best])) best = i;
            }});
            const box = gd.getBoundingClientRect();
            return {{
              px: box.left + fl.xaxis._offset + fl.xaxis.d2p(trace.x[best]),
              py: box.top + fl.yaxis._offset + fl.yaxis.l2p(trace.y[best] / 2),
              label: String(trace.x[best]),
            }};
        }}"""
        )

        dashboard.mouse.move(target["px"] - 40, target["py"] - 40)
        dashboard.mouse.move(target["px"], target["py"], steps=8)
        dashboard.wait_for_timeout(800)

        labels = dashboard.evaluate(
            f"""() => [...({GRAPH_DIV})().querySelectorAll('.hovertext text')]
                      .map((node) => node.textContent)"""
        )
        assert labels, "hovering a bar produced no tooltip"
        assert any(target["label"][:12] in text for text in labels), labels

    def test_dragging_across_the_plot_zooms_it(self, dashboard):
        """The other half of interactive, and the one a modebar click cannot
        stand in for: the modebar buttons kept working throughout the bug,
        because they are HTML rather than SVG under the overlay."""
        area = plot_area(dashboard)
        before = x_range(dashboard)

        y = area["top"] + area["height"] * 0.5
        left = area["left"] + area["width"] * 0.35
        right = area["left"] + area["width"] * 0.65
        dashboard.mouse.move(left, y)
        dashboard.mouse.down()
        dashboard.mouse.move(right, y, steps=12)
        dashboard.mouse.up()
        dashboard.wait_for_timeout(800)

        zoomed = x_range(dashboard)
        assert zoomed != before, "dragging across the plot did not zoom"
        assert (zoomed[1] - zoomed[0]) < (before[1] - before[0])

        # And double-click puts it back, so a reader cannot get stranded.
        dashboard.mouse.dblclick((left + right) / 2, y)
        dashboard.wait_for_timeout(800)
        assert x_range(dashboard) == pytest.approx(before, rel=0.01)

    def test_the_modebar_is_there_for_the_things_a_drag_cannot_do(self, dashboard):
        buttons = dashboard.evaluate(
            f"""() => [...({GRAPH_DIV})().querySelectorAll('.modebar-btn')]
                      .map((b) => b.getAttribute('data-title'))"""
        )
        assert buttons, "no modebar on a dashboard chart"
        assert any("Download" in (title or "") for title in buttons)
        # Removed on purpose: the notebook-only buttons and the lasso nobody uses.
        assert not any("Lasso" in (title or "") for title in buttons)
