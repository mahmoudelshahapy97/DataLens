import * as React from 'react';

import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useLocale } from '@/i18n';
import { api, post, del } from '@/lib/api';
import { toastError, toastSuccess } from '@/lib/toast';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * Ready-made instruction packs.
 *
 * A workspace starts knowing nothing about its own domain, and the difference
 * between a useful answer and a plausible one is usually a convention nobody
 * thought to write down -- that a balance is a level and a payment is a flow,
 * that "revenue" excludes tax. These are those conventions, per industry,
 * written once.
 *
 * Enabling a pack copies its rules in as `pack`-origin instructions, which the
 * Instructions screen can then disable individually. Removing it takes them back
 * out. Nothing here edits a rule in place: a pack is a source, and a rule edited
 * in place would silently diverge from it.
 */

interface Pack {
  id: string;
  name: string;
  description: string;
  instruction_count: number;
  enabled: boolean;
  preview: string[];
}

export default function LibraryPage() {
  const { t } = useLocale();
  const tenant = useConcreteScope();

  const [rows, setRows] = React.useState<Pack[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setRows(null);
    try {
      const body = await api<{ packs: Pack[] }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/instruction-packs`,
      );
      setRows(body.packs ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const setEnabled = async (pack: Pack, enabled: boolean) => {
    setBusy(pack.id);
    const base =
      `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}` +
      `/instruction-packs/${encodeURIComponent(pack.id)}`;
    try {
      if (enabled) await post(`${base}/enable`);
      else await del(base);
      // `lib.packEnabled`/`lib.packRemoved`, not `lib.enabled`/`lib.removed`:
      // those already existed as "added" and "Removed {n} rules", so passing a
      // {name} rendered the old wording and dropped the pack entirely.
      toastSuccess(
        enabled
          ? t('lib.packEnabled', { name: pack.name })
          : t('lib.packRemoved', { name: pack.name }),
      );
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <PageBody>
      <PageHeader title={t('tab.library')} description={t('lib.blurb', { workspace: tenant })} />

      <Toolbar>
        <ScopePicker id="library-scope" />
      </Toolbar>

      {error ? (
        <ErrorState title={t('lib.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingCards count={6} />
      ) : rows.length === 0 ? (
        <EmptyState title={t('lib.none')} />
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((pack) => (
            <Card key={pack.id} className="flex flex-col p-4">
              <div className="mb-1 flex items-start justify-between gap-2">
                <h3 className="text-[0.9rem] font-semibold" dir="auto">{pack.name}</h3>
                {pack.enabled ? <Badge tone="good">{t('lib.on')}</Badge> : null}
              </div>
              <p className="text-[0.8125rem] text-muted-foreground" dir="auto">
                {pack.description}
              </p>

              {pack.preview?.length ? (
                <ul className="mt-2 list-disc space-y-1 ps-4 text-[0.75rem] text-muted-foreground">
                  {pack.preview.slice(0, 3).map((line, index) => (
                    <li key={index} dir="auto">{line}</li>
                  ))}
                </ul>
              ) : null}

              <div className="mt-3 flex items-center justify-between gap-2 pt-1">
                <span className="text-[0.72rem] text-muted-foreground">
                  {t('lib.ruleCount', { n: pack.instruction_count })}
                </span>
                <Button
                  size="sm"
                  variant={pack.enabled ? 'danger' : 'primary'}
                  disabled={busy === pack.id}
                  onClick={() => void setEnabled(pack, !pack.enabled)}
                >
                  {pack.enabled ? t('lib.remove') : t('lib.enable')}
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </PageBody>
  );
}
