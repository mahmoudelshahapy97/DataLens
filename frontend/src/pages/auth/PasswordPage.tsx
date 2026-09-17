import { ShieldCheck } from 'lucide-react';
import * as React from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { useSession } from '@/app/session';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useLocale } from '@/i18n';
import { post, type ApiError } from '@/lib/api';
import { toastSuccess } from '@/lib/toast';

/**
 * The one thing a temporary password can do.
 *
 * An account created or reset by an administrator gets a password-change-scoped
 * session (`auth.py`: `SCOPE_PASSWORD_CHANGE`, one hour). Every other route
 * refuses it, so this screen is not a courtesy prompt that can be dismissed --
 * it is the only reachable destination until the password is changed.
 *
 * The server requires the current password even though the caller is already
 * authenticated, which is deliberate: a borrowed laptop should not be a way to
 * lock the owner out of their own account. It also ends every *other* session
 * and revokes API tokens, then promotes this one to a full session -- so on
 * success the app is enterable and no second sign-in is needed.
 */

/** `MIN_PASSWORD_LENGTH` in `backend/vanna_app/routes/auth.py`. */
const MIN_LENGTH = 12;

export default function PasswordPage() {
  const { t } = useLocale();
  const navigate = useNavigate();
  const location = useLocation();
  const { refresh } = useSession();

  const [current, setCurrent] = React.useState('');
  const [next, setNext] = React.useState('');
  const [again, setAgain] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const from = (location.state as { from?: string } | null)?.from ?? '/ask';

  // Checked here only to keep the button honest; the server re-checks all of it
  // and adds a weak-password test this screen deliberately does not duplicate.
  const mismatch = again.length > 0 && next !== again;
  const tooShort = next.length > 0 && next.length < MIN_LENGTH;
  const ready = current && next.length >= MIN_LENGTH && next === again && next !== current;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post('/api/vanna/v2/auth/password', {
        current_password: current,
        new_password: next,
      });
      toastSuccess(t('password.changed'));
      // The session was just promoted to a full one, but `me` still carries the
      // restricted shape until it is refetched.
      await refresh();
      navigate(from, { replace: true });
    } catch (caught) {
      setError((caught as ApiError).message);
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-dvh place-items-center bg-surface-2 p-6">
      <Card className="w-full max-w-sm p-6">
        <h1 className="flex items-center gap-2 text-[1.05rem] font-semibold leading-tight">
          <ShieldCheck className="size-5 text-primary-ink" aria-hidden />
          {t('password.title')}
        </h1>
        <p className="mt-1 mb-5 text-[0.8125rem] text-muted-foreground">{t('password.blurb')}</p>

        <form onSubmit={submit} className="flex flex-col gap-3">
          <div>
            <Label htmlFor="pw-current">{t('account.currentPassword')}</Label>
            <Input
              id="pw-current"
              type="password"
              autoComplete="current-password"
              autoFocus
              required
              value={current}
              onChange={(event) => setCurrent(event.currentTarget.value)}
            />
          </div>

          <div>
            <Label htmlFor="pw-new">{t('account.newPassword')}</Label>
            <Input
              id="pw-new"
              type="password"
              autoComplete="new-password"
              required
              value={next}
              onChange={(event) => setNext(event.currentTarget.value)}
            />
            <p className="mt-1 text-[0.72rem] text-muted-foreground">
              {t('password.minLength', { n: MIN_LENGTH })}
            </p>
          </div>

          <div>
            <Label htmlFor="pw-again">{t('password.confirm')}</Label>
            <Input
              id="pw-again"
              type="password"
              autoComplete="new-password"
              required
              value={again}
              onChange={(event) => setAgain(event.currentTarget.value)}
            />
          </div>

          {mismatch ? (
            <p role="alert" className="text-[0.8125rem] text-bad">
              {t('password.mismatch')}
            </p>
          ) : null}
          {tooShort ? (
            <p className="text-[0.8125rem] text-warn">{t('password.minLength', { n: MIN_LENGTH })}</p>
          ) : null}
          {error ? (
            <p role="alert" className="text-[0.8125rem] text-bad" dir="auto">
              {error}
            </p>
          ) : null}

          <Button type="submit" variant="primary" disabled={busy || !ready} className="mt-1">
            {busy ? t('password.saving') : t('account.changePassword')}
          </Button>
        </form>
      </Card>
    </div>
  );
}
