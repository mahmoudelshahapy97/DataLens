import { ClipboardCheck } from 'lucide-react';
import * as React from 'react';

import { useSession } from '@/app/session';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { relative, until } from '@/lib/time';
import { toastError, toastSuccess } from '@/lib/toast';

/**
 * Changes the agent proposed but has not made.
 *
 * A workspace that allows writes does not thereby allow the model to run them.
 * The statement is parsed, checked against the grants, and parked here; a human
 * reads the SQL and decides. `write_approval_mode` says whether the requester
 * may approve their own -- an administrator has already decided which tables are
 * writable at all, so self-approval is the default and a second pair of eyes is
 * the opt-in.
 *
 * A workspace with writes disabled has no queue and the endpoint answers 404,
 * which is the honest reply: the surface genuinely does not exist for it.
 */

/**
 * One row of the queue, named as `_render` in `routes/writes.py` names it.
 *
 * Every field here was wrong: the page read `sql`, `table`, `statement_kind`,
 * `requested_at` and `row_estimate`, none of which the endpoint returns. So a
 * pending change rendered as an empty badge, a blank table name and -- worst --
 * an empty `<pre>` where the SQL should be. An approver was being asked to
 * approve a statement the screen did not show them.
 */
interface PendingWrite {
  id: string;
  status: string;
  /** insert / update / delete. */
  operation: string;
  /** Plural, and an array: one statement can touch several. */
  tables: string[];
  expected_row_count: number | null;
  is_destructive: boolean;
  /** Shape-only, deliberately: literals are stripped server-side. */
  statement_preview: string;
  parameter_summary: string;
  requested_by: string;
  requested_by_email: string;
  expires_at: string;
  created_at: string;
  description: string;
}

export default function ApprovalsPage() {
  const { t, locale } = useLocale();
  const { identity } = useSession();

  const [rows, setRows] = React.useState<PendingWrite[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [unavailable, setUnavailable] = React.useState(false);
  const [busy, setBusy] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setRows(null);
    setUnavailable(false);
    try {
      const body = await api<{ pending: PendingWrite[]; writes?: PendingWrite[] }>(
        '/api/vanna/v2/writes',
      );
      setRows(body.pending ?? body.writes ?? []);
      setError(null);
    } catch (caught) {
      const status = (caught as { status?: number }).status;
      // 404 is "writes are not enabled here", not a failure to report as one.
      if (status === 404) setUnavailable(true);
      else setError((caught as Error).message);
    }
  }, []);

  // Refetch when the *session* workspace or database changes, since that is what
  // the endpoint scopes by. Keyed on both: a pending change belongs to the
  // database it was proposed against, so switching database changes the queue.
  React.useEffect(() => {
    void load();
  }, [load, identity?.tenant, identity?.dataSourceId]);

  const decide = async (write: PendingWrite, approve: boolean) => {
    setBusy(write.id);
    try {
      // `{approved: bool}` -- what `DecisionPayload` declares. This sent
      // `{decision: 'approve'|'reject'}`, which pydantic rejected, so every
      // approve and every reject was a 422 and the queue never moved.
      await post(`/api/vanna/v2/writes/${write.id}/decision`, { approved: approve });
      toastSuccess(approve ? t('appr.approved') : t('appr.rejected'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <PageBody>
      <PageHeader title={t('nav.approvals')} description={t('appr.blurb')} />

      {/* No workspace picker. `/writes` takes no tenant parameter -- the queue
          is always the caller's own session workspace and session data source,
          deliberately (`writes.py` resolves `X-Data-Source-Id`, because a
          pending change belongs to the database it was proposed against). The
          picker that used to sit here changed nothing, and a control that does
          nothing is worse than no control: it says the screen is showing you
          something it is not. */}
      {unavailable ? (
        <EmptyState
          icon={<ClipboardCheck className="size-7" />}
          title={t('appr.disabled')}
          hint={t('appr.disabledHint')}
        />
      ) : error ? (
        <ErrorState title={t('appr.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={4} />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<ClipboardCheck className="size-7" />}
          title={t('appr.none')}
          hint={t('appr.noneHint')}
        />
      ) : (
        <div className="flex flex-col gap-3">
          {rows.map((write) => (
            <Card key={write.id} className="p-4">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                {/* Destructive is its own colour. A DELETE and an INSERT are not
                    the same decision and must not look like one. */}
                <Badge tone={write.is_destructive ? 'bad' : 'warn'}>{write.operation}</Badge>
                <span className="font-mono text-[0.78rem]">{write.tables.join(', ')}</span>
                <span className="ms-auto text-[0.72rem] text-muted-foreground">
                  {write.requested_by_email || write.requested_by} ·{' '}
                  {relative(write.created_at, t, locale)}
                </span>
              </div>

              {write.description ? (
                <p className="mb-2 text-[0.8125rem]" dir="auto">{write.description}</p>
              ) : null}

              <pre className="overflow-x-auto rounded-md border border-border bg-surface-2 p-3 text-[0.75rem] leading-relaxed">
                {write.statement_preview}
              </pre>

              <p className="mt-2 flex flex-wrap gap-x-3 text-[0.72rem] text-muted-foreground">
                {write.expected_row_count != null ? (
                  <span>{t('appr.rows', { n: write.expected_row_count })}</span>
                ) : null}
                {write.parameter_summary ? <span>{write.parameter_summary}</span> : null}
                {/* An expired request cannot be approved, so say so before the
                    button is pressed rather than after the server refuses. */}
                <span>{t('appr.expires', { when: until(write.expires_at, t, locale) })}</span>
              </p>

              <div className="mt-3 flex gap-2">
                <Button variant="primary" disabled={busy === write.id}
                  onClick={() => void decide(write, true)}>
                  {t('appr.approve')}
                </Button>
                <Button variant="danger" disabled={busy === write.id}
                  onClick={() => void decide(write, false)}>
                  {t('appr.reject')}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </PageBody>
  );
}
