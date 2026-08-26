import { KeyRound, UserPlus } from 'lucide-react';
import * as React from 'react';

import { useConfirm } from '@/components/primitives/confirm';
import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useLocale } from '@/i18n';
import { api, patch, post } from '@/lib/api';
import { relative } from '@/lib/time';
import { toast, toastError } from '@/lib/toast';
import type { Account } from '@/types';

/**
 * Every sign-in on the platform.
 *
 * Not scoped, and it must not be: identity lives in `vanna_app.users` and is
 * global, while membership lives in `tenant_users` and is per workspace. One
 * person can hold a different role in each of eleven workspaces, and asking
 * "does this address have an account at all" is a question no workspace can
 * answer on its own.
 *
 * ## The temporary password is shown once, and only sometimes
 *
 * The server generates it -- an admin choosing somebody else's password means
 * the admin knows it, and the owner has no way to tell whether they still do.
 * When SMTP is configured the password is mailed and the response withholds it
 * deliberately; with no mail server it comes back in the response because
 * otherwise the account is unreachable. This screen has to handle both, and say
 * which happened, or an admin waits for an email that was never sent.
 */

interface AccountWrite {
  email: string;
  temporary_password: string;
  mailed: boolean;
}

export default function AccountsPage() {
  const { t, locale } = useLocale();
  const confirm = useConfirm();

  const [rows, setRows] = React.useState<Account[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [creating, setCreating] = React.useState(false);
  const [issued, setIssued] = React.useState<AccountWrite | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      const body = await api<{ accounts: Account[] }>('/api/vanna/v2/admin/accounts');
      setRows(body.accounts ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function toggleActive(account: Account) {
    setBusy(account.email);
    const next = !account.is_active;
    setRows((current) =>
      current?.map((a) => (a.email === account.email ? { ...a, is_active: next } : a)) ?? current,
    );
    try {
      await patch(`/api/vanna/v2/admin/accounts/${encodeURIComponent(account.email)}`, {
        is_active: next,
      });
      toast(t('common.updated'));
    } catch (caught) {
      setRows((current) =>
        current?.map((a) =>
          a.email === account.email ? { ...a, is_active: account.is_active } : a,
        ) ?? current,
      );
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function resetPassword(account: Account) {
    const ok = await confirm.ask({
      title: t('acc.resetTitle'),
      body: t('acc.resetConfirm', { email: account.email }),
      confirmLabel: t('acc.reset'),
      danger: true,
    });
    if (!ok) return;
    try {
      const body = await post<AccountWrite>(
        `/api/vanna/v2/admin/accounts/${encodeURIComponent(account.email)}/reset`,
      );
      setIssued({ ...body, email: account.email });
      void load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('tab.accounts')}
        description={t('acct.blurb')}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            <UserPlus />
            {t('acc.create')}
          </Button>
        }
      />

      <Toolbar>
        <p className="text-[0.8125rem] text-muted-foreground">{t('acc.notMembership')}</p>
      </Toolbar>

      {error ? (
        <ErrorState title={t('acct.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={6} />
      ) : rows.length === 0 ? (
        <EmptyState
          title={t('acc.none')}
          action={
            <Button variant="primary" onClick={() => setCreating(true)}>
              <UserPlus />
              {t('acc.create')}
            </Button>
          }
        />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('acc.email')}</Th>
                <Th>{t('acc.name')}</Th>
                <Th className="w-32">{t('acc.status')}</Th>
                <Th>{t('acc.lastSignIn')}</Th>
                <Th className="w-32" />
              </Tr>
            </thead>
            <Tbody>
              {rows.map((account) => (
                <Tr key={account.email}>
                  <Td dir="auto">{account.email}</Td>
                  <Td dir="auto">{account.full_name || '—'}</Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <Switch
                        checked={account.is_active}
                        disabled={busy === account.email}
                        aria-label={`${t('acc.status')} ${account.email}`}
                        onCheckedChange={() => void toggleActive(account)}
                      />
                      <Badge tone={account.is_active ? 'ok' : 'err'}>
                        {account.is_active ? t('acc.active') : t('ov.inactive')}
                      </Badge>
                    </div>
                  </Td>
                  <Td className="text-muted-foreground">
                    {account.last_login_at
                      ? relative(account.last_login_at, t, locale)
                      : t('ov.never')}
                  </Td>
                  <Td>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => void resetPassword(account)}
                    >
                      <KeyRound />
                      {t('acc.reset')}
                    </Button>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}

      {creating ? (
        <CreateAccount
          onClose={() => setCreating(false)}
          onCreated={(result) => {
            setCreating(false);
            setIssued(result);
            void load();
          }}
        />
      ) : null}

      {issued ? <IssuedPassword result={issued} onClose={() => setIssued(null)} /> : null}
    </PageBody>
  );
}

/**
 * The one-time password, or the news that it was mailed instead.
 *
 * A modal rather than a toast: this value cannot be retrieved again, and a toast
 * that dismisses itself after four seconds is the wrong container for something
 * you have to write down.
 */
function IssuedPassword({ result, onClose }: { result: AccountWrite; onClose: () => void }) {
  const { t } = useLocale();

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-[460px]">
        <DialogHeader>
          <DialogTitle>{result.email}</DialogTitle>
          <DialogDescription>
            {result.mailed ? t('acc.mailed') : t('acc.showOnce')}
          </DialogDescription>
        </DialogHeader>

        {result.temporary_password ? (
          <div className="rounded-md border border-warn/40 bg-warn/5 p-3">
            <code className="block break-all font-mono text-[0.85rem]" dir="ltr">
              {result.temporary_password}
            </code>
          </div>
        ) : null}

        <p className="mt-2 text-[0.8125rem] text-muted-foreground">{t('acc.mustChange')}</p>

        <DialogFooter>
          <Button variant="primary" onClick={onClose}>
            {t('common.close')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CreateAccount({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (result: AccountWrite) => void;
}) {
  const { t } = useLocale();
  const [email, setEmail] = React.useState('');
  const [fullName, setFullName] = React.useState('');
  const [saving, setSaving] = React.useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const body = await post<AccountWrite>('/api/vanna/v2/admin/accounts', {
        email: email.trim().toLowerCase(),
        full_name: fullName.trim(),
      });
      onCreated({ ...body, email: email.trim().toLowerCase() });
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-[460px]">
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{t('acc.create')}</DialogTitle>
            {/* An account can sign in; it sees nothing until a workspace grants
                it membership on the Members screen. */}
            <DialogDescription>{t('acc.createHint')}</DialogDescription>
          </DialogHeader>

          <div className="mt-3 flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="acc-email">{t('acc.email')}</Label>
              <Input
                id="acc-email"
                type="email"
                dir="ltr"
                required
                autoFocus
                value={email}
                placeholder="person@example.com"
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="acc-name">{t('acc.name')}</Label>
              <Input
                id="acc-name"
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary" disabled={saving || !email.trim()}>
              {t('common.create')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
