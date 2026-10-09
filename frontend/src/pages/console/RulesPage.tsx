import { Lock } from 'lucide-react';
import * as React from 'react';

import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { toastError, toastSuccess } from '@/lib/toast';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * The standing instructions the agent is given before it sees a question.
 *
 * Three origins, and the distinction is the whole screen. **Platform** rules are
 * safety properties -- "only reference tables that exist" -- and are locked:
 * a workspace admin turning one off would be turning off a guardrail for their
 * own users. **Pack** rules come from the starter library and are disableable.
 * **Workspace** rules are the ones this customer wrote.
 *
 * `locked` and `disableable` are the server's answer, not this screen's
 * inference. The toggle is hidden when the server says the rule cannot move,
 * because a control that always fails is worse than no control.
 */

interface Instruction {
  id: string;
  text: string;
  scope: string;
  scope_ref: string | null;
  priority: number;
  enabled: boolean;
  locked: boolean;
  disableable: boolean;
  origin: string;
  source_pack: string | null;
  created_by: string | null;
  updated_at: string | null;
}

export default function RulesPage() {
  const { t } = useLocale();
  const tenant = useConcreteScope();

  const [rows, setRows] = React.useState<Instruction[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setRows(null);
    try {
      const body = await api<{ instructions: Instruction[] }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/instructions`,
      );
      setRows(body.instructions ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (rule: Instruction, enabled: boolean) => {
    setBusy(rule.id);
    try {
      await post(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}` +
          `/instructions/${encodeURIComponent(rule.id)}/enabled`,
        { enabled },
      );
      toastSuccess(enabled ? t('rules.enabled') : t('rules.disabled'));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const tone = (origin: string) =>
    origin === 'platform' ? 'bad' : origin === 'pack' ? 'accent' : 'neutral';

  return (
    <PageBody>
      <PageHeader title={t('tab.rules')} description={t('rules.blurb', { workspace: tenant })} />

      <Toolbar>
        <ScopePicker id="rules-scope" />
      </Toolbar>

      {error ? (
        <ErrorState title={t('rules.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={5} />
      ) : rows.length === 0 ? (
        <EmptyState title={t('rules.none')} hint={t('rules.noneHint')} />
      ) : (
        <div className="flex flex-col gap-2">
          {rows.map((rule) => (
            <Card key={rule.id} className="flex items-start gap-3 p-3">
              <div className="min-w-0 flex-1">
                <p className="text-[0.875rem]" dir="auto">{rule.text}</p>
                <p className="mt-1.5 flex flex-wrap items-center gap-2 text-[0.72rem] text-muted-foreground">
                  <Badge tone={tone(rule.origin)}>{rule.origin}</Badge>
                  <span className="font-mono">{rule.scope}</span>
                  {rule.scope_ref ? <span className="font-mono">{rule.scope_ref}</span> : null}
                  {/* `rules.priorityN`: `rules.priority` is the bare label
                      "Priority", so the number was silently dropped. */}
                  <span>{t('rules.priorityN', { n: rule.priority })}</span>
                  {rule.source_pack ? <span className="font-mono">{rule.source_pack}</span> : null}
                </p>
              </div>

              {rule.locked || !rule.disableable ? (
                <span
                  className="flex shrink-0 items-center gap-1.5 text-[0.72rem] text-muted-foreground"
                  title={t('rules.lockedHint')}
                >
                  <Lock className="size-3.5" aria-hidden />
                  {t('rules.locked')}
                </span>
              ) : (
                <Switch
                  className="shrink-0"
                  aria-label={rule.text.slice(0, 60)}
                  checked={rule.enabled}
                  disabled={busy === rule.id}
                  onCheckedChange={(next) => void toggle(rule, next)}
                />
              )}
            </Card>
          ))}
        </div>
      )}
    </PageBody>
  );
}
