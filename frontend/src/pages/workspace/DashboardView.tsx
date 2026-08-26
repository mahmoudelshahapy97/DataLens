import { ArrowLeft, Download, Printer, RefreshCw } from 'lucide-react';
import * as React from 'react';
import { Link, useParams } from 'react-router-dom';

import { PlotlyChart } from '@/components/PlotlyChart';
import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useLocale } from '@/i18n';
import { api, download } from '@/lib/api';
import { currentTheme } from '@/lib/theme';
import { tileFigure } from '@/lib/tile-figure';
import { toastError } from '@/lib/toast';
import type { Dashboard, Tile, TileResult } from '@/types';

import { ParameterBar, defaultsFor, type ParameterValues } from './ParameterBar';

/**
 * One dashboard, executed.
 *
 * A full page rather than the modal the vanilla app used. A twelve-tile
 * dashboard inside a dialog is a scroll area inside a scroll area, and it has no
 * URL -- so nobody could send anybody a link to one.
 *
 * **Every tile ran as you.** `/data` executes each tile through the workspace's
 * tool registry with the caller's identity, so the row and column rules that
 * apply to this person's questions apply here too. Two people opening this page
 * can legitimately see different numbers, and the footer says so.
 */

interface DataResponse {
  results: TileResult[];
  parameters: Dashboard['parameters'];
}

/** Tiles in reading order: top to bottom, then start to end. */
function ordered(tiles: Tile[]): Tile[] {
  return [...tiles].sort((a, b) => {
    const ay = a.grid?.y ?? 0;
    const by = b.grid?.y ?? 0;
    if (ay !== by) return ay - by;
    return (a.grid?.x ?? 0) - (b.grid?.x ?? 0);
  });
}

export default function DashboardView() {
  const { t } = useLocale();
  const { dashboardId = '' } = useParams();

  const [dashboard, setDashboard] = React.useState<Dashboard | null>(null);
  const [results, setResults] = React.useState<TileResult[]>([]);
  const [values, setValues] = React.useState<ParameterValues>({});
  const [applied, setApplied] = React.useState<ParameterValues>({});
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [running, setRunning] = React.useState(false);
  const [exporting, setExporting] = React.useState(false);
  const theme = currentTheme();

  // The document first: its `parameters` decide what the bar offers, and the
  // defaults decide what the first execution asks for. Fetching data before the
  // document would render every report at no parameters at all.
  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const body = await api<{ dashboard: Dashboard }>(
          `/api/vanna/v2/dashboards/${dashboardId}`,
        );
        if (!current) return;
        setDashboard(body.dashboard);
        const defaults = defaultsFor(body.dashboard.parameters ?? []);
        setValues(defaults);
        setApplied(defaults);
      } catch (caught) {
        if (current) setError((caught as Error).message);
      } finally {
        if (current) setLoading(false);
      }
    })();
    return () => {
      current = false;
    };
  }, [dashboardId]);

  const query = React.useMemo(() => {
    const params = new URLSearchParams();
    for (const [name, value] of Object.entries(applied)) {
      if (value !== '' && value !== '..') params.set(name, value);
    }
    return params.toString();
  }, [applied]);

  const loadData = React.useCallback(
    async (alive: () => boolean = () => true) => {
      if (!dashboard) return;
      setRunning(true);
      try {
        const body = await api<DataResponse>(
          `/api/vanna/v2/dashboards/${dashboardId}/data${query ? `?${query}` : ''}`,
        );
        if (!alive()) return;
        setResults(body.results ?? []);
        setError(null);
      } catch (caught) {
        if (alive()) setError((caught as Error).message);
      } finally {
        if (alive()) setRunning(false);
      }
    },
    [dashboard, dashboardId, query],
  );

  React.useEffect(() => {
    let current = true;
    void loadData(() => current);
    return () => {
      current = false;
    };
  }, [loadData]);

  async function exportHtml() {
    setExporting(true);
    try {
      // Through `api`'s download helper, not an anchor: the export route needs
      // the tenant header as well as the cookie, and an <a href> carries only
      // the cookie -- so the server would render whichever workspace the session
      // happens to default to.
      await download(
        `/api/vanna/v2/dashboards/${dashboardId}/export${query ? `?${query}` : ''}`,
        `${dashboard?.title ?? 'dashboard'}.html`,
      );
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setExporting(false);
    }
  }

  if (loading) {
    return (
      <PageBody>
        <LoadingCards count={6} />
      </PageBody>
    );
  }

  if (error && !dashboard) {
    return (
      <PageBody>
        <ErrorState title={t('history.reload')} detail={error} />
      </PageBody>
    );
  }

  if (!dashboard) return null;

  const byTile = new Map(results.map((result) => [result.tile_id, result]));
  const tiles = ordered(dashboard.tiles ?? []);

  return (
    <PageBody>
      <PageHeader
        title={dashboard.title}
        description={dashboard.description || undefined}
        actions={
          <>
            <Button asChild data-print="hide">
              <Link to="/dashboards">
                <ArrowLeft className="arrow" />
                {t('dash.title')}
              </Link>
            </Button>
            <Button onClick={() => void loadData()} disabled={running} data-print="hide">
              <RefreshCw />
              {t('common.refresh')}
            </Button>
            <Button onClick={() => window.print()} data-print="hide">
              <Printer />
              {t('dash.print')}
            </Button>
            <Button variant="primary" onClick={() => void exportHtml()} disabled={exporting} data-print="hide">
              <Download />
              {exporting ? t('dash.exporting') : t('dash.export')}
            </Button>
          </>
        }
      />

      <ParameterBar
        parameters={dashboard.parameters ?? []}
        values={values}
        onChange={setValues}
        onApply={() => setApplied(values)}
        running={running}
      />

      {tiles.length === 0 ? (
        <EmptyState title={t('dash.noTiles')} />
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-6 lg:grid-cols-12">
          {tiles.map((tile) => (
            <TilePanel
              key={tile.id}
              tile={tile}
              result={byTile.get(tile.id) ?? null}
              running={running}
              dark={theme === 'dark'}
            />
          ))}
        </div>
      )}

      <p className="mt-5 text-[0.75rem] text-muted-foreground">
        {t('dash.executedAs')}
      </p>
      <p className="text-[0.75rem] text-muted-foreground">{t('dash.exportHint')}</p>
    </PageBody>
  );
}

