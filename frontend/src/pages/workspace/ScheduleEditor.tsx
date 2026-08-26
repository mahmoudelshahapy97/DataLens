import { Plus, X } from 'lucide-react';
import * as React from 'react';

import { useSession } from '@/app/session';
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
import { useLocale } from '@/i18n';
import { api, patch, post } from '@/lib/api';
import { toastError } from '@/lib/toast';
import type { Dashboard } from '@/types';

export interface ReportChannel {
  kind: 'email' | 'webhook' | 'inapp';
  target: string;
}

export interface ReportSchedule {
  id: string;
  dashboard_id: string;
  dashboard_title?: string;
  name: string;
  cron: string;
  schedule_label?: string;
  timezone: string;
  run_as: string;
  channels: ReportChannel[];
  parameters: Record<string, string>;
  format: string;
  is_active: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
}

interface Meta {
  presets: Array<{ cron: string; label: string }>;
  timezones: string[];
  formats: string[];
  channels: string[];
  allow_external_recipients: boolean;
}

/**
 * Creating or editing a schedule.
 *
 * The presets, timezones and formats come from `GET /reports/meta` rather than
 * being listed here. That is deliberate: the backend refuses a cron expression
 * it cannot parse, and a form offering a preset the backend would refuse is a
 * form that produces an error message instead of a report.
 *
 * **Runs as** is the field with consequences. Tiles execute with that member's
 * permissions, so it decides whose view of the data gets delivered. Only a
 * workspace admin may set it to somebody else; for everybody else it is fixed to
 * themselves and shown read-only, which is the same rule the API enforces.
 */
