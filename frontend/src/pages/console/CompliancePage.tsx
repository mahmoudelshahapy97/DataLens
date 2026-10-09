import { ShieldCheck } from 'lucide-react';
import * as React from 'react';

import { useSession } from '@/app/session';
import { useConfirm } from '@/components/primitives/confirm';
import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { relative } from '@/lib/time';
import { TONE_TEXT_CLASSES } from '@/lib/tone';
import { toast, toastError } from '@/lib/toast';

/**
 * Right-to-be-forgotten requests.
 *
 * Two properties of this workflow are unusual enough that the screen states them
 * rather than leaving them to be discovered:
 *
 * **Two people.** A request is created by one platform admin and executed by
 * another. The Execute button is therefore disabled for whoever raised it -- the
 * API refuses it too, and so does a CHECK constraint on the table, but a button
 * that looks available and then fails is worse than one that explains itself.
 *
 * **Redaction, not deletion, for the audit trail.** Question text and SQL are
 * blanked and the user id anonymised, but the generation rows survive with their
 * timing and cost. That is deliberate: the record that a deletion happened is
 * the compliance artifact, and a system that erased its own proof could not
 * answer the question the erasure was for. The outcome panel shows exactly which
 * tables were emptied and which were redacted.
 */

interface DeletionRequest {
  id: string;
  subject_email: string;
  tenant_id: string;
  status: string;
  requested_by: string;
  executed_by: string | null;
  notes: string;
  outcome: {
    deleted?: Record<string, number>;
    redacted?: Record<string, number>;
    kept?: Record<string, string>;
    scope?: string;
  };
  error: string;
  created_at: string;
  executed_at: string | null;
}

function statusTone(status: string): 'good' | 'bad' | 'warn' | 'neutral' {
  if (status === 'completed') return 'good';
  if (status === 'failed') return 'bad';
  if (status === 'pending' || status === 'executing') return 'warn';
  return 'neutral';
}

