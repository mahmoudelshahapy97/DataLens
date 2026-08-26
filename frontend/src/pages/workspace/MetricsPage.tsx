import { BarChart3, Play } from 'lucide-react';
import * as React from 'react';

import { PlotlyChart } from '@/components/PlotlyChart';
import { DataTable, ScrollX, Tbody, Td, Th, Tr } from '@/components/primitives/data-table';
import { PageBody, PageHeader } from '@/components/primitives/page';
import { EmptyState, ErrorState, LoadingCards } from '@/components/primitives/states';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { currentTheme } from '@/lib/theme';
import { tileFigure } from '@/lib/tile-figure';
import { toastError } from '@/lib/toast';

/**
 * The semantic layer, explored directly.
 *
 * A cube's measures carry their own aggregation -- decided by whoever defined the
 * cube -- which is why this is safer than writing the equivalent SQL by hand: a
 * query built here cannot invent a SUM that double-counts across a one-to-many
 * join, because the SUM was not invented here.
 *
 * Hidden from the navigation entirely when a workspace has no manifest. An empty
 * cube explorer is not a feature waiting for data; it is a screen that does not
 * apply.
 */

interface Measure {
  name: string;
  expression: string;
  description?: string;
}

interface Cube {
  name: string;
  base_object: string;
  description: string;
  measures: Measure[];
  /** Plain names. The API sends strings here, not objects. */
  dimensions: string[];
  time_dimensions: string[];
}

const GRAINS = ['day', 'week', 'month', 'quarter', 'year'] as const;

