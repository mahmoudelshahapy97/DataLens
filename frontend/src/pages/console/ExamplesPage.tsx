import { BadgeCheck, Sparkles } from 'lucide-react';
import * as React from 'react';

import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';
import { relative } from '@/lib/time';
import { toastError, toastSuccess } from '@/lib/toast';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * The training-data flywheel, in its two halves.
 *
 * A thumbs-up captures a candidate automatically. That is a *signal*, not a
 * review: the user liked the answer, which is not the same as the SQL being
 * right, and a verified example is retrieved and shown to the model as an
 * exemplar. So promotion is a deliberate act by somebody who read the query.
 *
 * Rejected examples are kept rather than deleted, so the same bad pattern is not
 * silently recaptured the next time somebody likes it.
 */

export interface Example {
  id: string;
  question: string;
  sql: string;
  status: 'candidate' | 'verified' | 'rejected';
  tenant_id: string;
  data_source_id: string;
  created_by: string;
  created_at: string;
  verified_by: string | null;
  verified_at: string | null;
  tables: string[];
  tags: string[];
}

/** `review` is the queue; `verified` is what the agent is actually shown. */
export function ExamplesScreen({ mode }: { mode: 'candidate' | 'verified' }) {
  const { t, locale } = useLocale();
  const tenant = useConcreteScope();

  const [rows, setRows] = React.useState<Example[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [filter, setFilter] = React.useState('');
  const [busy, setBusy] = React.useState<string | null>(null);

  /**
   * Scope this screen to the workspace the console is looking at.
   *
   * `/admin/examples` takes no tenant parameter -- it is a library route that
   * derives the workspace from the resolved caller, i.e. from `X-Tenant-Id`.
   * The global header provider sends the *session* workspace, so a platform
   * admin reviewing a customer from their own session was shown their own
   * queue, with nothing on screen saying which one they were reading.
   *
   * Overriding the header per call is enough, and needs no change to the
   * vendored library: `identity.py` lets a platform admin name any workspace
   * (`if member is None and not platform_admin: raise`), and for everybody else
   * this value already *is* their own workspace, so it is a no-op.
   */
  const scoped = React.useMemo(
    () => (tenant ? { headers: { 'X-Tenant-Id': tenant } } : {}),
    [tenant],
  );

  const load = React.useCallback(async () => {
    setRows(null);
    try {
      const body = await api<{ examples: Example[] }>(
        '/api/vanna/v2/admin/examples',
        scoped,
      );
      setRows(body.examples ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [scoped]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const setStatus = async (example: Example, status: string) => {
    setBusy(example.id);
    try {
      // Same override on the mutations: promoting an example must land in the
      // workspace whose queue it was read from.
      await api(`/api/vanna/v2/admin/examples/${example.id}/status`, {
        ...scoped,
        method: 'POST',
        body: JSON.stringify({ status }),
      });
      toastSuccess(t('ex.updated'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (example: Example) => {
    setBusy(example.id);
    try {
      await api(`/api/vanna/v2/admin/examples/${example.id}`, {
        ...scoped,
        method: 'DELETE',
      });
      toastSuccess(t('common.deleted'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const needle = filter.trim().toLowerCase();
  const visible = (rows ?? [])
    .filter((row) => row.status === mode)
    .filter(
      (row) =>
        !needle ||
        row.question.toLowerCase().includes(needle) ||
        row.sql.toLowerCase().includes(needle),
    );

  const title = mode === 'candidate' ? t('tab.review') : t('tab.verified');

  return (
    <PageBody>
      <PageHeader
        title={title}
        description={mode === 'candidate' ? t('ex.reviewBlurb') : t('ex.verifiedBlurb')}
      />

      <Toolbar>
        {/* Now that the request is scoped, the picker actually moves it. */}
        <ScopePicker id="examples-scope" />
        <Input
          type="search"
          className="w-72"
          placeholder={t('ex.search')}
          value={filter}
          onChange={(event) => setFilter(event.currentTarget.value)}
        />
        <span className="text-[0.8125rem] text-muted-foreground">
          {t('ex.count', { n: visible.length })}
        </span>
      </Toolbar>

      {error ? (
        <ErrorState title={t('ex.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={5} />
      ) : visible.length === 0 ? (
        <EmptyState
          icon={mode === 'candidate' ? <Sparkles className="size-7" /> : <BadgeCheck className="size-7" />}
          title={mode === 'candidate' ? t('ex.noneCandidate') : t('ex.noneVerified')}
          hint={mode === 'candidate' ? t('ex.noneCandidateHint') : t('ex.noneVerifiedHint')}
        />
      ) : (
        <div className="flex flex-col gap-3">
          {visible.map((example) => (
            <Card key={example.id} className="p-4">
              <div className="mb-2 flex flex-wrap items-start justify-between gap-2">
                <p className="font-medium" dir="auto">{example.question}</p>
                <Badge tone={example.status === 'verified' ? 'good' : 'warn'}>
                  {example.status}
                </Badge>
              </div>

              <pre className="overflow-x-auto rounded-md border border-border bg-surface-2 p-3 text-[0.75rem] leading-relaxed">
                {example.sql}
              </pre>

              <p className="mt-2 text-[0.72rem] text-muted-foreground">
                {example.data_source_id} · {example.created_by || '—'} ·{' '}
                {relative(example.created_at, t, locale)}
              </p>

              <div className="mt-3 flex flex-wrap gap-2">
                {mode === 'candidate' ? (
                  <>
                    <Button variant="primary" disabled={busy === example.id}
                      onClick={() => void setStatus(example, 'verified')}>
                      {t('ex.promote')}
                    </Button>
                    <Button disabled={busy === example.id}
                      onClick={() => void setStatus(example, 'rejected')}>
                      {t('ex.reject')}
                    </Button>
                  </>
                ) : (
                  <Button disabled={busy === example.id}
                    onClick={() => void setStatus(example, 'candidate')}>
                    {t('ex.unverify')}
                  </Button>
                )}
                <Button variant="danger" disabled={busy === example.id}
                  onClick={() => void remove(example)}>
                  {t('common.delete')}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </PageBody>
  );
}

export default function ReviewPage() {
  return <ExamplesScreen mode="candidate" />;
}
