import { BookmarkPlus, Download, ThumbsDown, ThumbsUp, Trash2 } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader, Toolbar } from '@/components/primitives/page';
import { useConfirm } from '@/components/primitives/confirm';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';

import { SaveQueryDialog } from './SaveQueryDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useSession } from '@/app/session';
import { useLocale } from '@/i18n';
import { api, del, post } from '@/lib/api';
import { relative } from '@/lib/time';
import { toast, toastError } from '@/lib/toast';

/**
 * Every question this workspace asked, and what came back.
 *
 * Two things here are not obvious from the screen.
 *
 * **A blank question is not a bug.** History rows carry a question only when one
 * was *asked*: the text is captured by a lifecycle hook on the agent's
 * `before_message`, so anything that posts SQL directly -- including this app's
 * own Run SQL button, and every seeded dashboard tile -- records the statement
 * and leaves the question empty. The row still says what ran, which is the part
 * an audit needs.
 *
 * **The CSV is built here, from what is on screen.** It exports the rows the
 * filters selected rather than the whole table, because the alternative is a
 * download that silently disagrees with the page that produced it.
 */

interface HistoryRow {
  id: string;
  question: string;
  sql: string;
  status: string;
  error: string | null;
  row_count: number | null;
  execution_ms: number | null;
  feedback: number | null;
  user_id: string;
  conversation_id: string | null;
  /** Required by `/feedback`, which keys the rating by request, not by row id. */
  request_id: string | null;
  model: string | null;
  cost_usd: number | null;
  created_at: string;
}

/**
 * The statuses the API will actually accept.
 *
 * `_HISTORY_STATUSES` in `routes/data.py`. This offered `refused`, which is not
 * one of them -- selecting it returned 400 "Unknown status" and the page showed
 * an error where the filtered list should have been. The real name is
 * `rejected_by_policy`, and three more existed that could not be filtered for
 * at all.
 *
 * `empty` is worth having in the list: a query that ran perfectly and returned
 * nothing is the signature of an invented filter literal, which is the most
 * common way an answer is confidently wrong.
 */
const STATUSES = [
  '',
  'valid',
  'empty',
  'invalid',
  'rejected_by_policy',
  'timeout',
  'error',
] as const;

function statusTone(status: string): 'good' | 'bad' | 'warn' | 'neutral' {
  if (status === 'valid') return 'good';
  if (status === 'invalid' || status === 'error') return 'bad';
  if (status === 'rejected_by_policy' || status === 'timeout' || status === 'empty') return 'warn';
  return 'neutral';
}

