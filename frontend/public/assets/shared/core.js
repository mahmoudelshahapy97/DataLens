/**
 * Shared front-end core: escaping, fetch, accessibility primitives.
 *
 * The workspace page and the admin console each had their own copy of `esc()`,
 * `api()`, `errorText()`, the theme toggle and the identity handling. Two copies of
 * a security-relevant escape function is one copy too many: they had already begun
 * to diverge, and the one nobody was looking at is the one that would have missed a
 * call site.
 *
 * Loaded as an ES module from /assets/, which is also what makes the Content
 * Security Policy possible -- an inline <script> and a CSP without 'unsafe-inline'
 * cannot coexist, and of the two the CSP is worth more.
 */

// ---------------------------------------------------------------- escaping ---

/**
 * Escape before interpolating into HTML.
 *
 * Questions, SQL, table names and user names all originate from users, an LLM, or a
 * database schema. This is the boundary that keeps any of them from executing as
 * markup in somebody else's browser.
 */
export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** Turn any error payload into one readable line. */
export function errorText(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    // Pydantic validation errors, and our own multi-issue lists.
    return detail.map((item) => errorText(item, '')).filter(Boolean).join('; ') || fallback;
  }

  if (typeof detail === 'object') {
    // Our structured envelope: code / phase / message / hint. The hint is the
    // actionable half, so it is kept.
    const message = detail.message || detail.msg || detail.detail || '';
    const hint = detail.hint ? ` ${detail.hint}` : '';
    if (message) return `${message}${hint}`.trim();
    try { return JSON.stringify(detail); } catch (_) { return fallback; }
  }
  return String(detail);
}

// -------------------------------------------------------------------- csrf ---

/**
 * The CSRF token the server issued, read from its (deliberately readable) cookie.
 *
 * Signed double-submit: the server sets `vanna_csrf` and requires the same value
 * back in a header. An attacker's page can cause the cookie to be *sent* but cannot
 * read it to build the header, which is the whole mechanism.
 */
export function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)vanna_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

// --------------------------------------------------------------------- api ---

/** Extra headers a page wants on every request (the workspace, mostly). */
let headerProvider = () => ({});

export function setHeaderProvider(fn) {
  headerProvider = fn;
}

/** Called with (status, detail) when a request fails with 401 or 403. */
let onAuthFailure = null;

export function setAuthFailureHandler(fn) {
  onAuthFailure = fn;
}

export async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = {
    'Content-Type': 'application/json',
    ...headerProvider(),
    ...(options.headers || {}),
  };
  // Only on state-changing verbs: the server does not check the safe ones, and
  // sending it everywhere would put the token in more places than necessary.
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfToken();
    if (token) headers['X-CSRF-Token'] = token;
  }

  const response = await fetch(path, { credentials: 'include', ...options, headers });

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    let body = null;
    try {
      body = await response.json();
      detail = errorText(body && body.detail, detail);
    } catch (_) { /* non-JSON error body; the status is all we have */ }

    if (response.status === 404) {
      detail = detail === 'Not found' ? 'Not found, or your account lacks access.' : detail;
    }
    if ((response.status === 401 || response.status === 403) && onAuthFailure) {
      onAuthFailure(response.status, body && body.detail);
    }
    const error = new Error(detail);
    error.status = response.status;
    error.detail = body && body.detail;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

// ------------------------------------------------------------------- theme ---

const THEME_KEY = 'vanna.theme';

export function applyTheme(theme) {
  const next = theme || localStorage.getItem(THEME_KEY) || 'light';
  document.documentElement.setAttribute('data-theme', next);
  return next;
}

export function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  localStorage.setItem(THEME_KEY, next);
  return applyTheme(next);
}

// --------------------------------------------------------- accessibility ---

/**
 * Announce something to assistive technology.
 *
 * Streamed answers arrive by mutating the DOM, which a screen reader does not report
 * unless the region says it should. Without this a blind user gets silence for
 * however long the model takes and no signal that it finished -- the single largest
 * accessibility gap in the product.
 *
 * `polite` waits for a pause in speech; `assertive` interrupts and is reserved for
 * errors, where interrupting is the point.
 */
