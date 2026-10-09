import { Download } from 'lucide-react';
import * as React from 'react';

import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/i18n';
import { post } from '@/lib/api';
import { toastError } from '@/lib/toast';

/**
 * Running a statement and showing what came back.
 *
 * Shared by Saved queries and the schema browser rather than duplicated, because
 * the three things that are easy to get subtly wrong -- the refusal shape, cell
 * rendering, and the CSV -- should be got right once.
 *
 * **Refusals are not errors.** The SQL policy answers with a structured
 * `{code, phase, message}` explaining *why* a statement was declined; that
 * message is the useful part ("this workspace is queried through its semantic
 * models") and rendering it as a generic failure throws it away. `api()` already
 * unwraps the envelope into the message, so this only has to show it plainly.
 */

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  warnings: string[];
}

export interface Runner {
  open: boolean;
  title: string;
  sql: string;
  result: QueryResult | null;
  error: string | null;
  running: boolean;
  run: (sql: string, title?: string) => Promise<void>;
  close: () => void;
}

export function useQueryRunner(): Runner {
  const [open, setOpen] = React.useState(false);
  const [title, setTitle] = React.useState('');
  const [sql, setSql] = React.useState('');
  const [result, setResult] = React.useState<QueryResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [running, setRunning] = React.useState(false);

  const run = React.useCallback(async (statement: string, label = '') => {
    setOpen(true);
    setTitle(label);
    setSql(statement);
    setResult(null);
    setError(null);
    setRunning(true);
    try {
      setResult(await post<QueryResult>('/api/vanna/v2/run-sql', { sql: statement }));
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setRunning(false);
    }
  }, []);

  const close = React.useCallback(() => setOpen(false), []);

  return { open, title, sql, result, error, running, run, close };
}

/** A value as a table cell. */
function cell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export function downloadCsv(result: QueryResult, name: string): void {
  const escape = (value: unknown) => `"${cell(value).replace(/"/g, '""')}"`;
  const csv = [
    result.columns.map(escape).join(','),
    ...result.rows.map((row) => row.map(escape).join(',')),
  ].join('\n');

  // BOM, so Excel does not read UTF-8 as the local codepage.
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${name || 'result'}.csv`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function ResultPanel({ runner }: { runner: Runner }) {
  const { t } = useLocale();
  const { open, title, sql, result, error, running, close } = runner;

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent wide>
        <DialogHeader>
          <DialogTitle dir="auto">{title || t('common.run')}</DialogTitle>
          <DialogDescription asChild>
            <pre className="sql mt-1 max-h-32 overflow-auto rounded-md border border-border bg-surface-2 p-2 font-mono text-[0.75rem]" dir="ltr">
              {sql}
            </pre>
          </DialogDescription>
        </DialogHeader>

        {running ? (
          <p className="py-8 text-center text-[0.875rem] text-muted-foreground">
            {t('common.loading')}
          </p>
        ) : error ? (
          // A refusal from the SQL policy lands here. Its message explains which
          // rule declined the statement and what to do instead, so it is shown
          // as prose rather than compressed into "query failed".
          <div role="alert" className="rounded-md border border-bad/30 bg-bad/5 p-4">
            <p className="text-[0.875rem]" dir="auto">
              {error}
            </p>
          </div>
        ) : result ? (
          <>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge tone="neutral">
                {t('history.rowsColumn')}: {result.row_count}
              </Badge>
              {result.truncated ? <Badge tone="warn">{t('result.truncated')}</Badge> : null}
              {result.warnings.map((warning) => (
                <Badge key={warning} tone="warn">
                  {warning}
                </Badge>
              ))}
              <span className="flex-1" />
              <Button
                onClick={() => downloadCsv(result, title)}
                disabled={result.rows.length === 0}
              >
                <Download />
                {t('result.downloadCsv')}
              </Button>
            </div>

            {result.rows.length === 0 ? (
              <p className="py-8 text-center text-[0.875rem] text-muted-foreground">
                {t('dash.noData')}
              </p>
            ) : (
              <ScrollX className="max-h-[52vh] overflow-y-auto rounded-md border border-border">
                <DataTable>
                  <thead className="sticky top-0 z-10 bg-surface">
                    <Tr>
                      {result.columns.map((column) => (
                        <Th key={column}>{column}</Th>
                      ))}
                    </Tr>
                  </thead>
                  <Tbody>
                    {result.rows.map((row, index) => (
                      <Tr key={index}>
                        {row.map((value, position) => (
                          <Td key={position} dir="auto" className="font-mono text-[0.78rem]">
                            {cell(value)}
                          </Td>
                        ))}
                      </Tr>
                    ))}
                  </Tbody>
                </DataTable>
              </ScrollX>
            )}
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/** Shared by callers that want to run without the dialog. */
export async function runSql(sql: string): Promise<QueryResult | null> {
  try {
    return await post<QueryResult>('/api/vanna/v2/run-sql', { sql });
  } catch (caught) {
    toastError((caught as Error).message);
    return null;
  }
}
