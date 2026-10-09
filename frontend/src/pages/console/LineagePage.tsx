import { GitBranch, Search } from 'lucide-react';
import * as React from 'react';

import { PageBody, PageHeader } from '@/components/primitives/page';
import { StatTile } from '@/components/primitives/stat-tile';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';
import { toastError } from '@/lib/toast';

/**
 * Which tables feed which saved queries, dashboards and reports.
 *
 * The graph is *derived* on each request from rows the system already keeps --
 * saved query SQL, dashboard tile queries, report schedules -- rather than
 * recorded into a lineage table by a hook somebody has to remember to call. It
 * therefore cannot be stale, and it cannot be empty because a recorder was never
 * wired up.
 *
 * Rendered as columns rather than a node-link diagram. The relationships here are
 * layered by nature (table -> query -> dashboard -> report) and a force-directed
 * graph of forty tables is a hairball that answers no question; four columns and
 * an impact search answer the two questions people actually have.
 */

interface LineageNode {
  id: string;
  kind: string;
  label: string;
  missing?: boolean;
}

interface LineageEdge {
  source: string;
  target: string;
  relation: string;
}

interface Graph {
  nodes: LineageNode[];
  edges: LineageEdge[];
  counts: { tables: number; saved: number; dashboards: number; reports: number };
}

interface Impact {
  table: string;
  found: boolean;
  candidates: string[];
  affected: Array<{ id: string; kind: string; label: string }>;
}

const LAYERS: Array<{ kind: string; labelKey: string }> = [
  { kind: 'table', labelKey: 'lineage.tables' },
  { kind: 'saved', labelKey: 'nav.saved' },
  { kind: 'dashboard', labelKey: 'nav.dashboards' },
  { kind: 'report', labelKey: 'nav.reports' },
];

/** Node *kind*, not a verdict -- a category is not a severity, so this never
 * resolves to 'warn' or 'bad'. */
function toneFor(kind: string): 'accent' | 'good' | 'info' | 'neutral' {
  if (kind === 'table') return 'neutral';
  if (kind === 'saved') return 'accent';
  if (kind === 'dashboard') return 'good';
  return 'info'; // 'report'
}

