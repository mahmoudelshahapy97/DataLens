import { Download } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Button } from '@/components/ui/button';
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
import { api, download } from '@/lib/api';
import { relative } from '@/lib/time';
import { toastError, toastSuccess } from '@/lib/toast';
import type { AuditEvent } from '@/types';

import { ScopePicker, useConsoleScope } from './scope';

/**
 * What operators did to the system.
 *
 * `admin_audit` has recorded every privileged mutation since the beginning and
 * nothing ever read it back: the trail was write-only, which is the same as not
 * having one the first time somebody asks what happened.
 *
 * The action vocabulary is served by `/admin/overview` rather than hardcoded
 * here, so the filter cannot offer an action the writer does not accept -- or
 * miss one it started accepting last week.
 */

const ALL_ACTIONS = '__all__';
const LIMITS = [50, 100, 250, 500];

export default function AuditPage() {
  const { t, locale } = useLocale();
  const { scope } = useConsoleScope();

  const [action, setAction] = React.useState('');
  const [actor, setActor] = React.useState('');
  const [limit, setLimit] = React.useState(100);
  const [expanded, setExpanded] = React.useState<string | null>(null);

  const [events, setEvents] = React.useState<AuditEvent[]>([]);
  const [actions, setActions] = React.useState<string[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const query = React.useCallback(() => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (scope) params.set('tenant_id', scope);
    if (action) params.set('action', action);
    if (actor) params.set('actor_email', actor);
    return params;
  }, [scope, action, actor, limit]);

  const load = React.useCallback(
    async (alive: () => boolean = () => true) => {
      try {
        const body = await api<{ events: AuditEvent[] }>(
          `/api/vanna/v2/admin/audit?${query()}`,
        );
        if (!alive()) return;
        setEvents(body.events ?? []);
        setError(null);
      } catch (caught) {
        if (!alive()) return;
        setError((caught as Error).message);
      } finally {
        if (alive()) setLoading(false);
      }
    },
    [query],
  );

  React.useEffect(() => {
    setLoading(true);
    // An older response must not land after a newer one; see the note in
    // OverviewPage. Typing in the actor box is the fast path into this.
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  // The vocabulary, fetched once per scope. days=1 because this call is only
  // wanted for `actions` and there is no reason to make the database roll up a
  // month to answer it.
  React.useEffect(() => {
    void (async () => {
      const params = new URLSearchParams({ days: '1' });
      if (scope) params.set('tenant_id', scope);
      try {
        const body = await api<{ actions: string[] }>(
          `/api/vanna/v2/admin/overview?${params}`,
        );
        setActions(body.actions ?? []);
      } catch {
        // A filter with no options is a usable screen; a blank one is not.
        setActions([]);
      }
    })();
  }, [scope]);

  const exportCsv = async () => {
    try {
      await download(`/api/vanna/v2/admin/audit.csv?${query()}`, 'audit.csv');
      toastSuccess(t('audit.exported'));
    } catch {
      toastError(t('audit.exportFailed'));
    }
  };

  const reset = () => {
    setAction('');
    setActor('');
    setExpanded(null);
  };

  return (
    <PageBody>
      <PageHeader
        title={t('nav.audit')}
        description={t('audit.blurb')}
        actions={
          <Button onClick={() => void exportCsv()}>
            <Download />
            {t('audit.export')}
          </Button>
        }
      />

      <Toolbar>
        <ScopePicker id="audit-scope" concrete={false} />

        <div className="flex items-center gap-2">
          <Label htmlFor="audit-action">{t('audit.action')}</Label>
          <Select
            value={action || ALL_ACTIONS}
            onValueChange={(next) => setAction(next === ALL_ACTIONS ? '' : next)}
          >
            <SelectTrigger id="audit-action" className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_ACTIONS}>{t('audit.allActions')}</SelectItem>
              {actions.map((name) => (
                <SelectItem key={name} value={name}>
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-2">
          <Label htmlFor="audit-actor">{t('audit.actor')}</Label>
          <Input
            id="audit-actor"
            type="search"
            className="w-52"
            placeholder={t('audit.actorHint')}
            defaultValue={actor}
            // On commit rather than on every keystroke: this refetches, and a
            // request per character is a request per character.
            onBlur={(event) => setActor(event.currentTarget.value.trim())}
            onKeyDown={(event) => {
              if (event.key === 'Enter') setActor(event.currentTarget.value.trim());
            }}
          />
        </div>

        <div className="flex items-center gap-2">
          <Label htmlFor="audit-limit">{t('audit.limit')}</Label>
          <Select value={String(limit)} onValueChange={(next) => setLimit(Number(next))}>
            <SelectTrigger id="audit-limit" className="w-24">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LIMITS.map((n) => (
                <SelectItem key={n} value={String(n)}>
                  {n}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Button variant="ghost" onClick={reset}>
          {t('audit.reset')}
        </Button>
      </Toolbar>

      {loading ? (
        <LoadingRows rows={8} />
      ) : error ? (
        <ErrorState
          title={t('audit.failed')}
          detail={error}
          onRetry={() => void load()}
          retryLabel={t('common.retry')}
        />
      ) : events.length === 0 ? (
        <EmptyState title={t('audit.none')} hint={t('audit.noneHint')} />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('audit.time')}</Th>
                <Th>{t('audit.actor')}</Th>
                <Th>{t('audit.action')}</Th>
                <Th>{t('ov.workspace')}</Th>
                <Th>{t('audit.target')}</Th>
                <Th>{t('audit.ip')}</Th>
              </Tr>
            </thead>
            <Tbody>
              {events.map((event) => (
                <React.Fragment key={event.id}>
                  <Tr
                    className="cursor-pointer"
                    onClick={() => setExpanded(expanded === event.id ? null : event.id)}
                  >
                    <Td className="whitespace-nowrap text-muted-foreground" title={event.created_at}>
                      {relative(event.created_at, t, locale)}
                    </Td>
                    <Td dir="auto">{event.actor_email ?? '—'}</Td>
                    <Td className="font-mono text-[0.78rem]">{event.action}</Td>
                    <Td className="font-mono text-[0.78rem]">{event.tenant_id ?? '—'}</Td>
                    <Td className="font-mono text-[0.78rem]" dir="auto">
                      {event.target ?? '—'}
                    </Td>
                    <Td className="font-mono text-[0.78rem] text-muted-foreground">
                      {event.actor_ip ?? '—'}
                    </Td>
                  </Tr>
                  {expanded === event.id ? (
                    <tr>
                      <Td colSpan={6} className="pt-0">
                        <DetailBlock value={event.details} />
                      </Td>
                    </tr>
                  ) : null}
                </React.Fragment>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}
    </PageBody>
  );
}

/**
 * The recorded detail, as JSON.
 *
 * Shown raw rather than prettified into fields: the shape differs per action and
 * an investigator wants exactly what was written, not this screen's reading of
 * it. The audit writer has already redacted anything credential-shaped by key
 * name before it reached the table.
 */
export function DetailBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface-2 p-3 text-[0.75rem] leading-relaxed">
      {JSON.stringify(value ?? {}, null, 2)}
    </pre>
  );
}
