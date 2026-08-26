import { Blocks } from 'lucide-react';
import * as React from 'react';

import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { useLocale } from '@/i18n';
import { api } from '@/lib/api';

import { ScopePicker, useConcreteScope } from './scope';

/**
 * Business domains: which tables belong together, and what the words mean.
 *
 * Retrieval over a warehouse with four hundred tables is mostly a problem of
 * exclusion, and a domain is how a workspace says "a question about churn is
 * about these eleven tables and not the other three hundred and eighty-nine".
 *
 * Membership of a domain is emphatically *not* an access control -- the grant
 * matrix is, and it is enforced in the SQL layer. A domain that quietly
 * restricted reads would be a permission system nobody could audit.
 */

/**
 * One row of `business_domains`, named as `domain_store._out` names it.
 *
 * This read `enabled`, `table_count` and `terms`; the store sends `is_enabled`,
 * `tables` (an array) and `terminology` (an object). All three were `undefined`,
 * so every domain rendered as **disabled with zero tables** however many tables
 * it actually grouped — a screen confidently describing the opposite of the
 * data behind it.
 */
interface Domain {
  id: string;
  data_source_id: string;
  name: string;
  description: string;
  is_enabled: boolean;
  tables: string[];
  terminology: Record<string, string>;
  updated_at: string | null;
}

export default function DomainsPage() {
  const { t } = useLocale();
  const tenant = useConcreteScope();

  const [rows, setRows] = React.useState<Domain[] | null>(null);
  const [source, setSource] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!tenant) return;
    setRows(null);
    try {
      const body = await api<{ domains: Domain[]; data_source: string }>(
        `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenant)}/domains`,
      );
      setRows(body.domains ?? []);
      setSource(body.data_source ?? '');
      setError(null);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }, [tenant]);

  React.useEffect(() => {
    void load();
  }, [load]);

  return (
    <PageBody>
      <PageHeader
        title={t('tab.domains')}
        description={t('dom.blurb', { workspace: tenant })}
      />

      <Toolbar>
        <ScopePicker id="domains-scope" />
        {/* Domains are keyed (tenant, data_source): the same workspace's second
            database has its own, and saying which one this is avoids the "I
            created it and it vanished" report. */}
        {source ? (
          <span className="font-mono text-[0.75rem] text-muted-foreground">{source}</span>
        ) : null}
      </Toolbar>

      {error ? (
        <ErrorState title={t('dom.failed')} detail={error} onRetry={() => void load()}
          retryLabel={t('common.retry')} />
      ) : rows === null ? (
        <LoadingCards count={4} />
      ) : rows.length === 0 ? (
        <EmptyState
          icon={<Blocks className="size-7" />}
          title={t('dom.none')}
          hint={t('dom.noneHint')}
        />
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {rows.map((domain) => (
            <Card key={domain.id} className="p-4">
              <div className="mb-1 flex items-start justify-between gap-2">
                <h3 className="text-[0.9rem] font-semibold" dir="auto">{domain.name}</h3>
                <Badge tone={domain.is_enabled ? 'ok' : 'neutral'}>
                  {domain.is_enabled ? t('dom.on') : t('dom.off')}
                </Badge>
              </div>
              <p className="text-[0.8125rem] text-muted-foreground" dir="auto">
                {domain.description || '—'}
              </p>
              {/* Counted from the payload rather than read from a count field the
                  API does not send.

                  `dom.tableCount`, not `dom.tables`: the latter already existed
                  as the *column heading* "Tables" in the vanilla console, so
                  interpolating a count into it rendered the bare word and
                  silently dropped the number. */}
              <p className="mt-2 text-[0.72rem] text-muted-foreground">
                {t('dom.tableCount', { n: (domain.tables ?? []).length })}
                {Object.keys(domain.terminology ?? {}).length
                  ? ` · ${t('dom.termCount', { n: Object.keys(domain.terminology).length })}`
                  : ''}
              </p>
            </Card>
          ))}
        </div>
      )}
    </PageBody>
  );
}
