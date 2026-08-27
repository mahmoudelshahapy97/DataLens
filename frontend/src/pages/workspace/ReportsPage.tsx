import { CalendarClock, Download, Pause, Play, Plus, Trash2 } from 'lucide-react';
import * as React from 'react';

import { useSession } from '@/app/session';
import { useConfirm } from '@/components/primitives/confirm';
import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingRows } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useLocale } from '@/i18n';
import { api, del, download, patch, post } from '@/lib/api';
import { relative, until } from '@/lib/time';
import { toast, toastError } from '@/lib/toast';

import { ScheduleEditor, type ReportSchedule } from './ScheduleEditor';

/**
 * Scheduled reports.
 *
 * A report is a dashboard that declares parameters, plus a schedule and a
 * delivery list -- there is no separate report document. The column that matters
 * most on this screen is **Runs as**: the tiles execute with that member's
 * permissions, so it decides whose view of the data gets delivered. It is shown
 * on every row rather than hidden in the editor for exactly that reason.
 */

interface ReportRun {
  id: string;
  status: string;
  run_at: string;
  finished_at: string | null;
  tile_count: number;
  row_count: number;
  error: string;
  artifact_filename: string;
  artifact_bytes: number;
}

function statusTone(status: string): 'ok' | 'err' | 'warn' | 'neutral' {
  if (status === 'succeeded') return 'ok';
  if (status === 'failed') return 'err';
  if (status === 'running' || status === 'claimed') return 'warn';
  return 'neutral';
}

