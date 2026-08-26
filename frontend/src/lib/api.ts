/**
 * The one way this application talks to its API.
 *
 * A direct port of `public/assets/shared/core.js`, and the behaviours below are
 * load-bearing rather than incidental -- each one was a bug before it was a line
 * of code:
 *
 *   credentials: 'include'   the session is an httpOnly cookie this code cannot read
 *   X-CSRF-Token             signed double-submit, on unsafe verbs *only*
 *   header provider          X-Tenant-Id / X-Data-Source-Id, a preference the server
 *                            re-checks against tenant_users -- never a credential
 *   errorText()              the API's {code, phase, message, hint} envelope and
 *                            Pydantic's array-of-errors both become one readable line
 *   404 rewording            "Not found" is what an authorisation failure returns
 *                            here (see backend/vanna_app/authz.py) -- so the message
 *                            has to admit the second possibility
 *
 * `esc()` is deliberately *not* ported. React escapes by default; a stray esc()
 * in JSX renders `&amp;lt;` to the user.
 */

export interface ApiError extends Error {
  status: number;
  detail?: unknown;
}

/** Turn any error payload into one readable line. */
export function errorText(detail: unknown, fallback: string): string {
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    // Pydantic validation errors, and our own multi-issue lists.
    return detail.map((item) => errorText(item, '')).filter(Boolean).join('; ') || fallback;
  }

  if (typeof detail === 'object') {
    // Our structured envelope: code / phase / message / hint. The hint is the
    // actionable half, so it is kept.
    const record = detail as Record<string, unknown>;
    const message = (record.message || record.msg || record.detail || '') as string;
    const hint = record.hint ? ` ${record.hint}` : '';
    if (message) return `${message}${hint}`.trim();
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return String(detail);
}

/**
 * The CSRF token the server issued, read from its (deliberately readable) cookie.
 *
 * Signed double-submit: the server sets `vanna_csrf` and requires the same value
 * back in a header. An attacker's page can cause the cookie to be *sent* but cannot
 * read it to build the header, which is the whole mechanism.
 */
export function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)vanna_csrf=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : '';
}

type HeaderProvider = () => Record<string, string>;

/** Extra headers every request carries (the workspace and data source, mostly). */
let headerProvider: HeaderProvider = () => ({});

export function setHeaderProvider(fn: HeaderProvider): void {
  headerProvider = fn;
}

type AuthFailureHandler = (status: number, detail: unknown) => void;

let onAuthFailure: AuthFailureHandler | null = null;

export function setAuthFailureHandler(fn: AuthFailureHandler | null): void {
  onAuthFailure = fn;
}

const SAFE = new Set(['GET', 'HEAD', 'OPTIONS']);

export async function api<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || 'GET').toUpperCase();

  const headers: Record<string, string> = {
    ...headerProvider(),
    ...((options.headers as Record<string, string>) || {}),
  };

  // FormData sets its own multipart boundary; naming a Content-Type here would
  // produce one without it and the server would fail to parse the body.
  if (!(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }

  // Only on state-changing verbs: the server does not check the safe ones, and
  // sending it everywhere would put the token in more places than necessary.
  if (!SAFE.has(method)) {
    const token = csrfToken();
    if (token) headers['X-CSRF-Token'] = token;
  }

  const response = await fetch(path, { credentials: 'include', ...options, headers });

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    let body: { detail?: unknown } | null = null;
    try {
      body = await response.json();
      detail = errorText(body?.detail, detail);
    } catch {
      /* non-JSON error body; the status is all we have */
    }

    if (response.status === 404) {
      detail = detail === 'Not found' ? 'Not found, or your account lacks access.' : detail;
    }
    if ((response.status === 401 || response.status === 403) && onAuthFailure) {
      onAuthFailure(response.status, body?.detail);
    }

    const error = new Error(detail) as ApiError;
    error.status = response.status;
    error.detail = body?.detail;
    throw error;
  }

  return (response.status === 204 ? null : await response.json()) as T;
}

/** JSON body helpers, so no call site hand-rolls JSON.stringify. */
export const post = <T = unknown>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });

export const patch = <T = unknown>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) });

export const put = <T = unknown>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) });

export const del = <T = unknown>(path: string) => api<T>(path, { method: 'DELETE' });

/**
 * A download that goes through the same credential path as `api`.
 *
 * Not an `<a href>`: the export routes are behind the session cookie *and* the
 * tenant header, and an anchor carries the cookie but not the header -- so the
 * server would render the export for whichever workspace the session defaults to.
 */
export async function download(path: string, fallbackName: string): Promise<void> {
  const response = await fetch(path, {
    credentials: 'include',
    headers: headerProvider(),
  });
  if (!response.ok) {
    const error = new Error(`Download failed (${response.status})`) as ApiError;
    error.status = response.status;
    throw error;
  }

  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = match ? decodeURIComponent(match[1]) : fallbackName;

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoked on the next tick rather than immediately: Safari reads the blob
  // asynchronously and a synchronous revoke cancels the download.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