export default function LineagePage() {
  const { t } = useLocale();

  const [graph, setGraph] = React.useState<Graph | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const [table, setTable] = React.useState('');
  const [impact, setImpact] = React.useState<Impact | null>(null);
  const [checking, setChecking] = React.useState(false);

  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const body = await api<Graph>('/api/vanna/v2/lineage');
        if (current) {
          setGraph(body);
          setError(null);
        }
      } catch (caught) {
        if (current) setError((caught as Error).message);
      } finally {
        if (current) setLoading(false);
      }
    })();
    return () => {
      current = false;
    };
  }, []);

  async function checkImpact(event: React.FormEvent) {
    event.preventDefault();
    if (!table.trim()) return;
    setChecking(true);
    try {
      setImpact(
        await api<Impact>(`/api/vanna/v2/lineage/impact/${encodeURIComponent(table.trim())}`),
      );
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setChecking(false);
    }
  }

  const byLayer = React.useMemo(() => {
    const grouped: Record<string, LineageNode[]> = {};
    for (const node of graph?.nodes ?? []) {
      (grouped[node.kind] ??= []).push(node);
    }
    return grouped;
  }, [graph]);

  /** Everything one node feeds, for the hover/expand list. */
  const downstream = React.useMemo(() => {
    const map: Record<string, string[]> = {};
    const labels = new Map((graph?.nodes ?? []).map((n) => [n.id, n.label]));
    for (const edge of graph?.edges ?? []) {
      (map[edge.source] ??= []).push(labels.get(edge.target) ?? edge.target);
    }
    return map;
  }, [graph]);

  return (
    <PageBody>
      <PageHeader title={t('nav.lineage')} description={t('lineage.sub')} />

      {loading ? (
        <LoadingRows rows={8} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} />
      ) : !graph || graph.nodes.length === 0 ? (
        <EmptyState
          icon={<GitBranch className="size-7" />}
          title={t('lineage.empty')}
          hint={t('lineage.emptyHint')}
        />
      ) : (
        <>
          <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile label={t('lineage.tables')} value={graph.counts.tables} />
            <StatTile label={t('nav.saved')} value={graph.counts.saved} tone="accent" />
            <StatTile label={t('nav.dashboards')} value={graph.counts.dashboards} tone="good" />
            <StatTile label={t('nav.reports')} value={graph.counts.reports} tone="warn" />
          </div>

          <Card className="mb-4 p-4">
            <form className="flex flex-wrap items-end gap-2" onSubmit={checkImpact}>
              <div className="flex min-w-[240px] flex-1 flex-col gap-1.5">
                <label htmlFor="impact-table" className="text-[0.8125rem] font-medium text-muted-foreground">
                  {t('lineage.impactOf')}
                </label>
                <Input
                  id="impact-table"
                  dir="ltr"
                  value={table}
                  placeholder={t('lineage.tablePlaceholder')}
                  onChange={(event) => setTable(event.target.value)}
                />
              </div>
              <Button type="submit" variant="primary" disabled={checking}>
                <Search />
                {t('lineage.check')}
              </Button>
            </form>

            {impact ? (
              <div className="mt-3 border-t border-border-soft pt-3">
                {!impact.found ? (
                  <p className="text-[0.8125rem] text-muted-foreground">
                    {impact.candidates.length
                      ? t('lineage.ambiguous', { names: impact.candidates.join(', ') })
                      : t('lineage.notFound', { table: impact.table })}
                  </p>
                ) : impact.affected.length === 0 ? (
                  <p className="text-[0.8125rem] text-muted-foreground">
                    {t('lineage.noImpact', { table: impact.table })}
                  </p>
                ) : (
                  <>
                    <p className="mb-2 text-[0.8125rem]">
                      {t('lineage.impactSummary', {
                        table: impact.table,
                        n: impact.affected.length,
                      })}
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {impact.affected.map((node) => (
                        <Badge key={node.id} tone={toneFor(node.kind)}>
                          {node.label}
                        </Badge>
                      ))}
                    </div>
                  </>
                )}
              </div>
            ) : null}
          </Card>

          <div className="grid gap-3 lg:grid-cols-4">
            {LAYERS.map((layer) => {
              const nodes = byLayer[layer.kind] ?? [];
              return (
                <Card key={layer.kind} className="p-3">
                  <h3 className="mb-2 text-[0.6875rem] font-semibold uppercase tracking-wider text-muted-foreground">
                    {t(layer.labelKey)} ({nodes.length})
                  </h3>
                  <ul className="flex max-h-[52vh] flex-col gap-1 overflow-y-auto">
                    {nodes.map((node) => (
                      <li
                        key={node.id}
                        className="rounded-md border border-border-soft bg-surface-2 px-2 py-1.5"
                        title={(downstream[node.id] ?? []).join('\n')}
                      >
                        <span
                          className={[
                            'block truncate text-[0.78rem]',
                            layer.kind === 'table' ? 'font-mono' : '',
                            node.missing ? 'text-bad' : '',
                          ].join(' ')}
                          dir="auto"
                        >
                          {node.label}
                        </span>
                        {downstream[node.id]?.length ? (
                          <span className="text-[0.7rem] text-muted-foreground">
                            {t('lineage.feeds', { n: downstream[node.id].length })}
                          </span>
                        ) : null}
                      </li>
                    ))}
                    {nodes.length === 0 ? (
                      <li className="px-2 py-1.5 text-[0.78rem] text-muted-foreground">
                        {t('lineage.none')}
                      </li>
                    ) : null}
                  </ul>
                </Card>
              );
            })}
          </div>
        </>
      )}
    </PageBody>
  );
}