export default function MetricsPage() {
  const { t } = useLocale();
  const dark = currentTheme() === 'dark';

  const [cubes, setCubes] = React.useState<Cube[]>([]);
  const [name, setName] = React.useState('');
  const [measures, setMeasures] = React.useState<string[]>([]);
  const [dimensions, setDimensions] = React.useState<string[]>([]);
  const [timeDimension, setTimeDimension] = React.useState('');
  const [grain, setGrain] = React.useState<string>('month');

  const [result, setResult] = React.useState<{ columns: string[]; rows: unknown[][] } | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [running, setRunning] = React.useState(false);

  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const body = await api<{ cubes: Cube[] }>('/api/vanna/v2/cubes');
        if (!current) return;
        setCubes(body.cubes ?? []);
        const first = body.cubes?.[0];
        if (first) {
          setName(first.name);
          setMeasures(first.measures[0] ? [first.measures[0].name] : []);
        }
        setError(null);
      } catch (caught) {
        if (current) setError((caught as Error).message);
      } finally {
        if (current) setLoading(false);
      }
    })();
    return () => {
      current = false;
    };
  }, []);

  const cube = cubes.find((c) => c.name === name) ?? null;

  // A cube change invalidates every selection: measures and dimensions are named
  // per cube, so carrying them across would send names the new cube refuses.
  React.useEffect(() => {
    if (!cube) return;
    setMeasures(cube.measures[0] ? [cube.measures[0].name] : []);
    setDimensions([]);
    setTimeDimension('');
    setResult(null);
  }, [cube?.name]);

  function toggle(list: string[], value: string, set: (next: string[]) => void) {
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);
  }

  async function run() {
    if (!cube || measures.length === 0) return;
    setRunning(true);
    try {
      const body = await post<{ columns: string[]; rows: unknown[][] }>(
        `/api/vanna/v2/cubes/${encodeURIComponent(cube.name)}/query`,
        {
          measures,
          dimensions,
          time_dimension: timeDimension || undefined,
          granularity: timeDimension ? grain : undefined,
        },
      );
      setResult(body);
      setError(null);
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setRunning(false);
    }
  }

  const figure = React.useMemo(() => {
    if (!result || result.rows.length === 0) return null;
    try {
      return tileFigure(
        { kind: 'chart', title: cube?.name ?? '', chart: { type: timeDimension ? 'line' : 'bar' } },
        result,
        { dark, height: 320 },
      );
    } catch {
      return null;
    }
  }, [result, dark, timeDimension, cube?.name]);

  if (loading) {
    return (
      <PageBody>
        <LoadingCards count={3} />
      </PageBody>
    );
  }

  if (error && cubes.length === 0) {
    return (
      <PageBody>
        <ErrorState title={t('history.reload')} detail={error} />
      </PageBody>
    );
  }

  if (cubes.length === 0) {
    return (
      <PageBody>
        <PageHeader title={t('cubes.title')} description={t('cubes.sub')} />
        <EmptyState
          icon={<BarChart3 className="size-7" />}
          title={t('cubes.emptyBefore')}
          hint={t('cubes.emptyAfter')}
        />
      </PageBody>
    );
  }

  return (
    <PageBody>
      <PageHeader title={t('cubes.title')} description={cube?.description || t('cubes.sub')} />

      <div className="grid gap-3 lg:grid-cols-[300px_minmax(0,1fr)]">
        <Card className="flex flex-col gap-3 p-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="cube-name">{t('cubes.title')}</Label>
            <Select value={name} onValueChange={setName}>
              <SelectTrigger id="cube-name">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {cubes.map((option) => (
                  <SelectItem key={option.name} value={option.name}>
                    {option.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <fieldset className="flex flex-col gap-1.5">
            <legend className="mb-1 text-[0.8125rem] font-medium text-muted-foreground">
              {t('cubes.measures')}
            </legend>
            {(cube?.measures ?? []).map((measure) => (
              <label key={measure.name} className="flex items-center gap-2 text-[0.8125rem]">
                <Switch
                  checked={measures.includes(measure.name)}
                  onCheckedChange={() => toggle(measures, measure.name, setMeasures)}
                />
                <span title={measure.description || measure.expression}>{measure.name}</span>
              </label>
            ))}
          </fieldset>

          {cube?.dimensions?.length ? (
            <fieldset className="flex flex-col gap-1.5">
              <legend className="mb-1 text-[0.8125rem] font-medium text-muted-foreground">
                {t('cubes.breakdown')}
              </legend>
              {cube.dimensions.map((dimension) => (
                <label key={dimension} className="flex items-center gap-2 text-[0.8125rem]">
                  <Switch
                    checked={dimensions.includes(dimension)}
                    onCheckedChange={() => toggle(dimensions, dimension, setDimensions)}
                  />
                  <span>{dimension}</span>
                </label>
              ))}
            </fieldset>
          ) : null}

          {cube?.time_dimensions?.length ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="cube-time">{t('cubes.overTime')}</Label>
              <Select value={timeDimension || 'none'} onValueChange={(v) => setTimeDimension(v === 'none' ? '' : v)}>
                <SelectTrigger id="cube-time">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">—</SelectItem>
                  {cube.time_dimensions.map((dimension) => (
                    <SelectItem key={dimension} value={dimension}>
                      {dimension}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              {timeDimension ? (
                <Select value={grain} onValueChange={setGrain}>
                  <SelectTrigger aria-label={t('cubes.overTime')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {GRAINS.map((option) => (
                      <SelectItem key={option} value={option}>
                        {option}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : null}
            </div>
          ) : null}

          <Button
            variant="primary"
            disabled={running || measures.length === 0}
            onClick={() => void run()}
          >
            <Play />
            {running ? t('cubes.running') : t('common.run')}
          </Button>
          {measures.length === 0 ? (
            <p className="text-[0.75rem] text-muted-foreground">{t('cubes.pickMeasure')}</p>
          ) : null}
        </Card>

        <div className="min-w-0">
          {!result ? (
            <EmptyState title={t('cubes.pickMeasure')} />
          ) : result.rows.length === 0 ? (
            <EmptyState title={t('dash.noData')} />
          ) : (
            <div className="flex flex-col gap-3">
              {figure ? (
                <Card className="p-4">
                  <PlotlyChart figure={figure} theme={dark ? 'dark' : 'light'} label={cube?.name} />
                </Card>
              ) : null}

              <Card className="p-0">
                <ScrollX className="max-h-[45vh] overflow-y-auto">
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
                              {value === null || value === undefined ? '' : String(value)}
                            </Td>
                          ))}
                        </Tr>
                      ))}
                    </Tbody>
                  </DataTable>
                </ScrollX>
              </Card>
            </div>
          )}
        </div>
      </div>
    </PageBody>
  );
}
