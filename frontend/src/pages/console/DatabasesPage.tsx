import { Database } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';
import { relative } from '@/lib/time';
import { toastError, toastSuccess } from '@/lib/toast';
import type { DataSource } from '@/types';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * The databases a workspace may be asked about, and whether they answer.
 *
 * A source was probed once, when it was registered, and the result was thrown
 * away with the request -- so a rotated password or a moved host looked exactly
 * like a healthy source until somebody asked a question and got an error they
 * had no way to interpret. The check here writes its result back, which is what
 * makes the column mean anything an hour later.
 *
 * `last_ok` is tri-state and the third state is the dangerous one: `null` is
 * *unknown*, and it must never render the same as "working".
 */
export default function DatabasesPage() {
  const { t, locale } = useLocale();
  const tenant = useConcreteScope();

  const [rows, setRows] = React.useState<DataSource[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [checking, setChecking] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setRows(null);
    try {
      const body = await api<{ data_sources: DataSource[] }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/datasources`,
      );
      setRows(body.data_sources ?? []);
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const check = async (source: DataSource) => {
    setChecking(source.data_source_id);
    try {
      const result = await api<{ ok?: boolean; error?: string }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}` +
          `/datasources/${source.data_source_id}/health`,
      );
      if (result.ok) toastSuccess(t('db.checkOk', { label: source.label }));
      else toastError(result.error || t('db.checkFailed', { label: source.label }));
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setChecking(null);
    }
  };

  return (
    <PageBody>
      <PageHeader title={t('nav.datasources')} description={t('db.blurb', { workspace: tenant })} />

      <Toolbar>
        <ScopePicker id="db-scope" />
      </Toolbar>

      {error ? (
        <ErrorState title={t('db.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingRows rows={4} />
      ) : rows.length === 0 ? (
        <EmptyState icon={<Database className="size-7" />} title={t('ov.noSources')} />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('ov.source')}</Th>
                <Th>{t('ov.status')}</Th>
                <Th>{t('ov.checked')}</Th>
                <Th />
              </Tr>
            </thead>
            <Tbody>
              {rows.map((source) => (
                <Tr key={source.data_source_id}>
                  <Td>
                    <span className="font-mono text-[0.78rem]">{source.label}</span>
                    {source.is_default ? (
                      <Badge tone="admin" className="ms-2">{t('db.default')}</Badge>
                    ) : null}
                  </Td>
                  <Td>
                    {source.last_ok === true ? (
                      <Badge tone="ok">{t('ov.healthOk')}</Badge>
                    ) : source.last_ok === false ? (
                      <Badge tone="err">{t('ov.healthFailing')}</Badge>
                    ) : (
                      <Badge tone="warn">{t('ov.healthUnknown')}</Badge>
                    )}
                    {source.last_error ? (
                      <span className="mt-1 block text-[0.72rem] text-muted-foreground" dir="auto">
                        {source.last_error}
                      </span>
                    ) : null}
                  </Td>
                  <Td className="text-muted-foreground">
                    {source.last_checked_at
                      ? relative(source.last_checked_at, t, locale)
                      : t('ov.never')}
                  </Td>
                  <Td>
                    <Button
                      size="sm"
                      disabled={checking === source.data_source_id}
                      onClick={() => void check(source)}
                    >
                      {checking === source.data_source_id ? t('db.checking') : t('db.check')}
                    </Button>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}
    </PageBody>
  );
}
