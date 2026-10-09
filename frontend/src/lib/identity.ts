/**
 * Which workspace and which database this browser is asking about.
 *
 * Both travel as headers and neither is a credential. `X-Tenant-Id` names a
 * workspace; the server checks membership against `tenant_users` before honouring
 * it and answers 404 if there is none, so naming someone else's workspace reveals
 * nothing. `X-Data-Source-Id` is validated against the workspace's registry the
 * same way.
 *
 * Persisted because a reload should not drop you into a different workspace than
 * the one you were reading.
 */

const IDENTITY_KEY = 'vanna.identity';

export interface Identity {
  tenant: string;
  dataSourceId?: string;
}

export function readIdentity(): Identity | null {
  try {
    const raw = localStorage.getItem(IDENTITY_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Identity>;
    return parsed.tenant ? { tenant: parsed.tenant, dataSourceId: parsed.dataSourceId } : null;
  } catch {
    // A corrupt entry is not worth a crash on boot; treat it as absent and let
    // the sign-in screen write a good one.
    return null;
  }
}

export function writeIdentity(identity: Identity | null): void {
  try {
    if (identity) localStorage.setItem(IDENTITY_KEY, JSON.stringify(identity));
    else localStorage.removeItem(IDENTITY_KEY);
  } catch {
    /* private mode; the session cookie still works, only the preference is lost */
  }
}

/** The headers every API call carries. Wired into api.ts by the app shell. */
export function identityHeaders(identity: Identity | null): Record<string, string> {
  if (!identity) return {};
  const headers: Record<string, string> = { 'X-Tenant-Id': identity.tenant };
  if (identity.dataSourceId) headers['X-Data-Source-Id'] = identity.dataSourceId;
  return headers;
}
