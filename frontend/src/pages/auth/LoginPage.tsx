import { KeyRound, LogIn } from 'lucide-react';
import * as React from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';

import { useSession } from '@/app/session';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useLocale } from '@/i18n';
import { api, post, type ApiError } from '@/lib/api';
import { toast } from '@/lib/toast';

/**
 * The way in.
 *
 * There was no sign-in screen at all: the shell told a signed-out user to go to
 * `/`, nginx served `/` from this same bundle, and they arrived back at the same
 * message. A closed loop, and the reason nothing in the app could be tested
 * without hand-seeding a session cookie.
 *
 * Three things here are the server's decision, not this screen's:
 *
 * **What is offered.** `GET /auth/methods` says whether password sign-in is
 * enabled, whether OIDC is configured (and what to call it), and whether a
 * reset can be mailed. Rendering a password form on an SSO-only deployment, or
 * a "forgot password" link with no mail server behind it, is offering something
 * that cannot work.
 *
 * **Where a successful login goes.** A temporary password buys a session scoped
 * to exactly one action (`auth.py`: `SCOPE_PASSWORD_CHANGE`, one hour). Sending
 * such a session into the app produces a 403 on every screen, so
 * `must_change_password` routes to `/password` instead.
 *
 * **Why it was refused.** A wrong password and a throttled address are
 * different problems with different fixes, and the API distinguishes them
 * (401 vs 429). Collapsing both into "sign-in failed" makes the second one
 * look like the first, so people keep retrying and stay blocked.
 */

interface AuthMethods {
  password: boolean;
  oidc: boolean;
  oidc_label: string;
  can_reset: boolean;
}

interface LoginResult {
  email: string;
  full_name: string;
  must_change_password: boolean;
  /** Workspace ids, in order. Bare strings -- see the note on `Me.memberships`. */
  memberships: string[];
}

const OIDC_START = '/api/vanna/v2/auth/oidc/login';

export default function LoginPage() {
  const { t } = useLocale();
  const navigate = useNavigate();
  const location = useLocation();
  const { me, loading, signIn } = useSession();

  const [methods, setMethods] = React.useState<AuthMethods | null>(null);
  const [email, setEmail] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  // Set only when the account belongs to more than one workspace, so the
  // common case never sees a picker with a single option in it.
  const [choices, setChoices] = React.useState<string[] | null>(null);
  const [chosen, setChosen] = React.useState('');

  React.useEffect(() => {
    void (async () => {
      try {
        setMethods(await api<AuthMethods>('/api/vanna/v2/auth/methods'));
      } catch {
        // Offer the password form rather than nothing: a deployment that cannot
        // answer this is more likely misconfigured than SSO-only, and a blank
        // card gives the operator nothing to act on.
        setMethods({ password: true, oidc: false, oidc_label: '', can_reset: false });
      }
    })();
  }, []);

  // Where they were headed before the guard sent them here.
  const from = (location.state as { from?: string } | null)?.from ?? '/ask';

  if (!loading && me) return <Navigate to={from} replace />;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await post<LoginResult>('/api/vanna/v2/auth/login', {
        email: email.trim(),
        password,
        tenant: chosen || undefined,
      });

      if (result.must_change_password) {
        navigate('/password', { replace: true, state: { from } });
        return;
      }

      const workspaces = result.memberships ?? [];
      // More than one and none picked yet: ask, rather than guessing. Guessing
      // lands somebody in a workspace they did not mean to open and, because the
      // console reads the session workspace, shows them its data.
      if (workspaces.length > 1 && !chosen) {
        setChoices(workspaces);
        setChosen(workspaces[0]);
        setBusy(false);
        return;
      }

      await signIn({ tenant: chosen || workspaces[0] || '' });
      navigate(from, { replace: true });
    } catch (caught) {
      const failure = caught as ApiError;
      setError(failure.status === 429 ? t('login.throttled') : failure.message);
      setBusy(false);
    }
  }

  async function forgot() {
    const address = email.trim();
    if (!address) {
      setError(t('login.needEmail'));
      return;
    }
    try {
      await post('/api/vanna/v2/auth/forgot', { email: address });
    } catch {
      // Deliberately not surfaced. The endpoint answers the same way for an
      // unknown address as a known one, and a UI that reported the difference
      // would undo that: it would turn this form into a way to test whether an
      // address has an account here.
    }
    toast(t('login.resetSent'));
  }

  return (
    <div className="grid min-h-dvh place-items-center bg-surface-2 p-6">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-[1.05rem] font-semibold leading-tight">DataLens</h1>
        <p className="mt-1 mb-5 text-[0.8125rem] text-muted-foreground">{t('login.blurb')}</p>

        {methods?.password === false ? null : (
          <form onSubmit={submit} className="flex flex-col gap-3">
            <div>
              <Label htmlFor="login-email">{t('acc.email')}</Label>
              <Input
                id="login-email"
                type="email"
                autoComplete="username"
                autoFocus
                required
                value={email}
                onChange={(event) => setEmail(event.currentTarget.value)}
              />
            </div>

            <div>
              <Label htmlFor="login-password">{t('account.currentPassword')}</Label>
              <Input
                id="login-password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.currentTarget.value)}
              />
            </div>

            {choices ? (
              <div>
                <Label htmlFor="login-workspace">{t('ov.workspace')}</Label>
                <Select value={chosen} onValueChange={setChosen}>
                  <SelectTrigger id="login-workspace">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {choices.map((id) => (
                      <SelectItem key={id} value={id}>
                        {id}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            ) : null}

            {/* role=alert: this appears after the page has settled, and is not
                announced otherwise -- the user is left looking at a form that
                did nothing. */}
            {error ? (
              <p role="alert" className="text-[0.8125rem] text-bad" dir="auto">
                {error}
              </p>
            ) : null}

            <Button type="submit" variant="primary" disabled={busy} className="mt-1">
              <LogIn />
              {busy ? t('login.signingIn') : t('login.signIn')}
            </Button>
          </form>
        )}

        {methods?.oidc ? (
          <>
            {methods.password ? (
              <div className="my-4 flex items-center gap-3 text-[0.75rem] text-muted-foreground">
                <span className="h-px flex-1 bg-border" />
                {t('login.or')}
                <span className="h-px flex-1 bg-border" />
              </div>
            ) : null}
            {/* A real navigation, not fetch: the provider redirects the browser,
                and an XHR cannot follow that to another origin. */}
            <Button
              className="w-full"
              onClick={() => {
                window.location.href = OIDC_START;
              }}
            >
              <KeyRound />
              {methods.oidc_label || t('login.sso')}
            </Button>
          </>
        ) : null}

        {methods?.can_reset ? (
          <button
            type="button"
            onClick={() => void forgot()}
            className="mt-4 text-[0.8125rem] text-primary underline-offset-4 hover:underline"
          >
            {t('login.forgot')}
          </button>
        ) : null}
      </Card>
    </div>
  );
}
