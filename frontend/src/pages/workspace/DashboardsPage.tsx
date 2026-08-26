import { LayoutDashboard, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { useSession } from '@/app/session';
import { useConfirm } from '@/components/primitives/confirm';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api, del, post } from '@/lib/api';
import { relative } from '@/lib/time';
import { toast, toastError } from '@/lib/toast';
import type { Dashboard } from '@/types';

/**
 * The dashboard gallery.
 *
 * The description under the title is not decoration. "Two people can open the
 * same dashboard and see different numbers" is the single most surprising
 * property of this product -- tiles execute as the caller, so the row and column
 * rules that apply to a person's questions apply here too -- and somebody
 * reading a figure off a shared screen needs to know it.
 */

interface DashboardRow {
  id: string;
  title: string;
  document: Dashboard;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export default function DashboardsPage() {
  const { t, locale } = useLocale();
  const { canAuthor } = useSession();
  const confirm = useConfirm();
  const navigate = useNavigate();

  const [rows, setRows] = React.useState<DashboardRow[]>([]);
  const [filter, setFilter] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(async (alive: () => boolean = () => true) => {
    try {
      const body = await api<{ dashboards: DashboardRow[] }>('/api/vanna/v2/dashboards');
      if (!alive()) return;
      setRows(body.dashboards ?? []);
      setError(null);
    } catch (caught) {
      if (!alive()) return;
      setError((caught as Error).message);
    } finally {
      if (alive()) setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load]);

  const visible = React.useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) => (row.document?.title ?? row.title).toLowerCase().includes(needle));
  }, [rows, filter]);

  async function remove(row: DashboardRow) {
    const ok = await confirm.ask({
      title: t('dash.deleteTitle'),
      body: t('dash.deleteConfirm', { title: row.document?.title ?? row.title }),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/dashboards/${row.id}`);
      setRows((current) => current.filter((r) => r.id !== row.id));
      toast(t('common.deleted'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  const [creating, setCreating] = React.useState(false);

  /**
   * Create an empty dashboard, then open it.
   *
   * The button used to link to `/dashboards/new`. Nothing routed that path, so
   * it matched `/dashboards/:dashboardId` with the id "new" and the detail view
   * asked the API for a dashboard by that name -- a 404, rendered to the user as
   * "Not found, or your account lacks access." There was no way to create one.
   *
   * A dashboard needs a real id before it can have tiles pinned to it, and the
   * server mints that id, so creating is a round trip rather than a client-side
   * route. An empty document is valid (`verify_dashboard` reports no errors for
   * one), which is what makes this possible without a builder screen.
   */
  async function create() {
    setCreating(true);
    try {
      const body = await post<{ dashboard: { id: string } }>('/api/vanna/v2/dashboards', {
        title: t('dash.untitled'),
        description: '',
        tiles: [],
        parameters: [],
      });
      navigate(`/dashboards/${body.dashboard.id}`);
    } catch (caught) {
      toastError((caught as Error).message);
      setCreating(false);
    }
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('dash.title')}
        description={t('dash.sub')}
        actions={
          canAuthor ? (
            <Button variant="primary" disabled={creating} onClick={() => void create()}>
              <Plus />
              {creating ? t('dash.creating') : t('dash.newDashboard')}
            </Button>
          ) : null
        }
      />

      <Toolbar>
        <Input
          className="min-w-[220px] flex-1"
          type="search"
          value={filter}
          placeholder={t('dash.filter')}
          aria-label={t('dash.filter')}
          onChange={(event) => setFilter(event.target.value)}
        />
      </Toolbar>

      {loading ? (
        <LoadingCards count={6} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={<LayoutDashboard className="size-7" />}
          title={rows.length ? t('schema.noMatch') : t('dash.empty')}
          hint={rows.length ? undefined : t('dash.sub')}
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((row) => {
            const document = row.document ?? ({} as Dashboard);
            const tiles = document.tiles?.length ?? 0;
            const parameters = document.parameters?.length ?? 0;
            return (
              <Card key={row.id} className="flex flex-col p-4">
                <div className="flex-1">
                  <h3 className="text-[0.95rem] font-semibold leading-tight" dir="auto">
                    {document.title || row.title}
                  </h3>
                  {document.description ? (
                    <p className="mt-1 line-clamp-2 text-[0.8125rem] text-muted-foreground" dir="auto">
                      {document.description}
                    </p>
                  ) : null}

                  <div className="mt-2.5 flex flex-wrap gap-1.5">
                    <Badge tone="neutral">
                      {t(tiles === 1 ? 'dash.tile1' : 'dash.tiles', { n: tiles })}
                    </Badge>
                    {/* A dashboard that declares parameters is what this product
                        calls a report -- the same document, with controls. */}
                    {parameters > 0 ? (
                      <Badge tone="admin">
                        {t(parameters === 1 ? 'dash.parameter1' : 'dash.parameters', {
                          n: parameters,
                        })}
                      </Badge>
                    ) : null}
                  </div>
                </div>

                <p className="mt-3 text-[0.75rem] text-muted-foreground" dir="auto">
                  {row.created_by} &middot; {relative(row.updated_at || row.created_at, t, locale)}
                </p>

                <div className="mt-3 flex gap-2">
                  <Button variant="primary" asChild className="flex-1">
                    <Link to={`/dashboards/${row.id}`}>{t('dash.open')}</Link>
                  </Button>
                  {canAuthor ? (
                    <Button
                      variant="danger"
                      size="icon"
                      aria-label={t('common.delete')}
                      title={t('common.delete')}
                      onClick={() => void remove(row)}
                    >
                      <Trash2 />
                    </Button>
                  ) : null}
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </PageBody>
  );
}
