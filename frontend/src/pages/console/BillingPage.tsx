import { Receipt, XCircle } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { StatTile } from '@/components/primitives/stat-tile';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useConfirm } from '@/components/primitives/confirm';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { toast, toastError } from '@/lib/toast';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { relative } from '@/lib/time';
import type { Billing } from '@/types';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * The plan a workspace is on, and what it is allowed to do.
 *
 * Reading is tenant-admin; *changing* is platform-admin, because setting a plan
 * sets the quota, and a workspace admin who could POST their own plan could
 * grant themselves a five-hundred-fold quota increase for free. `can_change`
 * carries the server's answer rather than this screen inferring it.
 *
 * Amounts are integer cents end to end. Money in a float is a rounding error
 * waiting for a reconciliation.
 */
export default function BillingPage() {
  const { t, locale } = useLocale();
  const tenant = useConcreteScope();

  const confirm = useConfirm();
  const [data, setData] = React.useState<Billing | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [paying, setPaying] = React.useState(false);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setData(null);
    try {
      setData(
        await api<Billing>(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/billing`),
      );
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}`;

  async function choosePlan(plan: string, label: string) {
    const ok = await confirm.ask({
      title: t('bill.changeTitle'),
      // Naming the quota, not just the plan: the plan is what changes, the
      // quota is what anybody will notice.
      body: t('bill.changeConfirm', { plan: label, workspace: tenant }),
      confirmLabel: t('bill.choose'),
    });
    if (!ok) return;
    setBusy(true);
    try {
      await post(`${base}/billing/plan`, { plan, months: 1 });
      toast(t('common.updated'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancelPlan() {
    const ok = await confirm.ask({
      title: t('bill.cancelTitle'),
      body: t('bill.cancelConfirm', { workspace: tenant }),
      confirmLabel: t('bill.cancel'),
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await post(`${base}/billing/cancel`);
      toast(t('common.updated'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const number = (value: number) => new Intl.NumberFormat(locale).format(value || 0);
  const money = (cents: number, currency: string) =>
    new Intl.NumberFormat(locale, { style: 'currency', currency: currency || 'USD' })
      .format((cents || 0) / 100);

  const expires = data?.subscription?.expires_at ?? null;
  const expired = expires ? new Date(expires) < new Date() : false;

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('tab.billing')}
        description={t('bill.blurb', { workspace: tenant })}
        actions={
          data?.can_change ? (
            <>
              <Button onClick={() => setPaying(true)} disabled={busy}>
                <Receipt />
                {t('bill.recordPayment')}
              </Button>
              {data.subscription ? (
                <Button variant="danger" onClick={() => void cancelPlan()} disabled={busy}>
                  <XCircle />
                  {t('bill.cancel')}
                </Button>
              ) : null}
            </>
          ) : null
        }
      />

      <Toolbar>
        <ScopePicker id="billing-scope" />
      </Toolbar>

      {error ? (
        <ErrorState title={t('bill.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : !data ? (
        <LoadingCards count={3} />
      ) : (
        <>
          <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              label={t('bill.plan')}
              value={
                <span className="flex items-center gap-2">
                  {data.plan}
                  {expired ? <Badge tone="err">{t('time.expired')}</Badge> : null}
                </span>
              }
              hint={expires ? relative(expires, t, locale) : t('bill.noExpiry')}
            />
            {/* Usage is a 30-day figure and the quota is daily, so they are shown
                as separate numbers rather than one misleading ratio. */}
            <StatTile label={t('bill.questionsPerDay')} value={number(data.limits.daily_quota)}
              hint={t('bill.source', { source: data.limits.quota_source })} />
            <StatTile label={t('bill.rowsPerQuery')} value={number(data.limits.max_rows)}
              hint={t('bill.source', { source: data.limits.rows_source })} />
            <StatTile label={t('kpi.questions')} value={number(data.usage.questions)}
              hint={t('ov.lastDays', { n: data.usage.window_days })} />
          </div>

          {data.available_plans?.length ? (
            <Card className="mb-3 p-4">
              <h3 className="mb-1 text-[0.9rem] font-semibold">{t('bill.plans')}</h3>
              <p className="mb-3 text-[0.8125rem] text-muted-foreground">
                {data.can_change ? t('bill.changeElsewhere') : t('bill.readOnly')}
              </p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {data.available_plans.map((plan) => (
                  <Card
                    key={plan.name}
                    className={
                      plan.name === data.plan
                        ? 'border-primary bg-primary-soft/40 p-3'
                        : 'p-3'
                    }
                  >
                    <p className="flex items-center gap-2 text-[0.875rem] font-semibold">
                      {plan.label}
                      {plan.name === data.plan ? (
                        <Badge tone="admin">{t('bill.current')}</Badge>
                      ) : null}
                    </p>
                    <p className="mt-1 text-[0.8125rem] text-muted-foreground">
                      {plan.description}
                    </p>
                    <p className="mt-2 text-[0.75rem] text-muted-foreground tabular-nums">
                      {t('bill.questionsPerDay')}: {number(plan.daily_quota)} ·{' '}
                      {t('bill.rowsPerQuery')}: {number(plan.max_rows)}
                    </p>
                    {/* Offered only when the server says so. `can_change` is
                        platform-admin: setting a plan sets the quota, and a
                        workspace admin who could POST their own would be granting
                        themselves a quota increase. */}
                    {data.can_change && plan.name !== data.plan ? (
                      <Button
                        className="mt-3 w-full"
                        size="sm"
                        disabled={busy}
                        onClick={() => void choosePlan(plan.name, plan.label)}
                      >
                        {t('bill.choose')}
                      </Button>
                    ) : null}
                  </Card>
                ))}
              </div>
            </Card>
          ) : null}

          <Card className="p-4">
            <h3 className="mb-2 text-[0.9rem] font-semibold">{t('bill.payments')}</h3>
            {data.payments.length === 0 ? (
              <EmptyState title={t('bill.noPayments')} hint={t('bill.noPaymentsHint')} />
            ) : (
              <ScrollX>
                <DataTable>
                  <thead>
                    <Tr>
                      <Th>{t('audit.time')}</Th>
                      <Th>{t('bill.amount')}</Th>
                      <Th>{t('bill.note')}</Th>
                    </Tr>
                  </thead>
                  <Tbody>
                    {data.payments.map((payment) => (
                      <Tr key={payment.id}>
                        <Td className="text-muted-foreground">
                          {relative(payment.created_at, t, locale)}
                        </Td>
                        <Td className="font-mono tabular-nums">
                          {money(payment.amount_cents, payment.currency)}
                        </Td>
                        <Td dir="auto">{payment.note || '—'}</Td>
                      </Tr>
                    ))}
                  </Tbody>
                </DataTable>
              </ScrollX>
            )}
          </Card>
        </>
      )}

      {paying ? (
        <RecordPayment
          tenant={tenant}
          onClose={() => setPaying(false)}
          onRecorded={() => {
            setPaying(false);
            void load();
          }}
        />
      ) : null}
    </PageBody>
  );
}

/**
 * A payment somebody took outside this system, written into its ledger.
 *
 * Amounts are entered in whole currency and stored as integer cents, converted
 * once here. Money in a float is a rounding error waiting for a reconciliation,
 * and the conversion has to happen somewhere -- doing it at the boundary means
 * nothing downstream ever sees a fraction.
 */
function RecordPayment({
  tenant,
  onClose,
  onRecorded,
}: {
  tenant: string;
  onClose: () => void;
  onRecorded: () => void;
}) {
  const { t } = useLocale();
  const [reference, setReference] = React.useState('');
  const [amount, setAmount] = React.useState('');
  const [currency, setCurrency] = React.useState('usd');
  const [description, setDescription] = React.useState('');
  const [saving, setSaving] = React.useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      await post(`/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/billing/payments`, {
        reference: reference.trim(),
        amount_cents: Math.round(Number(amount || 0) * 100),
        currency: currency.trim().toLowerCase() || 'usd',
        description: description.trim(),
        months: 1,
      });
      toast(t('bill.recorded'));
      onRecorded();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-[460px]">
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>{t('bill.recordPayment')}</DialogTitle>
            <DialogDescription>{t('bill.recordHint')}</DialogDescription>
          </DialogHeader>

          <div className="mt-3 flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="pay-ref">{t('bill.reference')}</Label>
              <Input
                id="pay-ref"
                dir="ltr"
                required
                autoFocus
                value={reference}
                placeholder="inv-2026-0041"
                onChange={(event) => setReference(event.target.value)}
              />
            </div>

            <div className="grid grid-cols-[1fr_100px] gap-3">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="pay-amount">{t('bill.amount')}</Label>
                <Input
                  id="pay-amount"
                  type="number"
                  min="0"
                  step="0.01"
                  dir="ltr"
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="pay-currency">{t('bill.currency')}</Label>
                <Input
                  id="pay-currency"
                  dir="ltr"
                  maxLength={3}
                  value={currency}
                  onChange={(event) => setCurrency(event.target.value)}
                />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="pay-note">{t('bill.note')}</Label>
              <Input
                id="pay-note"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary" disabled={saving || !reference.trim()}>
              {t('bill.record')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
