import * as React from 'react';

import { api, setHeaderProvider, type ApiError } from '@/lib/api';
import { identityHeaders, readIdentity, writeIdentity, type Identity } from '@/lib/identity';
import type { Me, Role } from '@/types';

/**
 * Who is signed in, and to which workspace.
 *
 * `me` is the *server's* answer, refetched whenever the workspace changes. It is
 * never derived from the identity in localStorage: the workspace header is a
 * preference the server re-checks against `tenant_users`, and the role that comes
 * back is the one that row says -- not one this code inferred.
 *
 * Everything this context exposes is for *presentation*. Hiding a nav item the
 * caller may not use is a courtesy; the guarantee is that the route behind it
 * answers 404. `backend/vanna_app/authz.py` is where that is enforced, and it is
 * the only place it is enforced.
 */

interface SessionValue {
  me: Me | null;
  identity: Identity | null;
  /** Still resolving whether there is a session at all. */
  loading: boolean;
  role: Role | null;
  isPlatformAdmin: boolean;
  isWorkspaceAdmin: boolean;
  /** analyst or admin -- the two that may author. Mirrors `forbid_viewer`. */
  canAuthor: boolean;
  signIn: (identity: Identity) => Promise<void>;
  signOut: () => Promise<void>;
  switchWorkspace: (tenant: string) => Promise<void>;
  switchDataSource: (dataSourceId: string | undefined) => Promise<void>;
  refresh: () => Promise<void>;
}

const SessionContext = React.createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [identity, setIdentity] = React.useState<Identity | null>(readIdentity);
  const [me, setMe] = React.useState<Me | null>(null);
  const [loading, setLoading] = React.useState(true);

  // The header provider is read on every request, so it has to see the *current*
  // identity. A ref rather than a closure over state: setHeaderProvider is called
  // once, and a closure captured at that moment would pin the first workspace
  // forever -- which is exactly how a workspace switch used to send the previous
  // workspace's header on the first request after it.
  const identityRef = React.useRef(identity);
  identityRef.current = identity;

  React.useEffect(() => {
    setHeaderProvider(() => identityHeaders(identityRef.current));
  }, []);

  const load = React.useCallback(async () => {
    try {
      const next = await api<Me>('/api/vanna/v2/me');
      setMe(next);
    } catch {
      // 401 means no session; anything else means the API is unreachable. Both
      // land on the sign-in screen, which is the only place that can help.
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load, identity?.tenant, identity?.dataSourceId]);

  const value = React.useMemo<SessionValue>(() => {
    const role = me?.user.role ?? null;
    return {
      me,
      identity,
      loading,
      role,
      isPlatformAdmin: Boolean(me?.is_platform_admin),
      isWorkspaceAdmin: Boolean(me?.is_admin),
      canAuthor: role === 'admin' || role === 'analyst',

      signIn: async (next) => {
        writeIdentity(next);
        setIdentity(next);
        setLoading(true);
      },

      signOut: async () => {
        // `/auth/logout`, not `/logout`. This posted to the latter, which is not
        // a registered route: the 404 was swallowed by the catch below, local
        // state cleared, the screen said "signed out" -- and the session cookie
        // stayed valid. Signing out did not sign you out.
        let failed: unknown = null;
        try {
          await api('/api/vanna/v2/auth/logout', { method: 'POST' });
        } catch (caught) {
          // An already-expired cookie is a 401 and genuinely fine; anything else
          // means the session may still be live on the server, and saying so is
          // the whole point -- swallowing it is what hid the bug above.
          if ((caught as ApiError).status !== 401) failed = caught;
        } finally {
          writeIdentity(null);
          setIdentity(null);
          setMe(null);
        }
        if (failed) throw failed;
      },

      switchWorkspace: async (tenant) => {
        // The data source is dropped deliberately: it is registered per
        // workspace, so carrying it across would name one the new workspace does
        // not have and every request would 404 until the user noticed.
        const next: Identity = { tenant };
        writeIdentity(next);
        setIdentity(next);
        setLoading(true);
      },

      switchDataSource: async (dataSourceId) => {
        if (!identity) return;
        const next: Identity = { ...identity, dataSourceId };
        writeIdentity(next);
        setIdentity(next);
      },

      refresh: load,
    };
  }, [me, identity, loading, load]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const context = React.useContext(SessionContext);
  if (!context) throw new Error('useSession must be used inside <SessionProvider>');
  return context;
}