export function announce(message, priority = 'polite') {
  const id = priority === 'assertive' ? 'a11y-alerts' : 'a11y-status';
  const region = document.getElementById(id);
  if (!region) return;
  // Clearing first forces a re-announcement when the same text repeats, which
  // otherwise reads as nothing having happened.
  region.textContent = '';
  window.setTimeout(() => { region.textContent = message; }, 50);
}

/** Elements that can hold focus, in document order. */
function focusable(root) {
  return Array.from(root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((el) => el.offsetParent !== null || el === document.activeElement);
}

/**
 * Trap focus inside a dialog until it closes.
 *
 * Returns a function that releases the trap and restores focus to whatever invoked
 * it. Without both halves, keyboard users tab straight out of an open dialog into
 * the page behind it, and on close are dropped at the top of the document with no
 * idea where they were.
 */
export function trapFocus(container, { onEscape } = {}) {
  const previous = document.activeElement;
  const first = focusable(container)[0];
  if (first) first.focus();

  function onKeyDown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      if (onEscape) onEscape();
      return;
    }
    if (event.key !== 'Tab') return;

    const items = focusable(container);
    if (!items.length) return;
    const start = items[0];
    const end = items[items.length - 1];

    if (event.shiftKey && document.activeElement === start) {
      event.preventDefault();
      end.focus();
    } else if (!event.shiftKey && document.activeElement === end) {
      event.preventDefault();
      start.focus();
    }
  }

  document.addEventListener('keydown', onKeyDown, true);

  return function release() {
    document.removeEventListener('keydown', onKeyDown, true);
    if (previous && typeof previous.focus === 'function') previous.focus();
  };
}

/**
 * Arrow-key movement across a group of controls, as WAI-ARIA expects of a tablist.
 *
 * A row of buttons is not a tablist to a screen reader unless it behaves like one:
 * one stop in the tab order, arrows to move within it.
 */
export function roveFocus(container, selector) {
  container.addEventListener('keydown', (event) => {
    const keys = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'];
    if (!keys.includes(event.key)) return;

    const items = Array.from(container.querySelectorAll(selector));
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;

    event.preventDefault();
    let next = index;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % items.length;
    else next = (index - 1 + items.length) % items.length;

    items.forEach((item, i) => item.setAttribute('tabindex', i === next ? '0' : '-1'));
    items[next].focus();
  });
}

/** Mark a container as busy, for both sighted users and assistive technology. */
export function setBusy(element, busy) {
  if (!element) return;
  element.setAttribute('aria-busy', busy ? 'true' : 'false');
}

// ------------------------------------------------------------------ timing ---

/** How long ago. Past instants only -- use `until` for the future. */
export function relative(iso, t, locale) {
  if (!iso) return '';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return t('time.justNow');
  if (seconds < 3600) return t('time.minutesAgo', { n: Math.floor(seconds / 60) });
  if (seconds < 86400) return t('time.hoursAgo', { n: Math.floor(seconds / 3600) });
  if (seconds < 604800) return t('time.daysAgo', { n: Math.floor(seconds / 86400) });
  return new Date(iso).toLocaleDateString(locale);
}

/**
 * How long until. For instants in the future -- expiries, renewals.
 *
 * Kept one-directional on purpose: a helper that quietly handles both is how
 * "expires just now" shipped on every live session.
 */
export function until(iso, t, locale) {
  if (!iso) return '';
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  if (seconds <= 0) return t('time.expired');
  if (seconds < 3600) return t('time.inMinutes', { n: Math.max(1, Math.floor(seconds / 60)) });
  if (seconds < 86400) return t('time.inHours', { n: Math.floor(seconds / 3600) });
  if (seconds < 604800) return t('time.inDays', { n: Math.floor(seconds / 86400) });
  return new Date(iso).toLocaleDateString(locale);
}