function TilePanel({
  tile,
  result,
  running,
  dark,
}: {
  tile: Tile;
  result: TileResult | null;
  running: boolean;
  dark: boolean;
}) {
  const { t } = useLocale();

  // Height in grid rows, matched to the 12-column model. 80px a row is what the
  // vanilla grid used and what the seeded dashboards were laid out against.
  const height = Math.max(160, (tile.grid?.height ?? 4) * 72);
  const span = tile.grid?.width ?? 6;

  const figure = React.useMemo(() => {
    if (tile.kind === 'text' || !result || result.error) return null;
    try {
      return tileFigure(tile, result, { dark, height: height - 56 });
    } catch {
      // A figure that will not build must not take the other eleven panels with
      // it. The tile shows its data as a table instead of vanishing.
      return null;
    }
  }, [tile, result, dark, height]);

  return (
    <Card
      data-print="tile"
      className="col-span-1 flex min-w-0 flex-col overflow-hidden p-4 md:col-span-6"
      style={{ gridColumn: `span ${span} / span ${span}` }}
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          {tile.title ? (
            <h3 className="text-[0.9rem] font-semibold leading-tight" dir="auto">
              {tile.title}
            </h3>
          ) : null}
          {tile.description ? (
            <p className="mt-0.5 text-[0.75rem] text-muted-foreground" dir="auto">
              {tile.description}
            </p>
          ) : null}
        </div>
        {result?.truncated ? <Badge tone="warn">{t('result.truncated')}</Badge> : null}
      </div>

      {/* Warnings are the compiler's caveats -- a fan-out that may double-count,
          say. Rendered on the tile, because a warning that only reaches a log is
          a warning nobody acts on. */}
      {result?.warnings?.length ? (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {result.warnings.map((warning) => (
            <Badge key={warning} tone="warn">
              {warning}
            </Badge>
          ))}
        </div>
      ) : null}

      <div className="min-h-0 flex-1" style={{ minHeight: height - 56 }}>
        {tile.kind === 'text' ? (
          <p className="whitespace-pre-wrap text-[0.875rem]" dir="auto">
            {tile.text}
          </p>
        ) : running && !result ? (
          <p className="grid h-full place-items-center text-[0.8125rem] text-muted-foreground">
            {t('dash.runningTiles')}
          </p>
        ) : result?.error ? (
          // Per tile, not per dashboard: one broken query leaves the other
          // panels readable, with the failure visible on the one that failed.
          <div role="alert" className="rounded-md border border-bad/30 bg-bad/5 p-3">
            <p className="text-[0.8125rem] text-bad" dir="auto">
              {result.error}
            </p>
          </div>
        ) : !result || result.rows.length === 0 ? (
          <p className="grid h-full place-items-center text-[0.8125rem] text-muted-foreground">
            {t('dash.noData')}
          </p>
        ) : figure ? (
          <PlotlyChart
            figure={figure}
            theme={dark ? 'dark' : 'light'}
            label={tile.title || tile.kind}
            className="h-full"
          />
        ) : (
          <ScrollX className="max-h-full overflow-y-auto">
            <DataTable>
              <thead>
                <Tr>
                  {result.columns.map((column) => (
                    <Th key={column}>{column}</Th>
                  ))}
                </Tr>
              </thead>
              <Tbody>
                {result.rows.slice(0, 100).map((row, index) => (
                  <Tr key={index}>
                    {row.map((value, position) => (
                      <Td key={position} dir="auto" className="font-mono text-[0.78rem]">
                        {value === null || value === undefined ? '' : String(value)}
                      </Td>
                    ))}
                  </Tr>
                ))}
              </Tbody>
            </DataTable>
          </ScrollX>
        )}
      </div>
    </Card>
  );
}
