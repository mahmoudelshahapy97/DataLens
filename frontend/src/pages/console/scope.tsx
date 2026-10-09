import * as React from 'react';

import { useSession } from '@/app/session';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';

/**
 * Which workspace the console screens are reading.
 *
 * Separate from the session's own workspace on purpose. `identity.tenant` is
 * what the *workspace* screens query -- it sets `X-Tenant-Id` and changing it
 * changes which warehouse the chat answers from. A platform admin reading the
 * audit trail for a customer is doing something else entirely, and making them
 * switch their whole session to do it would repoint their chat as a side effect.
 *
 * So this is a query parameter, not a header, and the server resolves it through
 * `visible_tenant` on every request. An empty string means "every workspace",
 * which only a platform admin can obtain -- for anybody else the server pins the
 * answer to their own workspace regardless of what is asked for here.
 */

interface ConsoleScopeValue {
  /** Workspace id, or '' for platform-wide. */
  scope: string;
  setScope: (next: string) => void;
  /** Whether there is anything to choose between. */
  canChooseScope: boolean;
  workspaces: Array<{ id: string; name: string }>;
}

const ConsoleScopeContext = React.createContext<ConsoleScopeValue | null>(null);

export function ConsoleScopeProvider({ children }: { children: React.ReactNode }) {
  const { me, isPlatformAdmin } = useSession();
  const [scope, setScope] = React.useState<string>('');
  const [workspaces, setWorkspaces] = React.useState<Array<{ id: string; name: string }>>([]);

  // A platform admin starts on the whole platform; everybody else on the only
  // workspace they can see. Keyed on the tier rather than set once, so the
  // default is right after `me` resolves rather than before it.
  React.useEffect(() => {
    setScope(isPlatformAdmin ? '' : (me?.tenant?.id ?? ''));
  }, [isPlatformAdmin, me?.tenant?.id]);

  React.useEffect(() => {
    if (!isPlatformAdmin) {
      setWorkspaces([]);
      return;
    }
    void (async () => {
      try {
        const body = await api<{ tenants: Array<{ id: string; name: string }> }>(
          '/api/vanna/v2/admin/tenants',
        );
        setWorkspaces(body.tenants ?? []);
      } catch {
        // The picker degrades to "all workspaces" only. Every screen under it
        // still works, so this is not worth an error state of its own.
        setWorkspaces([]);
      }
    })();
  }, [isPlatformAdmin]);

  const value = React.useMemo<ConsoleScopeValue>(
    () => ({
      scope,
      setScope,
      canChooseScope: isPlatformAdmin && workspaces.length > 0,
      workspaces,
    }),
    [scope, isPlatformAdmin, workspaces],
  );

  return <ConsoleScopeContext.Provider value={value}>{children}</ConsoleScopeContext.Provider>;
}

export function useConsoleScope(): ConsoleScopeValue {
  const context = React.useContext(ConsoleScopeContext);
  if (!context) throw new Error('useConsoleScope must be used inside <ConsoleScopeProvider>');
  return context;
}

/**
 * The workspace a per-workspace screen should read.
 *
 * The access log has no platform-wide reading -- `audit_events` is queried one
 * workspace at a time -- so "all workspaces" has to resolve to a concrete one,
 * and the screen says which rather than implying it is showing everything.
 */
export function useConcreteScope(): string {
  const { scope } = useConsoleScope();
  const { me } = useSession();
  return scope || me?.tenant?.id || '';
}

/**
 * Radix's Select treats the empty string as "no value", so platform-wide needs a
 * token of its own rather than ''.
 */
const ALL = '__all__';

/**
 * The workspace picker, shown only to somebody with more than one to pick.
 *
 * `concrete` omits the "all workspaces" option. Most console screens read one
 * workspace at a time -- there is no platform-wide grant matrix or member list
 * -- and offering an option those screens then quietly resolve to the caller's
 * own workspace is worse than not offering it: the picker read "All workspaces"
 * while the heading underneath read "in demo", and both were telling the truth
 * about different things.
 */
export function ScopePicker({ id, concrete = true }: { id: string; concrete?: boolean }) {
  const t = useLocale().t;
  const { scope, setScope, canChooseScope, workspaces } = useConsoleScope();
  const resolved = useConcreteScope();

  if (!canChooseScope) return null;

  const value = concrete ? resolved : scope || ALL;

  return (
    <div className="flex items-center gap-2">
      <Label htmlFor={id}>{t('ov.workspace')}</Label>
      <Select value={value} onValueChange={(next) => setScope(next === ALL ? '' : next)}>
        <SelectTrigger id={id} className="w-56">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {concrete ? null : <SelectItem value={ALL}>{t('ov.allWorkspaces')}</SelectItem>}
          {workspaces.map((workspace) => (
            <SelectItem key={workspace.id} value={workspace.id}>
              {workspace.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
