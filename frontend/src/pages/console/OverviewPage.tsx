import { Database } from 'lucide-react';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';

import { PlotlyChart, type PlotlyFigure } from '@/components/PlotlyChart';
import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { StatTile } from '@/components/primitives/stat-tile';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
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
import type { DataSourceHealth, Overview } from '@/types';

import { ScopePicker, useConsoleScope } from './scope';

/**
 * How the platform -- or one workspace -- is doing.
 *
 * Every number here was already being collected. `generations` has carried
 * per-question status, feedback, model and cost since the beginning and
 * `list_tenants_with_usage` already answered "how is each workspace doing" in a
 * single query; what was missing was a screen that asked. The console opened on
 * the review queue, so "is anything wrong" was a question you answered by
 * reading tabs.
 *
 * One request paints the whole screen: `routes/overview.py` gathers the pieces
 * concurrently rather than making the browser make five round trips.
 */

const WINDOWS = [7, 30, 90];
const REFRESH_MS = 30_000;

/**
 * Exported so `ActivityPage` can host it as a tab beside the cost view.
 * See the note on `UsageScreen` for why the two were merged.
 */
export function OverviewScreen({ embedded = false }: { embedded?: boolean } = {}) {
  const { t, locale } = useLocale();
  const navigate = useNavigate();
  const { scope } = useConsoleScope();

  const [days, setDays] = React.useState(30);
  const [auto, setAuto] = React.useState(false);
  const [data, setData] = React.useState<Overview | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(
    async (alive: () => boolean = () => true) => {
      const params = new URLSearchParams({ days: String(days) });
      if (scope) params.set('tenant_id', scope);
      try {
        const next = await api<Overview>(`/api/vanna/v2/admin/overview?${params}`);
        if (!alive()) return;
        setData(next);
        setError(null);
      } catch (caught) {
        if (!alive()) return;
        setError((caught as Error).message);
      } finally {
        if (alive()) setLoading(false);
      }
    },
    [days, scope],
  );

  React.useEffect(() => {
    setLoading(true);
    // Guards against an older request landing after a newer one: changing the
    // window or the workspace twice quickly leaves two in flight, and the
    // slower would paint the previous filter's numbers under the new controls.
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  // The poll is cleared by the effect's own teardown, so leaving the screen or
  // changing the window can never leave a second one running against a
  // component that has unmounted.
  React.useEffect(() => {
    if (!auto) return undefined;
    const handle = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(handle);
  }, [auto, load]);

  const number = React.useCallback(
    (value: number | null | undefined) => new Intl.NumberFormat(locale).format(value ?? 0),
    [locale],
  );

  const Shell = embedded ? React.Fragment : PageBody;
  const Head = embedded
    ? null
    : <PageHeader title={t('nav.overview')} description={t('ov.blurb')} />;

  if (loading && !data) {
    return (
      <Shell>
        {Head}
        <LoadingCards count={5} />
      </Shell>
    );
  }

  if (error && !data) {
    return (
      <Shell>
        {Head}
        <ErrorState
          title={t('ov.failed')}
          detail={error}
          onRetry={() => void load()}
          retryLabel={t('common.retry')}
        />
      </Shell>
    );
  }

  if (!data) return null;

  const { kpis } = data;
  const platformWide = data.scope === '';

  return (
    <Shell>
      {Head}

      <Toolbar>
        <ScopePicker id="ov-scope" concrete={false} />

        <div className="flex items-center gap-2">
          <Label htmlFor="ov-window">{t('ov.window')}</Label>
          <Select value={String(days)} onValueChange={(next) => setDays(Number(next))}>
            <SelectTrigger id="ov-window" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {WINDOWS.map((n) => (
                <SelectItem key={n} value={String(n)}>
                  {t('ov.lastDays', { n })}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex items-center gap-2">
          <Switch id="ov-auto" checked={auto} onCheckedChange={setAuto} />
          <Label htmlFor="ov-auto">{t('ov.autoRefresh')}</Label>
        </div>
      </Toolbar>

      <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        {platformWide ? (
          <StatTile
            label={t('kpi.workspaces')}
            value={number(kpis.workspaces)}
            hint={t('ov.activeOf', { n: number(kpis.active_workspaces) })}
          />
        ) : (
          <StatTile label={t('kpi.members')} value={number(kpis.members)} />
        )}
        <StatTile
          label={t('kpi.questions')}
          value={number(kpis.questions)}
          hint={t('ov.lastDays', { n: data.window_days })}
        />
        <StatTile
          label={t('kpi.successRate')}
          value={formatRate(kpis.success_rate)}
          hint={t('ov.ofN', { n: number(kpis.succeeded) })}
          tone={rateTone(kpis.success_rate)}
        />
        <StatTile label={t('kpi.activeUsers')} value={number(kpis.active_users)} />
        {/* Omitted rather than zeroed for a workspace admin: "we do not show you
            this" and "you spent nothing" are different answers. */}
        {kpis.cost_usd === undefined ? null : (
          <StatTile
            label={t('kpi.spend')}
            value={new Intl.NumberFormat(locale, {
              style: 'currency',
              currency: 'USD',
            }).format(kpis.cost_usd)}
          />
        )}
      </div>

      {/* `min-w-0` on every card in this grid, and it is load-bearing.
          A grid item defaults to `min-width: auto`, which means it refuses to
          shrink below its content's intrinsic width -- so the `overflow-x: auto`
          on ScrollX inside never engages and a wide table is simply clipped at
          the card's edge with no way to scroll to the rest. The Data sources
          table lost its last two columns to exactly this. Plotly is the same
          story from the other direction: it measures its container, and a
          container that will not shrink reports the wrong width. */}
      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <Card className="min-w-0 p-4">
          <h3 className="mb-2 text-[0.9rem] font-semibold">{t('ov.questionsPerDay')}</h3>
          <PlotlyChart
            figure={seriesFigure(data, t('ov.questions'), t('ov.succeeded'))}
            label={t('ov.questionsPerDay')}
            className="h-60"
          />
        </Card>
        <Card className="min-w-0 p-4">
          <h3 className="mb-2 text-[0.9rem] font-semibold">
            {platformWide ? t('ov.byWorkspace') : t('ov.feedback')}
          </h3>
          <PlotlyChart
            figure={
              platformWide
                ? workspaceFigure(data)
                : feedbackFigure(data, [t('ov.liked'), t('ov.disliked'), t('ov.unrated')])
            }
            label={platformWide ? t('ov.byWorkspace') : t('ov.feedback')}
            className="h-60"
          />
        </Card>
      </div>

      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <Card className="min-w-0 p-4">
          <h3 className="mb-2 text-[0.9rem] font-semibold">{t('ov.recentActivity')}</h3>
          {data.recent.length === 0 ? (
            <EmptyState title={t('ov.noActivity')} />
          ) : (
            <ul className="divide-y divide-border-soft">
              {data.recent.map((event) => (
                <li key={event.id}>
                  <button
                    type="button"
                    onClick={() => navigate('/console/audit')}
                    className="flex w-full flex-wrap items-baseline gap-x-2.5 gap-y-1 py-2 text-start hover:text-primary"
                  >
                    <span className="font-mono text-[0.78rem] font-semibold">{event.action}</span>
                    {event.target ? (
                      <span className="font-mono text-[0.75rem] text-muted-foreground" dir="auto">
                        {event.target}
                      </span>
                    ) : null}
                    <span className="text-[0.75rem] text-muted-foreground">
                      {event.actor_email}
                    </span>
                    <span className="ms-auto text-[0.72rem] text-muted-foreground">
                      {relative(event.created_at, t, locale)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card className="min-w-0 p-4">
          <h3 className="mb-2 text-[0.9rem] font-semibold">{t('ov.health')}</h3>
          <HealthPanel sources={data.data_sources} platformWide={platformWide} />
        </Card>
      </div>

      {platformWide && data.workspaces ? (
        <Card className="min-w-0 p-4">
          <h3 className="mb-2 text-[0.9rem] font-semibold">{t('ov.perWorkspace')}</h3>
          <ScrollX>
            <DataTable>
              <thead>
                <Tr>
                  <Th>{t('ov.workspace')}</Th>
                  <Th>{t('kpi.members')}</Th>
                  <Th>{t('kpi.questions')}</Th>
                  <Th>{t('kpi.successRate')}</Th>
                  <Th>{t('ov.feedback')}</Th>
                  <Th>{t('ov.lastActivity')}</Th>
                </Tr>
              </thead>
              <Tbody>
                {[...data.workspaces]
                  .sort((a, b) => b.usage.questions - a.usage.questions)
                  .map((workspace) => (
                    <Tr key={workspace.id}>
                      <Td>
                        <span className="font-medium">{workspace.name}</span>
                        {workspace.is_active ? null : (
                          <Badge tone="err" className="ms-2">
                            {t('ov.inactive')}
                          </Badge>
                        )}
                        <span className="block font-mono text-[0.72rem] text-muted-foreground">
                          {workspace.id}
                        </span>
                      </Td>
                      <Td>{number(workspace.usage.members)}</Td>
                      <Td>{number(workspace.usage.questions)}</Td>
                      <Td>
                        {formatRate(
                          workspace.usage.questions
                            ? workspace.usage.succeeded / workspace.usage.questions
                            : null,
                        )}
                      </Td>
                      <Td className="whitespace-nowrap">
                        <span className="text-good">▲ {number(workspace.usage.liked)}</span>{' '}
                        <span className="text-bad">▼ {number(workspace.usage.disliked)}</span>
                      </Td>
                      <Td className="text-muted-foreground">
                        {workspace.usage.last_activity
                          ? relative(workspace.usage.last_activity, t, locale)
                          : t('ov.never')}
                      </Td>
                    </Tr>
                  ))}
              </Tbody>
            </DataTable>
          </ScrollX>
        </Card>
      ) : null}
    </Shell>
  );
}

export default function OverviewPage() {
  return <OverviewScreen />;
}

function formatRate(rate: number | null | undefined): string {
  // Not "0%". A window nobody asked a question in has no success rate, and
  // rendering one as zero reads as a platform failing every request.
  return rate === null || rate === undefined ? '—' : `${(rate * 100).toFixed(1)}%`;
}

function rateTone(rate: number | null | undefined): 'neutral' | 'good' | 'warn' | 'bad' {
  if (rate === null || rate === undefined) return 'neutral';
  if (rate >= 0.9) return 'good';
  return rate >= 0.7 ? 'warn' : 'bad';
}

function HealthPanel({
  sources,
  platformWide,
}: {
  sources: DataSourceHealth[] | undefined;
  platformWide: boolean;
}) {
  const { t, locale } = useLocale();

  // Absent rather than empty: this is a platform-admin surface, and a workspace
  // admin should be told it is not theirs rather than shown an empty list that
  // reads as "no databases".
  if (!sources) {
    return <p className="text-[0.8125rem] text-muted-foreground">{t('ov.healthPlatformOnly')}</p>;
  }
  if (sources.length === 0) {
    return (
      <EmptyState
        icon={<Database className="size-7" />}
        title={platformWide ? t('ov.allHealthy') : t('ov.noSources')}
      />
    );
  }

  return (
    <ScrollX>
      <DataTable>
        <thead>
          <Tr>
            {platformWide ? <Th>{t('ov.workspace')}</Th> : null}
            <Th>{t('ov.source')}</Th>
            <Th>{t('ov.status')}</Th>
            <Th>{t('ov.checked')}</Th>
          </Tr>
        </thead>
        <Tbody>
          {sources.map((source) => (
            <Tr key={`${source.tenant_id ?? ''}:${source.data_source_id}`}>
              {platformWide ? (
                <Td className="font-mono text-[0.75rem]">{source.tenant_id}</Td>
              ) : null}
              <Td className="font-mono text-[0.75rem]">{source.label}</Td>
              <Td>
                <HealthBadge lastOk={source.last_ok} />
                {source.last_error ? (
                  <span className="mt-1 block text-[0.72rem] text-muted-foreground" dir="auto">
                    {source.last_error}
                  </span>
                ) : null}
              </Td>
              <Td className="text-muted-foreground">
                {source.last_checked_at
                  ? relative(source.last_checked_at, t, locale)
                  : t('ov.never')}
              </Td>
            </Tr>
          ))}
        </Tbody>
      </DataTable>
    </ScrollX>
  );
}

/**
 * `last_ok` has three states and the third is the one that matters: a source
 * nobody has checked is *unknown*, and rendering that as "OK" is how a rotated
 * password looks healthy right up until somebody asks a question.
 */
function HealthBadge({ lastOk }: { lastOk: boolean | null }) {
  const t = useLocale().t;
  if (lastOk === true) return <Badge tone="ok">{t('ov.healthOk')}</Badge>;
  if (lastOk === false) return <Badge tone="err">{t('ov.healthFailing')}</Badge>;
  return <Badge tone="warn">{t('ov.healthUnknown')}</Badge>;
}

// ----------------------------------------------------------------------
// Figures
// ----------------------------------------------------------------------

/**
 * Charts are built here rather than through `tileFigure()`.
 *
 * That module is the single source of truth for a *dashboard tile*, and it is
 * mirrored into the backend so an offline export draws the same picture as the
 * screen. These three are operator furniture over a fixed shape the API
 * guarantees, not user-defined tiles, and routing them through the tile builder
 * would mean teaching it about workspace usage.
 */
const CHART_HEIGHT = 240;

const BASE_LAYOUT = {
  // Explicit, and it has to be: `.plotly-div` has a 400px `min-height` floor,
  // and the element only lowers it when the layout names a height. Omit this and
  // the plot is drawn 400px tall inside a 240px card and spills over the panel
  // underneath it.
  height: CHART_HEIGHT,
  margin: { t: 12, r: 12, b: 34, l: 46 },
  // The card already paints a surface; an opaque plot background sits on it as a
  // lighter rectangle in dark mode and a darker one in light.
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  showlegend: false,
  autosize: true,
};

const CONFIG = { displayModeBar: false, responsive: true };

function seriesFigure(data: Overview, questions: string, succeeded: string): PlotlyFigure {
  const days = data.series.map((point) => point.day);
  return {
    traces: [
      {
        type: 'scatter',
        mode: 'lines',
        name: questions,
        x: days,
        y: data.series.map((point) => point.questions),
        line: { width: 2 },
        fill: 'tozeroy',
      },
      {
        type: 'scatter',
        mode: 'lines',
        name: succeeded,
        x: days,
        y: data.series.map((point) => point.succeeded),
        line: { width: 1.5, dash: 'dot' },
      },
    ],
    layout: { ...BASE_LAYOUT, showlegend: true, legend: { orientation: 'h', y: -0.25 } },
    config: CONFIG,
  };
}

function workspaceFigure(data: Overview): PlotlyFigure {
  // Busiest first, and only as many as fit: a bar per workspace is unreadable
  // past a dozen, and this panel is a ranking rather than an inventory.
  const top = [...(data.workspaces ?? [])]
    .sort((a, b) => b.usage.questions - a.usage.questions)
    .slice(0, 8)
    .reverse();
  return {
    traces: [
      {
        type: 'bar',
        orientation: 'h',
        y: top.map((workspace) => workspace.name),
        x: top.map((workspace) => workspace.usage.questions),
      },
    ],
    layout: { ...BASE_LAYOUT, margin: { t: 12, r: 12, b: 34, l: 130 } },
    config: CONFIG,
  };
}

function feedbackFigure(data: Overview, labels: string[]): PlotlyFigure {
  const { liked, disliked, questions } = data.kpis;
  return {
    traces: [
      {
        type: 'bar',
        x: labels,
        y: [liked, disliked, Math.max(0, questions - liked - disliked)],
        // Green and red are the right reading here: this is the one chart in the
        // product where the two colours literally mean approval and rejection.
        marker: { color: ['var(--good)', 'var(--bad)', 'var(--muted-foreground)'] },
      },
    ],
    layout: BASE_LAYOUT,
    config: CONFIG,
  };
}