export default function CompliancePage() {
  const { t, locale } = useLocale();
  const { me } = useSession();
  const confirm = useConfirm();
  const self = (me?.user.email ?? '').toLowerCase();

  const [requests, setRequests] = React.useState<DeletionRequest[]>([]);
  const [expanded, setExpanded] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const [subject, setSubject] = React.useState('');
  const [scope, setScope] = React.useState('');
  const [notes, setNotes] = React.useState('');
  const [creating, setCreating] = React.useState(false);

  const load = React.useCallback(async (alive: () => boolean = () => true) => {
    try {
      const body = await api<{ requests: DeletionRequest[] }>(
        '/api/vanna/v2/compliance/deletion-requests',
      );
      if (!alive()) return;
      setRequests(body.requests ?? []);
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

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setCreating(true);
    try {
      await post('/api/vanna/v2/compliance/deletion-requests', {
        subject_email: subject,
        tenant_id: scope,
        notes,
      });
      setSubject('');
      setScope('');
      setNotes('');
      toast(t('compliance.created'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setCreating(false);
    }
  }

  async function execute(request: DeletionRequest) {
    const ok = await confirm.ask({
      title: t('compliance.executeTitle'),
      body: t('compliance.executeBody', {
        subject: request.subject_email,
        scope: request.tenant_id || t('compliance.allWorkspaces'),
      }),
      confirmLabel: t('compliance.execute'),
      danger: true,
    });
    if (!ok) return;
    try {
      await post(`/api/vanna/v2/compliance/deletion-requests/${request.id}/execute`);
      toast(t('compliance.executed'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function cancel(request: DeletionRequest) {
    try {
      await post(`/api/vanna/v2/compliance/deletion-requests/${request.id}/cancel`);
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader title={t('nav.compliance')} description={t('compliance.sub')} />

      <Card className="mb-4 p-4">
        <h3 className="mb-1 text-[0.95rem] font-semibold">{t('compliance.newRequest')}</h3>
        <p className="mb-3 text-[0.8125rem] text-muted-foreground">
          {t('compliance.twoPerson')}
        </p>

        <form className="grid gap-3 sm:grid-cols-2" onSubmit={create}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="comp-subject">{t('compliance.subject')}</Label>
            <Input
              id="comp-subject"
              type="email"
              dir="ltr"
              required
              value={subject}
              placeholder="person@example.com"
              onChange={(event) => setSubject(event.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="comp-scope">{t('compliance.scope')}</Label>
            <Input
              id="comp-scope"
              dir="ltr"
              value={scope}
              placeholder={t('compliance.allWorkspaces')}
              onChange={(event) => setScope(event.target.value)}
            />
            {/* Blank means every workspace. Said here because the opposite guess
                -- that blank means "none" -- would be a very expensive mistake. */}
            <p className="text-[0.75rem] text-muted-foreground">{t('compliance.scopeHint')}</p>
          </div>

          <div className="flex flex-col gap-1.5 sm:col-span-2">
            <Label htmlFor="comp-notes">{t('compliance.notes')}</Label>
            <Textarea
              id="comp-notes"
              className="min-h-[70px]"
              value={notes}
              placeholder={t('compliance.notesHint')}
              onChange={(event) => setNotes(event.target.value)}
            />
          </div>

          <div className="sm:col-span-2">
            <Button type="submit" variant="primary" disabled={creating || !subject}>
              {t('compliance.createRequest')}
            </Button>
          </div>
        </form>
      </Card>

      {loading ? (
        <LoadingRows rows={5} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : requests.length === 0 ? (
        <EmptyState
          icon={<ShieldCheck className="size-7" />}
          title={t('compliance.empty')}
          hint={t('compliance.emptyHint')}
        />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('compliance.subject')}</Th>
                <Th>{t('compliance.scope')}</Th>
                <Th className="w-28">{t('history.status')}</Th>
                <Th>{t('compliance.requestedBy')}</Th>
                <Th>{t('compliance.executedBy')}</Th>
                <Th className="w-40" />
              </Tr>
            </thead>
            <Tbody>
              {requests.map((request) => {
                const isOwnRequest = request.requested_by === self;
                return (
                  <React.Fragment key={request.id}>
                    <Tr
                      className="cursor-pointer"
                      onClick={() => setExpanded(expanded === request.id ? null : request.id)}
                    >
                      <Td dir="ltr" className="font-mono text-[0.78rem]">
                        {request.subject_email}
                      </Td>
                      <Td className="text-muted-foreground">
                        {request.tenant_id || t('compliance.allWorkspaces')}
                      </Td>
                      <Td>
                        <Badge tone={statusTone(request.status)}>{request.status}</Badge>
                      </Td>
                      <Td dir="ltr" className="text-[0.78rem] text-muted-foreground">
                        {request.requested_by}
                        <span className="block">{relative(request.created_at, t, locale)}</span>
                      </Td>
                      <Td dir="ltr" className="text-[0.78rem] text-muted-foreground">
                        {request.executed_by ?? '—'}
                      </Td>
                      <Td>
                        {request.status === 'pending' ? (
                          <div className="flex justify-end gap-1">
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={(event) => {
                                event.stopPropagation();
                                void cancel(request);
                              }}
                            >
                              {t('compliance.cancel')}
                            </Button>
                            <Button
                              size="sm"
                              variant="danger"
                              // The two-person rule, made visible. The API and a
                              // CHECK constraint both refuse it as well.
                              disabled={isOwnRequest}
                              title={isOwnRequest ? t('compliance.needsOther') : undefined}
                              onClick={(event) => {
                                event.stopPropagation();
                                void execute(request);
                              }}
                            >
                              {t('compliance.execute')}
                            </Button>
                          </div>
                        ) : null}
                      </Td>
                    </Tr>

                    {expanded === request.id ? (
                      <tr>
                        <Td colSpan={6} className="pt-0">
                          <Outcome request={request} />
                        </Td>
                      </tr>
                    ) : null}
                  </React.Fragment>
                );
              })}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}
    </PageBody>
  );
}

/** What execution actually did, in the three categories that matter. */
function Outcome({ request }: { request: DeletionRequest }) {
  const { t } = useLocale();
  const { deleted = {}, redacted = {}, kept = {} } = request.outcome ?? {};

  if (request.status === 'pending') {
    return (
      <div className="rounded-md border border-border bg-surface-2 p-3">
        <p className="text-[0.8125rem] text-muted-foreground" dir="auto">
          {request.notes || t('compliance.noNotes')}
        </p>
      </div>
    );
  }

  return (
    <div className="grid gap-3 rounded-md border border-border bg-surface-2 p-3 sm:grid-cols-3">
      <Section title={t('compliance.deleted')} tone="bad">
        {Object.entries(deleted).map(([table, count]) => (
          <li key={table} className="flex justify-between gap-2">
            <span className="font-mono">{table}</span>
            <span className="tabular-nums">{count}</span>
          </li>
        ))}
      </Section>

      <Section title={t('compliance.redacted')} tone="warn">
        {Object.entries(redacted).map(([table, count]) => (
          <li key={table} className="flex justify-between gap-2">
            <span className="font-mono">{table}</span>
            <span className="tabular-nums">{count}</span>
          </li>
        ))}
      </Section>

      <Section title={t('compliance.kept')} tone="neutral">
        {Object.entries(kept).map(([table, why]) => (
          <li key={table}>
            <span className="font-mono">{table}</span>
            <span className="block text-[0.7rem]">{why}</span>
          </li>
        ))}
      </Section>

      {request.error ? (
        <p className="text-[0.8125rem] text-bad sm:col-span-3" dir="auto">
          {request.error}
        </p>
      ) : null}
    </div>
  );
}

function Section({
  title,
  tone,
  children,
}: {
  title: string;
  tone: 'bad' | 'warn' | 'neutral';
  children: React.ReactNode;
}) {
  const colour = TONE_TEXT_CLASSES[tone];
  return (
    <div>
      <h4 className={`mb-1 text-[0.6875rem] font-semibold uppercase tracking-wider ${colour}`}>
        {title}
      </h4>
      <ul className="flex flex-col gap-0.5 text-[0.78rem] text-muted-foreground">{children}</ul>
    </div>
  );
}