export default function ReportsPage() {
  const { t, locale } = useLocale();
  const { canAuthor } = useSession();
  const confirm = useConfirm();

  const [reports, setReports] = React.useState<ReportSchedule[]>([]);
  const [runs, setRuns] = React.useState<Record<string, ReportRun[]>>({});
  const [expanded, setExpanded] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<ReportSchedule | 'new' | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const load = React.useCallback(async (alive: () => boolean = () => true) => {
    try {
      const body = await api<{ reports: ReportSchedule[] }>('/api/vanna/v2/reports');
      if (!alive()) return;
      setReports(body.reports ?? []);
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

  async function openRuns(report: ReportSchedule) {
    const next = expanded === report.id ? null : report.id;
    setExpanded(next);
    if (next && !runs[next]) {
      try {
        const body = await api<{ runs: ReportRun[] }>(
          `/api/vanna/v2/reports/${report.id}/runs`,
        );
        setRuns((current) => ({ ...current, [report.id]: body.runs ?? [] }));
      } catch (caught) {
        toastError((caught as Error).message);
      }
    }
  }

  async function toggle(report: ReportSchedule) {
    try {
      const body = await patch<{ report: ReportSchedule }>(
        `/api/vanna/v2/reports/${report.id}`,
        { is_active: !report.is_active },
      );
      setReports((current) =>
        current.map((r) => (r.id === report.id ? body.report : r)),
      );
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function runNow(report: ReportSchedule) {
    try {
      await post(`/api/vanna/v2/reports/${report.id}/run`);
      // Queued, not executed: a twelve-tile dashboard against a slow warehouse
      // outlasts any sensible HTTP timeout, so the button reports acceptance and
      // the run appears in the history when the scheduler picks it up.
      toast(t('report.queued'));
      setRuns((current) => {
        const next = { ...current };
        delete next[report.id];
        return next;
      });
      if (expanded === report.id) void openRuns(report);
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  async function remove(report: ReportSchedule) {
    const ok = await confirm.ask({
      title: t('report.deleteTitle'),
      body: t('report.deleteConfirm', { name: report.name }),
      confirmLabel: t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/vanna/v2/reports/${report.id}`);
      setReports((current) => current.filter((r) => r.id !== report.id));
      toast(t('common.deleted'));
    } catch (caught) {
      toastError((caught as Error).message);
    }
  }

  return (
    <PageBody>
      {confirm.dialog}
      <PageHeader
        title={t('nav.reports')}
        description={t('report.sub')}
        actions={
          canAuthor ? (
            <Button variant="primary" onClick={() => setEditing('new')}>
              <Plus />
              {t('report.new')}
            </Button>
          ) : null
        }
      />

      {loading ? (
        <LoadingRows rows={5} />
      ) : error ? (
        <ErrorState title={t('history.reload')} detail={error} onRetry={() => void load()} />
      ) : reports.length === 0 ? (
        <EmptyState
          icon={<CalendarClock className="size-7" />}
          title={t('report.empty')}
          hint={t('report.emptyHint')}
          action={
            canAuthor ? (
              <Button variant="primary" onClick={() => setEditing('new')}>
                <Plus />
                {t('report.new')}
              </Button>
            ) : null
          }
        />
      ) : (
        <ScrollX>
          <DataTable>
            <thead>
              <Tr>
                <Th>{t('report.name')}</Th>
                <Th>{t('report.schedule')}</Th>
                <Th>{t('report.runsAs')}</Th>
                <Th>{t('report.channels')}</Th>
                <Th>{t('report.nextRun')}</Th>
                <Th className="w-44" />
              </Tr>
            </thead>
            <Tbody>
              {reports.map((report) => (
                <React.Fragment key={report.id}>
                  <Tr className="cursor-pointer" onClick={() => void openRuns(report)}>
                    <Td>
                      <span className="font-medium" dir="auto">
                        {report.name}
                      </span>
                      {report.dashboard_title ? (
                        <span className="block text-[0.75rem] text-muted-foreground" dir="auto">
                          {report.dashboard_title}
                        </span>
                      ) : null}
                    </Td>
                    <Td className="text-muted-foreground">
                      {report.schedule_label || report.cron}
                      <span className="block text-[0.75rem]">{report.timezone}</span>
                    </Td>
                    {/* Whose permissions produce the numbers. */}
                    <Td dir="auto" className="font-mono text-[0.78rem]">
                      {report.run_as}
                    </Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {(report.channels ?? []).map((channel, index) => (
                          <Badge key={index} tone="neutral">
                            {channel.kind}
                          </Badge>
                        ))}
                      </div>
                    </Td>
                    <Td className="whitespace-nowrap text-muted-foreground">
                      {report.is_active ? (
                        until(report.next_run_at, t, locale) || '—'
                      ) : (
                        <Badge tone="warn">{t('report.paused')}</Badge>
                      )}
                    </Td>
                    <Td>
                      <div className="flex items-center justify-end gap-0.5">
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={report.is_active ? t('report.pause') : t('report.resume')}
                          title={report.is_active ? t('report.pause') : t('report.resume')}
                          onClick={(event) => {
                            event.stopPropagation();
                            void toggle(report);
                          }}
                        >
                          {report.is_active ? <Pause /> : <Play />}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={(event) => {
                            event.stopPropagation();
                            void runNow(report);
                          }}
                        >
                          {t('report.runNow')}
                        </Button>
                        {canAuthor ? (
                          <Button
                            size="icon"
                            variant="ghost"
                            aria-label={t('common.delete')}
                            onClick={(event) => {
                              event.stopPropagation();
                              void remove(report);
                            }}
                          >
                            <Trash2 />
                          </Button>
                        ) : null}
                      </div>
                    </Td>
                  </Tr>

                  {expanded === report.id ? (
                    <tr>
                      <Td colSpan={6} className="pt-0">
                        <RunHistory runs={runs[report.id]} />
                      </Td>
                    </tr>
                  ) : null}
                </React.Fragment>
              ))}
            </Tbody>
          </DataTable>
        </ScrollX>
      )}

      {editing ? (
        <ScheduleEditor
          schedule={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      ) : null}
    </PageBody>
  );
}

function RunHistory({ runs }: { runs: ReportRun[] | undefined }) {
  const { t, locale } = useLocale();

  if (!runs) {
    return <p className="py-3 text-[0.8125rem] text-muted-foreground">{t('common.loading')}</p>;
  }
  if (runs.length === 0) {
    return <p className="py-3 text-[0.8125rem] text-muted-foreground">{t('report.noRuns')}</p>;
  }

  return (
    <div className="rounded-md border border-border bg-surface-2 p-2">
      <DataTable>
        <thead>
          <Tr>
            <Th>{t('audit.time')}</Th>
            <Th className="w-28">{t('history.status')}</Th>
            <Th className="w-20 text-end">{t('dash.tilesColumn')}</Th>
            <Th className="w-24 text-end">{t('history.rowsColumn')}</Th>
            <Th>{t('report.artifact')}</Th>
          </Tr>
        </thead>
        <Tbody>
          {runs.map((run) => (
            <Tr key={run.id}>
              <Td className="whitespace-nowrap text-muted-foreground" title={run.run_at}>
                {relative(run.finished_at || run.run_at, t, locale)}
              </Td>
              <Td>
                <Badge tone={statusTone(run.status)}>{run.status}</Badge>
              </Td>
              <Td className="text-end tabular-nums">{run.tile_count}</Td>
              <Td className="text-end tabular-nums">{run.row_count.toLocaleString()}</Td>
              <Td>
                {run.artifact_bytes > 0 ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      void download(
                        `/api/vanna/v2/reports/runs/${run.id}/artifact`,
                        run.artifact_filename,
                      ).catch((caught) => toastError((caught as Error).message))
                    }
                  >
                    <Download />
                    {Math.round(run.artifact_bytes / 1024)} KB
                  </Button>
                ) : run.error ? (
                  <span className="text-[0.78rem] text-bad" dir="auto">
                    {run.error}
                  </span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </Td>
            </Tr>
          ))}
        </Tbody>
      </DataTable>
    </div>
  );
}
