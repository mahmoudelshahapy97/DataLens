import { LayoutDashboard, Play, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';

import { useSession } from '@/app/session';
import { useConfirm } from '@/components/primitives/confirm';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';

import { SaveQueryDialog } from './SaveQueryDialog';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api, del } from '@/lib/api';
import { relative } from '@/lib/time';
import { toast, toastError } from '@/lib/toast';
import type { SavedQuery } from '@/types';

import { PinToDashboard } from './PinToDashboard';
import { ResultPanel, useQueryRunner } from './ResultPanel';

/**
 * Queries somebody kept.
 *
 * The card is the unit rather than a table row because the SQL is the content --
 * a saved query with its statement hidden behind a disclosure is a list of
 * titles, and titles are not what anybody is looking for here.
 *
 * "Pin to dashboard" is the workflow this page exists to serve. It creates a
 * tile that *references* the saved query rather than copying its SQL, so fixing
 * the statement here fixes every dashboard showing it -- which is the whole
 * reason `SavedQueryRef` is the preferred tile shape.
 */

export default function SavedPage() {
  const { t, locale } = useLocale();
  const [creating, setCreating] = React.useState(false);
  const { canAuthor } = useSession();
  const confirm = useConfirm();
  const runner = useQueryRunner();

  const [items, setItems] = React.useState<SavedQuery[]>([]);
  const [filter, setFilter] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [pinning, setPinning] = React.useState<SavedQuery | null>(null);

  const load = React.useCallback(async (alive: () => boolean = () => true) => {
    try {
      const body = await api<{ saved: SavedQuery[] }>('/api/vanna/v2/saved-queries');
      if (!alive()) return;
      setItems(body.saved ?? []);
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
    if (!needle) return items;
    return items.filter(
      (item) =>
        item.title.toLowerCase().includes(needle) ||
        item.question.toLowerCase().includes(needle) ||
        item.sql.toLowerCase().includes(needle),
    );
  }, [items, filter]);

  async function remove(item: SavedQuery) {
    const ok = await confirm.ask({
      title: t('saved.deleteTitle'),
      body: t('saved.deleteConfirm', { title: item.title }),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/saved-queries/${item.id}`);
      setItems((current) => current.filter((i) => i.id !== item.id));
      toast(t('common.deleted'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('saved.title')}
        description={t('saved.sub')}
        actions={
          canAuthor ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus />
              {t('saved.newQuery')}
            </Button>
          ) : null
        }
      />

      <Toolbar>
        <Input
          className="min-w-[220px] flex-1"
          type="search"
          value={filter}
          placeholder={t('saved.filter')}
          aria-label={t('saved.filter')}
          onChange={(event) => setFilter(event.target.value)}
        />
      </Toolbar>

      {loading ? (
        <LoadingCards count={4} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : visible.length === 0 ? (
        <EmptyState
          title={items.length ? t('schema.noMatch') : t('saved.empty')}
          hint={items.length ? undefined : t('saved.sub')}
        />
      ) : (
        <div className="flex flex-col gap-3">
          {visible.map((item) => (
            <Card key={item.id} className="p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-[0.95rem] font-semibold" dir="auto">
                    {item.title}
                  </h3>
                  {item.question ? (
                    <p className="mt-0.5 text-[0.8125rem] text-muted-foreground" dir="auto">
                      {item.question}
                    </p>
                  ) : null}
                </div>
                <p className="shrink-0 text-[0.75rem] text-muted-foreground">
                  {item.created_by} &middot; {relative(item.created_at, t, locale)}
                </p>
              </div>

              <pre
                className="sql mt-3 max-h-40 overflow-auto rounded-md border border-border bg-surface-2 p-2.5 font-mono text-[0.78rem]"
                dir="ltr"
              >
                {item.sql}
              </pre>

              <div className="mt-3 flex flex-wrap gap-2">
                <Button onClick={() => void runner.run(item.sql, item.title)}>
                  <Play />
                  {t('common.run')}
                </Button>
                {canAuthor ? (
                  <Button onClick={() => setPinning(item)}>
                    <LayoutDashboard />
                    {t('dash.pin')}
                  </Button>
                ) : null}
                {canAuthor ? (
                  <Button variant="danger" onClick={() => void remove(item)}>
                    <Trash2 />
                    {t('common.delete')}
                  </Button>
                ) : null}
              </div>
            </Card>
          ))}
        </div>
      )}

      <ResultPanel runner={runner} />

      {pinning ? (
        <PinToDashboard
          savedQuery={pinning}
          onClose={() => setPinning(null)}
          onPinned={() => {
            setPinning(null);
            toast(t('dash.pinned'));
          }}
        />
      ) : null}
      {creating ? (
        <SaveQueryDialog onClose={() => setCreating(false)} onSaved={() => void load()} />
      ) : null}
    </PageBody>
  );
}