export default function HistoryPage() {
  const { t, locale } = useLocale();
  const { me } = useSession();
  const confirm = useConfirm();

  const [search, setSearch] = React.useState('');
  const [mineOnly, setMineOnly] = React.useState(false);
  const [status, setStatus] = React.useState('');
  const [since, setSince] = React.useState('');
  const [until, setUntil] = React.useState('');

  const [rows, setRows] = React.useState<HistoryRow[]>([]);
  const [expanded, setExpanded] = React.useState<string | null>(null);
  const [saving, setSaving] = React.useState<{ question: string; sql: string } | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(
    async (alive: () => boolean = () => true) => {
      const params = new URLSearchParams({ limit: '200' });
      if (search.trim()) params.set('search', search.trim());
      if (mineOnly && me?.user.email) params.set('user_id', me.user.email);
      if (status) params.set('status', status);
      if (since) params.set('since', since);
      if (until) params.set('until', until);

      try {
        const body = await api<{ history: HistoryRow[] }>(
          `/api/vanna/v2/history?${params}`,
        );
        if (!alive()) return;
        setRows(body.history ?? []);
        setError(null);
      } catch (caught) {
        if (!alive()) return;
        setError((caught as Error).message);
      } finally {
        if (alive()) setLoading(false);
      }
    },
    [search, mineOnly, status, since, until, me?.user.email],
  );

  React.useEffect(() => {
    setLoading(true);
    let current = true;
    // Debounced: the search box refetches on every keystroke otherwise, and the
    // responses can land out of order so a slow early query overwrites a fast
    // later one.
    const timer = window.setTimeout(() => void load(() => current), 250);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [load]);

  async function rate(row: HistoryRow, value: number) {
    // `/feedback` keys a rating by the request that produced the answer, not by
    // the history row's id. This used to post `{generation_id, feedback}` --
    // none of the three fields the endpoint declares -- so every thumb was a
    // 422 reading "Field required; Field required; Field required", and because
    // that text was announced into a live region it then followed the user onto
    // every other page.
    if (!row.request_id) {
      toastError(t('history.cannotRate'));
      return;
    }

    // The API takes 'positive' or 'negative' and has no way to express "no
    // opinion", so clicking the lit thumb cannot clear the rating -- say so
    // rather than sending something that will be rejected.
    if (row.feedback === value) {
      toast(t('history.alreadyRated'));
      return;
    }

    // Optimistic: the thumb is the feedback. Waiting for a round trip to fill it
    // in makes the button feel broken on a slow connection.
    setRows((current) =>
      current.map((r) => (r.id === row.id ? { ...r, feedback: value } : r)),
    );
    try {
      await post('/api/vanna/v2/feedback', {
        request_id: row.request_id,
        conversation_id: row.conversation_id || '',
        rating: value > 0 ? 'positive' : 'negative',
        // Carried so a positive rating can promote the pair straight into the
        // verified example store, which is the point of collecting it.
        question: row.question || '',
        sql: row.sql || '',
      });
      toast(t('history.rated'));
    } catch (caught) {
      setRows((current) =>
        current.map((r) => (r.id === row.id ? { ...r, feedback: row.feedback } : r)),
      );
      toastError((caught as Error).message);
    }
  }

  async function removeRow(row: HistoryRow) {
    const ok = await confirm.ask({
      title: t('history.deleteTitle'),
      body: row.question || row.sql.slice(0, 160),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/history/${row.id}`);
      setRows((current) => current.filter((r) => r.id !== row.id));
      toast(t('common.deleted'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function clearMine() {
    const ok = await confirm.ask({
      title: t('history.clearTitle'),
      body: t('history.clearBody'),
      confirmLabel: t('history.clearMine'),
      danger: true,
    });
    if (!ok) return;
    try {
      // The count comes back from the server; `history.cleared` is
      // "{count} removed" and used to render the braces to the user.
      const body = await del<{ deleted?: number }>('/api/vanna/v2/history');
      toast(t('history.cleared', { count: body?.deleted ?? 0 }));
      void load();
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  function exportCsv() {
    const header = ['created_at', 'user', 'question', 'sql', 'status', 'rows', 'ms', 'model', 'cost_usd'];
    const escape = (value: unknown) => {
      const text = value == null ? '' : String(value);
      // Quote unconditionally. A SQL statement contains commas, newlines and
      // quotes, and deciding per-field which of those needs escaping is how a
      // CSV ends up with one row shifted by a column.
      return `"${text.replace(/"/g, '""')}"`;
    };
    const csv = [
      header.join(','),
      ...rows.map((r) =>
        [r.created_at, r.user_id, r.question, r.sql, r.status, r.row_count, r.execution_ms, r.model, r.cost_usd]
          .map(escape)
          .join(','),
      ),
    ].join('\n');

    // BOM: Excel reads a plain UTF-8 CSV as the local codepage and mangles every
    // non-ASCII character, which for an Arabic workspace is most of the file.
    const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `history-${new Date().toISOString().slice(0, 10)}.csv`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('history.title')}
        description={t('history.sub')}
        actions={
          <>
            <Button onClick={exportCsv} disabled={rows.length === 0}>
              <Download />
              {t('history.export')}
            </Button>
            <Button variant="danger" onClick={() => void clearMine()}>
              <Trash2 />
              {t('history.clearMine')}
            </Button>
          </>
        }
      />

      <Toolbar>
        <Input
          className="min-w-[220px] flex-1"
          type="search"
          value={search}
          placeholder={t('history.search')}
          aria-label={t('history.search')}
          onChange={(event) => setSearch(event.target.value)}
        />

        <div className="flex items-center gap-2">
          <Switch id="history-mine" checked={mineOnly} onCheckedChange={setMineOnly} />
          <Label htmlFor="history-mine">{t('history.onlyMine')}</Label>
        </div>

        <Select value={status || 'any'} onValueChange={(v) => setStatus(v === 'any' ? '' : v)}>
          <SelectTrigger className="w-40" aria-label={t('history.status')}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUSES.map((s) => (
              <SelectItem key={s || 'any'} value={s || 'any'}>
                {s ? t(`history.status.${s}`) : t('history.anyStatus')}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <div className="flex items-center gap-1.5">
          <Label htmlFor="history-since">{t('history.from')}</Label>
          <Input
            id="history-since"
            type="date"
            className="w-40"
            value={since}
            onChange={(event) => setSince(event.target.value)}
          />
          <Label htmlFor="history-until">{t('history.to')}</Label>
          <Input
            id="history-until"
            type="date"
            className="w-40"
            value={until}
            onChange={(event) => setUntil(event.target.value)}
          />
        </div>
      </Toolbar>

      {loading ? (
        <LoadingRows rows={10} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : rows.length === 0 ? (
        <EmptyState title={t('history.empty')} hint={t('history.sub')} />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th className="w-32">{t('audit.time')}</Th>
                <Th>{t('history.title')}</Th>
                <Th className="w-24">{t('history.status')}</Th>
                <Th className="w-20 text-end">{t('history.rowsColumn')}</Th>
                <Th className="w-32">{t('audit.actor')}</Th>
                <Th className="w-28" />
              </Tr>
            </thead>
            <Tbody>
              {rows.map((row) => (
                <React.Fragment key={row.id}>
                  <Tr
                    className="cursor-pointer"
                    onClick={() => setExpanded(expanded === row.id ? null : row.id)}
                  >
                    <Td className="whitespace-nowrap text-muted-foreground" title={row.created_at}>
                      {relative(row.created_at, t, locale)}
                    </Td>
                    <Td dir="auto" className="max-w-xl">
                      {row.question ? (
                        <span>{row.question}</span>
                      ) : (
                        // Not an error state: a row with no question came from
                        // Run SQL or a dashboard tile, which never runs the hook
                        // that captures one.
                        <span className="text-muted-foreground italic">
                          {t('history.noQuestion')}
                        </span>
                      )}
                    </Td>
                    <Td>
                      <Badge tone={statusTone(row.status)}>{row.status}</Badge>
                    </Td>
                    <Td className="text-end tabular-nums">{row.row_count ?? '—'}</Td>
                    <Td dir="auto" className="text-muted-foreground">{row.user_id}</Td>
                    <Td>
                      <div className="flex items-center justify-end gap-0.5">
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t('history.rateUp')}
                          title={t('history.rateUp')}
                          className={row.feedback === 1 ? 'text-good' : undefined}
                          onClick={(event) => {
                            event.stopPropagation();
                            void rate(row, 1);
                          }}
                        >
                          <ThumbsUp />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t('history.rateDown')}
                          title={t('history.rateDown')}
                          className={row.feedback === -1 ? 'text-bad' : undefined}
                          onClick={(event) => {
                            event.stopPropagation();
                            void rate(row, -1);
                          }}
                        >
                          <ThumbsDown />
                        </Button>
                        {/* History already holds the question and the SQL it
                            produced, so this is the shortest path to a saved
                            query -- and therefore to a dashboard tile. */}
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t('history.saveOne')}
                          title={t('history.saveOne')}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSaving({ question: row.question || '', sql: row.sql || '' });
                          }}
                        >
                          <BookmarkPlus />
                        </Button>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={t('history.deleteOne')}
                          title={t('history.deleteOne')}
                          onClick={(event) => {
                            event.stopPropagation();
                            void removeRow(row);
                          }}
                        >
                          <Trash2 />
                        </Button>
                      </div>
                    </Td>
                  </Tr>

                  {expanded === row.id ? (
                    <tr>
                      <Td colSpan={6} className="pt-0">
                        <div className="rounded-md border border-border bg-surface-2 p-3">
                          <pre className="sql overflow-x-auto font-mono text-[0.78rem]" dir="ltr">
                            {row.sql}
                          </pre>
                          {row.error ? (
                            <p className="mt-2 text-[0.8125rem] text-bad" dir="auto">
                              {row.error}
                            </p>
                          ) : null}
                          <p className="mt-2 flex flex-wrap gap-3 text-[0.75rem] text-muted-foreground">
                            {row.execution_ms != null ? <span>{row.execution_ms} ms</span> : null}
                            {row.model ? <span>{row.model}</span> : null}
                            {row.cost_usd != null ? <span>${row.cost_usd.toFixed(4)}</span> : null}
                          </p>
                        </div>
                      </Td>
                    </tr>
                  ) : null}
                </React.Fragment>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}
      {saving ? (
        <SaveQueryDialog
          question={saving.question}
          sql={saving.sql}
          onClose={() => setSaving(null)}
        />
      ) : null}
    </PageBody>
  );
}
