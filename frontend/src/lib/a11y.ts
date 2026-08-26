/**
 * Accessibility primitives that React does not provide.
 *
 * `trapFocus` from the vanilla core.js is deliberately *not* here: Radix's Dialog
 * (which every sheet and modal in this app is built on) implements the trap, the
 * `aria-modal` semantics and the focus restore correctly. Two implementations of a
 * focus trap is how one of them rots.
 *
 * What remains is the part no component library covers: announcing to a live region
 * that lives outside the React tree, and roving tabindex across a tablist.
 */

/**
 * Announce something to assistive technology.
 *
 * Answers stream in by mutating the DOM, which a screen reader does not report
 * unless a region says it should. Without this a blind user gets silence for
 * however long the model takes and no signal that it finished.
 *
 * `polite` waits for a pause in speech; `assertive` interrupts and is reserved for
 * errors, where interrupting is the point.
 *
 * The two regions live in index.html rather than in a component, so an announcement
 * during an error boundary's fallback still has somewhere to go.
 */
export function announce(message: string, priority: 'polite' | 'assertive' = 'polite'): void {
  const id = priority === 'assertive' ? 'a11y-alerts' : 'a11y-status';
  const region = document.getElementById(id);
  if (!region) return;
  // Clearing first forces a re-announcement when the same text repeats, which
  // otherwise reads as nothing having happened.
  region.textContent = '';
  window.setTimeout(() => {
    region.textContent = message;
  }, 50);
}

/**
 * Arrow-key movement across a group of controls, as WAI-ARIA expects of a tablist.
 *
 * A row of buttons is not a tablist to a screen reader unless it behaves like one:
 * one stop in the tab order, arrows to move within it.
 *
 * Returned as a keydown handler rather than an effect that attaches a listener, so
 * it composes with JSX: `<div role="tablist" onKeyDown={roveFocus('[role=tab]')}>`.
 */
export function roveFocus(selector: string) {
  return (event: React.KeyboardEvent<HTMLElement>): void => {
    const keys = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'];
    if (!keys.includes(event.key)) return;

    const container = event.currentTarget;
    const items = Array.from(container.querySelectorAll<HTMLElement>(selector));
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (index < 0) return;

    event.preventDefault();

    // Right and left swap meaning when the interface is mirrored. Reading the
    // computed direction rather than the locale keeps this correct inside any
    // subtree that opts back out to ltr.
    const rtl = getComputedStyle(container).direction === 'rtl';
    const forward =
      event.key === 'ArrowDown' ||
      (event.key === 'ArrowRight' && !rtl) ||
      (event.key === 'ArrowLeft' && rtl);

    let next = index;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else next = forward ? (index + 1) % items.length : (index - 1 + items.length) % items.length;

    items.forEach((item, i) => item.setAttribute('tabindex', i === next ? '0' : '-1'));
    items[next].focus();
  };
}
