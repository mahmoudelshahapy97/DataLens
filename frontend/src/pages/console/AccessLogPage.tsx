import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';
import { relative } from '@/lib/time';
import type { AccessEvent } from '@/types';

import { DetailBlock } from './AuditPage';
import { ScopePicker, useConcreteScope } from './scope';

/**
 * What the agent did on users' behalf, and what was refused.
 *
 * A different question from the admin trail next door: that one is what
 * operators changed, this one is which tables the agent actually reached and
 * which grants stopped it. "Show me every refusal this week" is the query that
 * turns a grant matrix from a configuration screen into something you can
 * verify.
 *
 * Per workspace by nature -- `audit_events` is queried one workspace at a time,
 * so unlike the admin trail there is no platform-wide reading. When the console
 * scope is "all workspaces" this falls back to the caller's own and says so
 * rather than implying it is showing everything.
 */

const LIMITS = [50, 100, 250, 500];

export default function AccessLogPage() {
  const { t, locale } = useLocale();
  const tenant = useConcreteScope();

  const [deniedOnly, setDeniedOnly] = React.useState(false);
  const [limit, setLimit] = React.useState(100);
  const [expanded, setExpanded] = React.useState<string | null>(null);

  const [events, setEvents] = React.useState<AccessEvent[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(
    async (alive: () => boolean = () => true) => {
      const params = new URLSearchParams({ limit: String(limit) });
      if (tenant) params.set('tenant_id', tenant);
      if (deniedOnly) params.set('denied_only', 'true');
      try {
        const body = await api<{ events: AccessEvent[] }>(
          `/api/vanna/v2/admin/access-log?${params}`,
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
    [tenant, deniedOnly, limit],
  );

  React.useEffect(() => {
    setLoading(true);
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  return (
    <PageBody>
      <PageHeader
        title={t('nav.accessLog')}
        description={t('access.blurb', { workspace: tenant })}
      />

      <Toolbar>
        <ScopePicker id="access-scope" />

        <div className="flex items-center gap-2">
          <Switch id="access-denied" checked={deniedOnly} onCheckedChange={setDeniedOnly} />
          <Label htmlFor="access-denied">{t('audit.deniedOnly')}</Label>
        </div>

        <div className="flex items-center gap-2">
          <Label htmlFor="access-limit">{t('audit.limit')}</Label>
          <Select value={String(limit)} onValueChange={(next) => setLimit(Number(next))}>
            <SelectTrigger id="access-limit" className="w-24">
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
        <EmptyState
          title={t('audit.none')}
          hint={deniedOnly ? t('access.noneDenied') : t('audit.noneHint')}
        />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('audit.time')}</Th>
                <Th>{t('audit.actor')}</Th>
                <Th>{t('audit.event')}</Th>
                <Th>{t('audit.tool')}</Th>
                <Th>{t('audit.outcome')}</Th>
              </Tr>
            </thead>
            <Tbody>
              {events.map((event) => (
                <React.Fragment key={event.event_id}>
                  <Tr
                    className="cursor-pointer"
                    onClick={() =>
                      setExpanded(expanded === event.event_id ? null : event.event_id)
                    }
                  >
                    <Td className="whitespace-nowrap text-muted-foreground" title={event.created_at}>
                      {relative(event.created_at, t, locale)}
                    </Td>
                    <Td dir="auto">{event.user_email ?? '—'}</Td>
                    <Td className="font-mono text-[0.78rem]">{event.event_type}</Td>
                    <Td className="font-mono text-[0.78rem]">{event.tool_name ?? '—'}</Td>
                    <Td>
                      {event.access_granted === false ? (
                        <Badge tone="err">{t('audit.denied')}</Badge>
                      ) : (
                        <Badge tone="ok">{t('audit.granted')}</Badge>
                      )}
                    </Td>
                  </Tr>
                  {expanded === event.event_id ? (
                    <tr>
                      <Td colSpan={5} className="pt-0">
                        <DetailBlock value={event.payload} />
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
