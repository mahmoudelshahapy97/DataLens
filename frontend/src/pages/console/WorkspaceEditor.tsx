import * as React from 'react';

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
import { Textarea } from '@/components/ui/textarea';
import { useLocale } from '@/i18n';
import { patch, post } from '@/lib/api';
import { toast, toastError } from '@/lib/toast';
import type { WorkspaceUsage } from '@/types';

/**
 * Creating a workspace, and editing one.
 *
 * ## The connection string is the dangerous field
 *
 * A workspace *is* its database. Repointing an existing one at another warehouse
 * does not migrate anything -- it silently shows this workspace's members
 * somebody else's data, under the names and grants of the workspace they thought
 * they were in. Nothing downstream can catch that, because every layer below is
 * working correctly on the connection it was given.
 *
 * So on an existing workspace the field starts locked behind a deliberate
 * toggle. That is friction on purpose, and it is the only place in this console
 * where a field is disabled by default rather than by permission.
 *
 * Leaving it blank on edit means *unchanged*, not *cleared* -- `TenantUpdate`
 * treats every field as optional, and a blank string would be a change to empty.
 */

const ID_RULE = /^[a-z0-9][a-z0-9_-]{1,38}[a-z0-9]$/;

export function WorkspaceEditor({
  workspace,
  onClose,
  onSaved,
}: {
  /** Absent when creating. */
  workspace: WorkspaceUsage | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useLocale();
  const editing = workspace !== null;

  const [id, setId] = React.useState(workspace?.id ?? '');
  const [name, setName] = React.useState(workspace?.name ?? '');
  const [description, setDescription] = React.useState(workspace?.description ?? '');
  const [databaseUrl, setDatabaseUrl] = React.useState('');
  const [repoint, setRepoint] = React.useState(false);
  const [dailyQuota, setDailyQuota] = React.useState(
    workspace?.daily_quota != null ? String(workspace.daily_quota) : '',
  );
  const [maxRows, setMaxRows] = React.useState(
    workspace?.max_rows != null ? String(workspace.max_rows) : '',
  );
  const [allowWrites, setAllowWrites] = React.useState(Boolean(workspace?.allow_writes));
  const [allowByoKey, setAllowByoKey] = React.useState(workspace?.allow_byo_key !== false);
  const [isActive, setIsActive] = React.useState(workspace?.is_active ?? true);
  const [saving, setSaving] = React.useState(false);

  const idValid = editing || ID_RULE.test(id);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);

    // Numbers are optional end to end: an empty box means "leave the default",
    // and sending 0 would set the quota to zero.
    const optionalNumber = (value: string) =>
      value.trim() === '' ? undefined : Number(value);

    try {
      if (editing) {
        await patch(`/api/vanna/v2/admin/tenants/${encodeURIComponent(workspace.id)}`, {
          name: name.trim(),
          description: description.trim(),
          is_active: isActive,
          allow_writes: allowWrites,
          allow_byo_key: allowByoKey,
          daily_quota: optionalNumber(dailyQuota),
          max_rows: optionalNumber(maxRows),
          // Only when the operator deliberately unlocked it. Undefined leaves the
          // connection exactly as it was.
          database_url: repoint && databaseUrl.trim() ? databaseUrl.trim() : undefined,
        });
      } else {
        await post('/api/vanna/v2/admin/tenants', {
          id: id.trim(),
          name: name.trim() || id.trim(),
          description: description.trim(),
          database_url: databaseUrl.trim() || undefined,
          daily_quota: optionalNumber(dailyQuota),
          max_rows: optionalNumber(maxRows),
        });
      }
      toast(editing ? t('common.updated') : t('ws.created'));
      onSaved();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-[560px]">
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{editing ? t('ws.edit') : t('ws.create')}</DialogTitle>
            <DialogDescription>
              {editing ? workspace.id : t('ws.createHint')}
            </DialogDescription>
          </DialogHeader>

          <div className="mt-3 flex flex-col gap-3">
            {!editing ? (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="ws-id">{t('ws.id')}</Label>
                <Input
                  id="ws-id"
                  dir="ltr"
                  required
                  autoFocus
                  value={id}
                  placeholder="northwind"
                  onChange={(event) => setId(event.target.value.toLowerCase())}
                />
                {/* The rule is enforced server-side too. Showing it here means a
                    typo is caught before a round trip, not after. */}
                <p className={`text-[0.75rem] ${idValid ? 'text-muted-foreground' : 'text-bad'}`}>
                  {t('ws.idRule')}
                </p>
              </div>
            ) : null}

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="ws-name">{t('ws.name')}</Label>
              <Input
                id="ws-name"
                value={name}
                autoFocus={editing}
                onChange={(event) => setName(event.target.value)}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="ws-desc">{t('ws.description')}</Label>
              <Textarea
                id="ws-desc"
                className="min-h-[64px]"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="ws-db">{t('ws.database')}</Label>

              {editing ? (
                <div className="mb-1 flex items-center gap-2">
                  <Switch id="ws-repoint" checked={repoint} onCheckedChange={setRepoint} />
                  <Label htmlFor="ws-repoint" className="text-bad">
                    {t('ws.repoint')}
                  </Label>
                </div>
              ) : null}

              <Input
                id="ws-db"
                dir="ltr"
                type="password"
                autoComplete="off"
                disabled={editing && !repoint}
                value={databaseUrl}
                placeholder={
                  editing ? workspace.data_source ?? '' : 'postgresql://user:pass@host/db'
                }
                onChange={(event) => setDatabaseUrl(event.target.value)}
              />
              <p className="text-[0.75rem] text-muted-foreground">
                {editing ? t('ws.repointHint') : t('ws.databaseHint')}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="ws-quota">{t('bill.questionsPerDay')}</Label>
                <Input
                  id="ws-quota"
                  type="number"
                  min="0"
                  dir="ltr"
                  value={dailyQuota}
                  placeholder={t('ws.default')}
                  onChange={(event) => setDailyQuota(event.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="ws-rows">{t('bill.rowsPerQuery')}</Label>
                <Input
                  id="ws-rows"
                  type="number"
                  min="1"
                  dir="ltr"
                  value={maxRows}
                  placeholder={t('ws.default')}
                  onChange={(event) => setMaxRows(event.target.value)}
                />
              </div>
            </div>

            {editing ? (
              <div className="flex flex-col gap-2 rounded-md border border-border bg-surface-3 p-3">
                <label className="flex items-center gap-2 text-[0.8125rem]">
                  <Switch checked={isActive} onCheckedChange={setIsActive} />
                  {t('ws.activeWorkspace')}
                </label>
                <label className="flex items-center gap-2 text-[0.8125rem]">
                  <Switch checked={allowWrites} onCheckedChange={setAllowWrites} />
                  {t('ws.writes')}
                </label>
                <label className="flex items-center gap-2 text-[0.8125rem]">
                  <Switch checked={allowByoKey} onCheckedChange={setAllowByoKey} />
                  {t('ws.ownKey')}
                </label>
                {/* A workspace also needs VANNA_ALLOW_WRITES on the deployment.
                    Saying so stops "I turned it on and nothing happened". */}
                <p className="text-[0.75rem] text-muted-foreground">{t('ws.writesHint')}</p>
              </div>
            ) : null}
          </div>

          <DialogFooter>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary" disabled={saving || !idValid}>
              {editing ? t('common.save') : t('common.create')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
