import { Check, GitMerge, Undo2, X } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/i18n';
import { api, put } from '@/lib/api';
import { toastError } from '@/lib/toast';

/**
 * Joins the scanner inferred from column names, for an admin to confirm.
 *
 * A database that declares no foreign keys gives the agent no join paths, so
 * the scanner proposes them (`orders.customer_id` -> `customers.id`) and samples
 * the data to score each one. Confident proposals are used straight away and
 * labelled as inferred in the prompt; this is where somebody who knows the data
 * accepts the right ones -- they then count as much as a declared key -- and
 * rejects the wrong ones, which stay rejected across rescans.
 *
 * Renders nothing when there is nothing to review, which is every database
 * whose foreign keys are declared.
 */

type ReviewStatus = 'accepted' | 'proposed' | 'rejected';

interface InferredJoin {
  from_table_key: string;
  from_column_key: string;
  to_table_key: string;
  to_column_key: string;
  confidence: number | null;
  review_status: ReviewStatus;
  reviewed_by: string | null;
  in_use: boolean;
}

interface InferredResponse {
  relationships: InferredJoin[];
  min_confidence: number;
}

const STATUS_TONE: Record<ReviewStatus, 'good' | 'warn' | 'bad'> = {
  accepted: 'good',
  proposed: 'warn',
  rejected: 'bad',
};

function keyOf(join: InferredJoin): string {
  return `${join.from_table_key}.${join.from_column_key}->${join.to_table_key}.${join.to_column_key}`;
}

export function InferredJoinsPanel({ tenantId, refreshKey }: { tenantId: string; refreshKey: number }) {
  const { t } = useLocale();
  const [joins, setJoins] = React.useState<InferredJoin[]>([]);
  const [minConfidence, setMinConfidence] = React.useState(0.8);
  const [busy, setBusy] = React.useState<string | null>(null);

  const base = `/api/vanna/v2/admin/tenants/${encodeURIComponent(tenantId)}/catalog/relationships`;

  const load = React.useCallback(
    async (alive: () => boolean = () => true) => {
      try {
        const body = await api<InferredResponse>(`${base}/inferred`);
        if (!alive()) return;
        setJoins(body.relationships ?? []);
        setMinConfidence(body.min_confidence ?? 0.8);
      } catch {
        // Older control planes have no such route; the panel simply stays hidden.
        if (alive()) setJoins([]);
      }
    },
    [base],
  );

  React.useEffect(() => {
    let current = true;
    void load(() => current);
    return () => {
      current = false;
    };
  }, [load, refreshKey]);

  async function review(join: InferredJoin, decision: ReviewStatus) {
    const id = keyOf(join);
    setBusy(id);
    try {
      await put(`${base}/review`, {
        from_table: join.from_table_key,
        from_column: join.from_column_key,
        to_table: join.to_table_key,
        to_column: join.to_column_key,
        decision,
      });
      await load();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (joins.length === 0) return null;

  return (
    <section className="mt-6" aria-labelledby="inferred-joins-title">
      <div className="mb-2 flex items-center gap-2">
        <GitMerge className="size-4 text-muted-foreground" />
        <h3 id="inferred-joins-title" className="text-[0.95rem] font-semibold">
          {t('schema.inferredJoins')}
        </h3>
        <Badge tone="neutral">{joins.length}</Badge>
      </div>
      <p className="mb-3 text-[0.8125rem] text-muted-foreground">
        {t('schema.inferredJoinsHelp', { pct: Math.round(minConfidence * 100) })}
      </p>
      <ScrollX className="rounded-md border border-border">
        <DataTable>
          <thead>
            <Tr>
              <Th>{t('schema.inferredFrom')}</Th>
              <Th>{t('schema.inferredTo')}</Th>
              <Th className="w-24">{t('schema.inferredConfidence')}</Th>
              <Th className="w-32">{t('schema.inferredStatus')}</Th>
              <Th className="w-40" />
            </Tr>
          </thead>
          <Tbody>
            {joins.map((join) => {
              const id = keyOf(join);
              return (
                <Tr key={id}>
                  <Td className="font-mono text-[0.78rem]">
                    {join.from_table_key}.{join.from_column_key}
                  </Td>
                  <Td className="font-mono text-[0.78rem]">
                    {join.to_table_key}.{join.to_column_key}
                  </Td>
                  <Td className="tabular-nums">
                    {join.confidence == null ? '—' : `${Math.round(join.confidence * 100)}%`}
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      <Badge tone={STATUS_TONE[join.review_status]}>
                        {t(`schema.inferred_${join.review_status}`)}
                      </Badge>
                      {join.in_use ? <Badge tone="info">{t('schema.inferredInUse')}</Badge> : null}
                    </div>
                  </Td>
                  <Td>
                    <div className="flex justify-end gap-1">
                      {join.review_status !== 'accepted' ? (
                        <Button
                          size="sm"
                          disabled={busy === id}
                          onClick={() => void review(join, 'accepted')}
                        >
                          <Check />
                          {t('schema.inferredAccept')}
                        </Button>
                      ) : null}
                      {join.review_status !== 'rejected' ? (
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={busy === id}
                          onClick={() => void review(join, 'rejected')}
                        >
                          <X />
                          {t('schema.inferredReject')}
                        </Button>
                      ) : null}
                      {join.review_status !== 'proposed' ? (
                        <Button
                          size="icon"
                          variant="ghost"
                          disabled={busy === id}
                          aria-label={t('schema.inferredUndo')}
                          title={t('schema.inferredUndo')}
                          onClick={() => void review(join, 'proposed')}
                        >
                          <Undo2 className="size-3.5" />
                        </Button>
                      ) : null}
                    </div>
                  </Td>
                </Tr>
              );
            })}
          </Tbody>
        </DataTable>
      </ScrollX>
    </section>
  );
}
