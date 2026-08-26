import { Trash2, UserPlus } from 'lucide-react';
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useSession } from '@/app/session';
import { useLocale } from '@/i18n';
import { api, del, patch, post } from '@/lib/api';
import { relative } from '@/lib/time';
import { toast, toastError } from '@/lib/toast';
import type { Member, Role } from '@/types';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * Who is in a workspace, and as what.
 *
 * The role here is the *workspace* role -- one of the three `tenant_users`
 * allows. A platform admin is a different tier entirely (an address in
 * `VANNA_ADMIN_EMAILS`) and does not appear as a fourth value: conflating them
 * is how a workspace admin ends up able to repoint their own warehouse.
 *
 * Identity is global and membership is not, so the same person can be an admin
 * here and a viewer next door. That is why this screen is scoped and the
 * Accounts screen is not.
 *
 * ## Adding somebody does not create them
 *
 * `POST .../users` grants an existing account membership. It does not mint a
 * credential -- that is the Accounts screen, and the separation is deliberate:
 * an account can sign in, a membership decides which workspaces it then sees.
 * The form says so, because "add member" reads like it should create a person.
 */

const ROLES: Role[] = ['admin', 'analyst', 'viewer'];

export default function MembersPage() {
  const { t, locale } = useLocale();
  const { me } = useSession();
  const tenant = useConcreteScope();
  const confirm = useConfirm();

  const [rows, setRows] = React.useState<Member[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [adding, setAdding] = React.useState(false);
  const [busy, setBusy] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setRows(null);
    try {
      const body = await api<{ users: Member[] }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/users`,
      );
      setRows(body.users ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}`;

  async function changeRole(member: Member, role: Role) {
    setBusy(member.id);
    // Optimistic: the badge *is* the role, and waiting for a round trip to
    // repaint it makes the select feel like it did not take.
    setRows((current) =>
      current?.map((m) => (m.id === member.id ? { ...m, role } : m)) ?? current,
    );
    try {
      await patch(`${base}/users/${encodeURIComponent(member.id)}`, { role });
      toast(t('common.updated'));
    } catch (caught) {
      setRows((current) =>
        current?.map((m) => (m.id === member.id ? { ...m, role: member.role } : m)) ?? current,
      );
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function toggleActive(member: Member) {
    setBusy(member.id);
    const next = !member.is_active;
    setRows((current) =>
      current?.map((m) => (m.id === member.id ? { ...m, is_active: next } : m)) ?? current,
    );
    try {
      await patch(`${base}/users/${encodeURIComponent(member.id)}`, { is_active: next });
      toast(t('common.updated'));
    } catch (caught) {
      setRows((current) =>
        current?.map((m) => (m.id === member.id ? { ...m, is_active: member.is_active } : m)) ??
        current,
      );
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function remove(member: Member) {
    const ok = await confirm.ask({
      title: t('mem.removeTitle'),
      body: t('mem.removeConfirm', { email: member.email, workspace: tenant }),
      confirmLabel: t('common.remove'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`${base}/users/${encodeURIComponent(member.id)}`);
      setRows((current) => current?.filter((m) => m.id !== member.id) ?? current);
      toast(t('common.removed'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  // Removing your own membership logs you out of the workspace you are
  // administering, from the screen you are administering it on.
  const self = (me?.user.email ?? '').toLowerCase();

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('tab.members')}
        description={t('mem.blurb', { workspace: tenant })}
        actions={
          <Button variant="primary" onClick={() => setAdding(true)} disabled={!tenant}>
            <UserPlus />
            {t('mem.add')}
          </Button>
        }
      />

      <Toolbar>
        <ScopePicker id="members-scope" />
      </Toolbar>

      {error ? (
        <ErrorState title={t('mem.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={6} />
      ) : rows.length === 0 ? (
        <EmptyState
          title={t('mem.none')}
          hint={t('mem.noneHint')}
          action={
            <Button variant="primary" onClick={() => setAdding(true)}>
              <UserPlus />
              {t('mem.add')}
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
                <Th className="w-36">{t('mem.role')}</Th>
                <Th className="w-28">{t('acc.status')}</Th>
                <Th>{t('mem.lastSeen')}</Th>
                <Th className="w-12" />
              </Tr>
            </thead>
            <Tbody>
              {rows.map((member) => {
                const isSelf = member.email.toLowerCase() === self;
                return (
                  <Tr key={member.id}>
                    <Td dir="auto">
                      {member.email}
                      {isSelf ? (
                        <Badge tone="neutral" className="ms-2">
                          {t('mem.you')}
                        </Badge>
                      ) : null}
                    </Td>
                    <Td dir="auto">{member.full_name || '—'}</Td>
                    <Td>
                      <Select
                        value={member.role}
                        disabled={busy === member.id || isSelf}
                        onValueChange={(next) => void changeRole(member, next as Role)}
                      >
                        <SelectTrigger aria-label={`${t('mem.role')} ${member.email}`}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {ROLES.map((role) => (
                            <SelectItem key={role} value={role}>
                              {role}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        <Switch
                          checked={member.is_active}
                          disabled={busy === member.id || isSelf}
                          aria-label={`${t('acc.status')} ${member.email}`}
                          onCheckedChange={() => void toggleActive(member)}
                        />
                        <Badge tone={member.is_active ? 'ok' : 'err'}>
                          {member.is_active ? t('acc.active') : t('ov.inactive')}
                        </Badge>
                      </div>
                    </Td>
                    <Td className="text-muted-foreground">
                      {member.last_seen_at
                        ? relative(member.last_seen_at, t, locale)
                        : t('ov.never')}
                    </Td>
                    <Td>
                      <Button
                        size="icon"
                        variant="ghost"
                        // Both controls above are disabled for your own row too:
                        // demoting or deactivating yourself locks you out of the
                        // screen you are standing on.
                        disabled={isSelf}
                        title={isSelf ? t('mem.notYourself') : t('common.remove')}
                        aria-label={t('common.remove')}
                        onClick={() => void remove(member)}
                      >
                        <Trash2 />
                      </Button>
                    </Td>
                  </Tr>
                );
              })}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}

      {adding ? (
        <AddMember
          tenant={tenant}
          onClose={() => setAdding(false)}
          onAdded={() => {
            setAdding(false);
            void load();
          }}
        />
      ) : null}
    </PageBody>
  );
}

function AddMember({
  tenant,
  onClose,
  onAdded,
}: {
  tenant: string;
  onClose: () => void;
  onAdded: () => void;
}) {
  const { t } = useLocale();
  const [email, setEmail] = React.useState('');
  const [fullName, setFullName] = React.useState('');
  const [role, setRole] = React.useState<Role>('analyst');
  const [saving, setSaving] = React.useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await post(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/users`, {
        email: email.trim().toLowerCase(),
        full_name: fullName.trim(),
        role,
      });
      toast(t('mem.added'));
      onAdded();
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
            <DialogTitle>{t('mem.add')}</DialogTitle>
            {/* Said plainly: this grants access to an account that already
                exists. It does not create one. */}
            <DialogDescription>{t('mem.addHint')}</DialogDescription>
          </DialogHeader>

          <div className="mt-3 flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mem-email">{t('acc.email')}</Label>
              <Input
                id="mem-email"
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
              <Label htmlFor="mem-name">{t('acc.name')}</Label>
              <Input
                id="mem-name"
                value={fullName}
                onChange={(event) => setFullName(event.target.value)}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="mem-role">{t('mem.role')}</Label>
              <Select value={role} onValueChange={(next) => setRole(next as Role)}>
                <SelectTrigger id="mem-role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROLES.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <DialogFooter>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary" disabled={saving || !email.trim()}>
              {t('mem.add')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