export function ScheduleEditor({
  schedule,
  onClose,
  onSaved,
}: {
  schedule: ReportSchedule | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useLocale();
  const { me, isWorkspaceAdmin } = useSession();
  const self = me?.user.email ?? '';

  const [meta, setMeta] = React.useState<Meta | null>(null);
  const [dashboards, setDashboards] = React.useState<Dashboard[]>([]);
  const [members, setMembers] = React.useState<string[]>([]);
  const [saving, setSaving] = React.useState(false);

  const [name, setName] = React.useState(schedule?.name ?? '');
  const [dashboardId, setDashboardId] = React.useState(schedule?.dashboard_id ?? '');
  const [cron, setCron] = React.useState(schedule?.cron ?? '0 8 * * *');
  const [timezone, setTimezone] = React.useState(schedule?.timezone ?? 'UTC');
  const [runAs, setRunAs] = React.useState(schedule?.run_as ?? self);
  const [format, setFormat] = React.useState(schedule?.format ?? 'html');
  const [channels, setChannels] = React.useState<ReportChannel[]>(
    schedule?.channels?.length ? schedule.channels : [{ kind: 'email', target: self }],
  );

  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const [metaBody, dashBody] = await Promise.all([
          api<Meta>('/api/vanna/v2/reports/meta'),
          api<{ dashboards: Array<{ document: Dashboard }> }>('/api/vanna/v2/dashboards'),
        ]);
        if (!current) return;
        setMeta(metaBody);
        const list = (dashBody.dashboards ?? []).map((row) => row.document);
        setDashboards(list);
        setDashboardId((existing) => existing || list[0]?.id || '');
      } catch (caught) {
        if (current) toastError((caught as Error).message);
      }

      // Only an admin can pick somebody else, so only an admin needs the roster.
      if (!isWorkspaceAdmin || !me?.tenant?.id) return;
      try {
        const body = await api<{ users: Array<{ email: string }> }>(
          `/api/vanna/v2/tenants/${me.tenant.id}/users`,
        );
        if (current) setMembers((body.users ?? []).map((u) => u.email));
      } catch {
        // The roster is a convenience; the field still accepts a typed address.
      }
    })();
    return () => {
      current = false;
    };
  }, [isWorkspaceAdmin, me?.tenant?.id]);

  const dashboard = dashboards.find((d) => d.id === dashboardId);

  async function save() {
    setSaving(true);
    const payload = {
      dashboard_id: dashboardId,
      name: name || dashboard?.title || 'Report',
      cron,
      timezone,
      run_as: runAs,
      format,
      channels: channels.filter((channel) => channel.target.trim()),
      parameters: schedule?.parameters ?? {},
    };
    try {
      if (schedule) await patch(`/api/vanna/v2/reports/${schedule.id}`, payload);
      else await post('/api/vanna/v2/reports', payload);
      onSaved();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-[640px]">
        <DialogHeader>
          <DialogTitle>{schedule ? t('report.edit') : t('report.new')}</DialogTitle>
          <DialogDescription>{t('report.editorHint')}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <Field label={t('report.dashboard')} htmlFor="rep-dashboard">
            <Select value={dashboardId} onValueChange={setDashboardId}>
              <SelectTrigger id="rep-dashboard">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {dashboards.map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.title}
                    {d.parameters?.length ? ` (${d.parameters.length})` : ''}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label={t('report.name')} htmlFor="rep-name">
            <Input
              id="rep-name"
              value={name}
              placeholder={dashboard?.title ?? ''}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t('report.schedule')} htmlFor="rep-cron">
              <Select value={cron} onValueChange={setCron}>
                <SelectTrigger id="rep-cron">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(meta?.presets ?? []).map((preset) => (
                    <SelectItem key={preset.cron} value={preset.cron}>
                      {preset.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label={t('report.timezone')} htmlFor="rep-tz">
              <Select value={timezone} onValueChange={setTimezone}>
                <SelectTrigger id="rep-tz">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(meta?.timezones ?? ['UTC']).map((zone) => (
                    <SelectItem key={zone} value={zone}>
                      {zone}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          <Field label={t('report.runsAs')} htmlFor="rep-runas" hint={t('report.runsAsHint')}>
            {isWorkspaceAdmin && members.length ? (
              <Select value={runAs} onValueChange={setRunAs}>
                <SelectTrigger id="rep-runas">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {members.map((email) => (
                    <SelectItem key={email} value={email}>
                      {email}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              // Not an admin: the API refuses a run_as that is not the caller, so
              // the field is shown fixed rather than offering a choice that would
              // be rejected on save.
              <Input id="rep-runas" value={runAs} readOnly disabled />
            )}
          </Field>

          <Field label={t('report.format')} htmlFor="rep-format">
            <Select value={format} onValueChange={setFormat}>
              <SelectTrigger id="rep-format">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(meta?.formats ?? ['html']).map((option) => (
                  <SelectItem key={option} value={option}>
                    {option.toUpperCase()}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <div className="flex flex-col gap-2">
            <Label>{t('report.channels')}</Label>
            {!meta?.allow_external_recipients ? (
              <p className="text-[0.75rem] text-muted-foreground">
                {t('report.membersOnly')}
              </p>
            ) : null}

            {channels.map((channel, index) => (
              <div key={index} className="flex items-center gap-2">
                <Select
                  value={channel.kind}
                  onValueChange={(next) =>
                    setChannels((current) =>
                      current.map((c, i) =>
                        i === index ? { ...c, kind: next as ReportChannel['kind'] } : c,
                      ),
                    )
                  }
                >
                  <SelectTrigger className="w-32" aria-label={t('report.channels')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(meta?.channels ?? ['email']).map((kind) => (
                      <SelectItem key={kind} value={kind}>
                        {t(`report.channel.${kind}`)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>

                <Input
                  className="flex-1"
                  dir="ltr"
                  value={channel.target}
                  placeholder={
                    channel.kind === 'webhook' ? 'https://hooks.example.com/...' : self
                  }
                  aria-label={t('report.recipients')}
                  onChange={(event) =>
                    setChannels((current) =>
                      current.map((c, i) =>
                        i === index ? { ...c, target: event.target.value } : c,
                      ),
                    )
                  }
                />

                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={t('common.remove')}
                  onClick={() =>
                    setChannels((current) => current.filter((_, i) => i !== index))
                  }
                >
                  <X />
                </Button>
              </div>
            ))}

            <Button
              className="self-start"
              size="sm"
              onClick={() =>
                setChannels((current) => [...current, { kind: 'email', target: '' }])
              }
            >
              <Plus />
              {t('report.addChannel')}
            </Button>
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" disabled={saving || !dashboardId} onClick={() => void save()}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint ? <p className="text-[0.75rem] text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
