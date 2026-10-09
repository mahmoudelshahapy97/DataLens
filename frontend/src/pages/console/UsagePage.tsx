import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { StatTile } from '@/components/primitives/stat-tile';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Card } from '@/components/ui/card';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';
import { relative } from '@/lib/time';
import type { Spend, Usage } from '@/types';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * What one workspace asked, and what answering it cost.
 *
 * `spend` is platform-admin only and read separately, so a workspace admin sees
 * their volume without seeing the platform's margins. A failed read of it is not
 * an error state here -- for most callers it is the expected answer.
 */
/**
 * Exported so `ActivityPage` can host it as a tab beside the overview.
 *
 * The two screens showed the same five KPI tiles from two different endpoints,
 * under two nav entries with the same icon -- which is how somebody ends up
 * comparing "questions" on one against "questions" on the other and wondering
 * why they disagree. (They disagree legitimately: one is scoped to the picker,
 * the other to one workspace.) One page with two tabs makes the scope of each
 * number visible instead of leaving it to be inferred from which link was
 * clicked.
 *
 * `embedded` suppresses this screen's own header and scroll container, because
 * the host already provides both and nesting them gives a scroll area inside a
 * scroll area.
 */
export function UsageScreen({ embedded = false }: { embedded?: boolean } = {}) {
  const { t, locale } = useLocale();
  const tenant = useConcreteScope();

  const [usage, setUsage] = React.useState<Usage | null>(null);
  const [spend, setSpend] = React.useState<Spend | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setUsage(null);
    const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}`;
    try {
      setUsage(await api<Usage>(`${base}/usage`));
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
      return;
    }
    try {
      setSpend(await api<Spend>(`${base}/spend`));
    } catch {
      // Platform-admin only. Absent is the correct answer for everybody else.
      setSpend(null);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const number = (value: number | null | undefined) =>
    new Intl.NumberFormat(locale).format(value ?? 0);
  const usd = (value: number) =>
    new Intl.NumberFormat(locale, { style: 'currency', currency: 'USD' }).format(value);

  const rate =
    usage && usage.questions ? `${((usage.succeeded / usage.questions) * 100).toFixed(1)}%` : '—';

  const Shell = embedded ? React.Fragment : PageBody;

  return (
    <Shell>
      {embedded ? null : (
        <PageHeader title={t('nav.usage')} description={t('use.blurb', { workspace: tenant })} />
      )}

      <Toolbar>
        <ScopePicker id="usage-scope" />
      </Toolbar>

      {error ? (
        <ErrorState title={t('use.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : !usage ? (
        <LoadingCards count={4} />
      ) : (
        <>
          <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
            <StatTile label={t('kpi.questions')} value={number(usage.questions)}
              hint={t('ov.lastDays', { n: usage.window_days })} />
            <StatTile label={t('kpi.successRate')} value={rate}
              hint={t('ov.ofN', { n: number(usage.succeeded) })} />
            <StatTile label={t('kpi.members')} value={number(usage.members)} />
            <StatTile label={t('kpi.activeUsers')} value={number(usage.active_users)} />
            <StatTile label={t('ov.feedback')}
              value={`▲ ${number(usage.liked)}  ▼ ${number(usage.disliked)}`} />
          </div>

          <Card className="mb-3 p-4">
            <p className="text-[0.8125rem] text-muted-foreground">{t('ov.lastActivity')}</p>
            <p className="mt-1 text-[0.9rem]">
              {usage.last_activity ? relative(usage.last_activity, t, locale) : t('ov.never')}
            </p>
          </Card>

          {spend ? (
            <Card className="p-4">
              <h3 className="mb-1 text-[0.9rem] font-semibold">{t('use.spend')}</h3>
              <p className="mb-3 text-[0.8125rem] text-muted-foreground">{t('use.spendHint')}</p>

              <div className="mb-3 grid gap-3 sm:grid-cols-3">
                <StatTile label={t('kpi.spend')} value={usd(spend.cost_usd)} />
                <StatTile label={t('use.promptTokens')} value={number(spend.prompt_tokens)} />
                <StatTile label={t('use.completionTokens')} value={number(spend.completion_tokens)} />
              </div>

              {spend.by_model && spend.by_model.length > 0 ? (
                <ScrollX>
                  <DataTable>
                    <thead>
                      <Tr>
                        <Th>{t('use.model')}</Th>
                        <Th>{t('kpi.questions')}</Th>
                        <Th>{t('kpi.spend')}</Th>
                      </Tr>
                    </thead>
                    <Tbody>
                      {spend.by_model.map((row) => (
                        <Tr key={row.model}>
                          <Td className="font-mono text-[0.78rem]">{row.model}</Td>
                          <Td>{number(row.questions)}</Td>
                          <Td>{usd(row.cost_usd)}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </DataTable>
                </ScrollX>
              ) : (
                <EmptyState title={t('use.noSpend')} hint={t('use.noSpendHint')} />
              )}
            </Card>
          ) : null}
        </>
      )}
    </Shell>
  );
}

export default function UsagePage() {
  return <UsageScreen />;
}
