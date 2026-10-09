import * as React from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useLocale } from '@/i18n';
import { api, post } from '@/lib/api';
import { toastError } from '@/lib/toast';
import type { ChartType, Dashboard, SavedQuery, Tile } from '@/types';

/**
 * Put a saved query on a dashboard.
 *
 * The tile stores a `SavedQueryRef`, never a copy of the SQL. That is the whole
 * point of the reference shape: the statement lives in one place, so correcting
 * it corrects every dashboard showing it. Copying the SQL in would produce
 * dashboards that quietly diverge from the query they were made from.
 *
 * A new tile is appended below everything already there rather than dropped at
 * the origin, where it would sit on top of an existing one. Twelve columns wide
 * by default -- the reader can narrow it in the builder, and a full-width chart
 * is right more often than a half-width one.
 */

function nextRow(dashboard: Dashboard): number {
  return dashboard.tiles.reduce(
    (lowest, tile) => Math.max(lowest, (tile.grid?.y ?? 0) + (tile.grid?.height ?? 4)),
    0,
  );
}

const CHART_TYPES: Array<{ value: ChartType | 'table' | 'metric'; labelKey: string }> = [
  { value: 'bar', labelKey: 'tile.bar' },
  { value: 'line', labelKey: 'tile.line' },
  { value: 'area', labelKey: 'tile.area' },
  { value: 'pie', labelKey: 'tile.pie' },
  { value: 'table', labelKey: 'tile.table' },
  { value: 'metric', labelKey: 'tile.metric' },
];

export function PinToDashboard({
  savedQuery,
  onClose,
  onPinned,
}: {
  savedQuery: SavedQuery;
  onClose: () => void;
  onPinned: () => void;
}) {
  const { t } = useLocale();

  const [dashboards, setDashboards] = React.useState<Dashboard[]>([]);
  const [target, setTarget] = React.useState<string>('');
  const [newTitle, setNewTitle] = React.useState('');
  const [tileTitle, setTileTitle] = React.useState(savedQuery.title);
  const [kind, setKind] = React.useState<string>('bar');
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const body = await api<{ dashboards: Array<{ id: string; document: Dashboard }> }>(
          '/api/vanna/v2/dashboards',
        );
        if (!current) return;
        const list = (body.dashboards ?? []).map((row) => row.document);
        setDashboards(list);
        setTarget(list[0]?.id ?? 'new');
      } catch {
        if (current) setTarget('new');
      }
    })();
    return () => {
      current = false;
    };
  }, []);

  async function pin() {
    setSaving(true);
    try {
      const tile: Tile = {
        id: '',
        kind: kind === 'table' || kind === 'metric' ? (kind as Tile['kind']) : 'chart',
        title: tileTitle,
        description: '',
        query: { source: 'saved', saved_query_id: savedQuery.id },
        chart:
          kind === 'table' || kind === 'metric'
            ? undefined
            : { type: kind as ChartType },
        grid: { x: 0, y: 0, width: 12, height: 5 },
      };

      if (target === 'new') {
        await post('/api/vanna/v2/dashboards', {
          title: newTitle || savedQuery.title,
          description: '',
          tiles: [{ ...tile, grid: { x: 0, y: 0, width: 12, height: 5 } }],
          parameters: [],
        });
      } else {
        const existing = dashboards.find((d) => d.id === target);
        if (!existing) throw new Error('That dashboard no longer exists.');
        // The whole document is posted back: `POST /dashboards` creates *or
        // replaces*, and it re-verifies on the way in. Sending only the new tile
        // would need a merge endpoint that does not exist and would have to
        // re-verify anyway.
        await post('/api/vanna/v2/dashboards', {
          ...existing,
          tiles: [
            ...existing.tiles,
            { ...tile, grid: { x: 0, y: nextRow(existing), width: 12, height: 5 } },
          ],
        });
      }
      onPinned();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{t('dash.pin')}</DialogTitle>
          <DialogDescription dir="auto">{savedQuery.title}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pin-target">{t('dash.title')}</Label>
            <Select value={target} onValueChange={setTarget}>
              <SelectTrigger id="pin-target">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {dashboards.map((dashboard) => (
                  <SelectItem key={dashboard.id} value={dashboard.id}>
                    {dashboard.title}
                  </SelectItem>
                ))}
                <SelectItem value="new">{t('dash.newDashboard')}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {target === 'new' ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="pin-new-title">{t('dash.newDashboardName')}</Label>
              <Input
                id="pin-new-title"
                value={newTitle}
                placeholder={savedQuery.title}
                onChange={(event) => setNewTitle(event.target.value)}
              />
            </div>
          ) : null}

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pin-tile-title">{t('tile.title')}</Label>
            <Input
              id="pin-tile-title"
              value={tileTitle}
              onChange={(event) => setTileTitle(event.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="pin-kind">{t('tile.kind')}</Label>
            <Select value={kind} onValueChange={setKind}>
              <SelectTrigger id="pin-kind">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CHART_TYPES.map((option) => (
                  <SelectItem key={option.value} value={String(option.value)}>
                    {t(option.labelKey)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button onClick={onClose}>{t('common.cancel')}</Button>
          <Button variant="primary" disabled={saving} onClick={() => void pin()}>
            {t('dash.pin')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
