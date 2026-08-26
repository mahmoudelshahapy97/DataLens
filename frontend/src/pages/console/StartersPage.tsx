import { Trash2 } from 'lucide-react';
import * as React from 'react';

import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api, post, del } from '@/lib/api';
import { toastError, toastSuccess } from '@/lib/toast';
import type { StarterQuestion } from '@/types';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * The questions a new user is offered before they know what to ask.
 *
 * The emptiest screen in the product is a chat box with a cursor in it: a user
 * who has never seen the data cannot guess what it can answer, and the first
 * question they invent is usually one it cannot. These are the ones that work.
 */
export default function StartersPage() {
  const { t } = useLocale();
  const tenant = useConcreteScope();

  const [rows, setRows] = React.useState<StarterQuestion[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [draft, setDraft] = React.useState('');
  const [busy, setBusy] = React.useState(false);

  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/starters`;

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setRows(null);
    try {
      const body = await api<{ starters: StarterQuestion[] }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/starters`,
      );
      setRows(body.starters ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const add = async () => {
    const question = draft.trim();
    if (!question) return;
    setBusy(true);
    try {
      await post(base, { question });
      setDraft('');
      toastSuccess(t('start.added'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (starter: StarterQuestion) => {
    setBusy(true);
    try {
      await del(`${base}/${starter.id}`);
      toastSuccess(t('common.deleted'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <PageBody>
      <PageHeader title={t('tab.starters')} description={t('start.blurb', { workspace: tenant })} />

      <Toolbar>
        <ScopePicker id="starters-scope" />
      </Toolbar>

      <Card className="mb-3 flex flex-wrap items-center gap-2 p-3">
        <Input
          className="min-w-64 flex-1"
          placeholder={t('start.placeholder')}
          value={draft}
          dir="auto"
          onChange={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void add();
          }}
        />
        <Button variant="primary" disabled={busy || !draft.trim()} onClick={() => void add()}>
          {t('common.create')}
        </Button>
      </Card>

      {error ? (
        <ErrorState title={t('start.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={4} />
      ) : rows.length === 0 ? (
        <EmptyState title={t('start.none')} hint={t('start.noneHint')} />
      ) : (
        <div className="flex flex-col gap-2">
          {rows.map((starter) => (
            <Card key={starter.id} className="flex items-center gap-3 p-3">
              <p className="min-w-0 flex-1 text-[0.875rem]" dir="auto">{starter.question}</p>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t('common.delete')}
                disabled={busy}
                onClick={() => void remove(starter)}
              >
                <Trash2 />
              </Button>
            </Card>
          ))}
        </div>
      )}
    </PageBody>
  );
}
