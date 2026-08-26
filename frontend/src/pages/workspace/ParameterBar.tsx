import * as React from 'react';

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
import { useLocale } from '@/i18n';
import type { Parameter } from '@/types';

/**
 * The controls a reader may change on a report.
 *
 * These exist in the document model, in the parameter type system, in
 * `verify_dashboard`, and in the `/data` and `/export` endpoints -- and the
 * vanilla page ignored them entirely, so all fifty seeded reports rendered at
 * their defaults with no way to say otherwise. This is the missing half.
 *
 * **There is deliberately no free-text control**, because there is no free-text
 * parameter. A value is rendered into SQL by a function that only knows how to
 * emit its declared type: a date becomes `DATE '2026-08-01'` and nothing else
 * can come out of it. An enum's `options` are an allowlist checked server-side,
 * not a hint -- so the select here is a convenience, and refusing an
 * out-of-range value is not left to it.
 */

/** Relative windows a `date_range` accepts by name, from `params.py`. */
const WINDOWS = [
  'last_7_days',
  'last_14_days',
  'last_30_days',
  'last_90_days',
  'last_180_days',
  'last_365_days',
  'month_to_date',
  'quarter_to_date',
  'year_to_date',
] as const;

export type ParameterValues = Record<string, string>;

/** The defaults a document declares, as the form's starting state. */
export function defaultsFor(parameters: Parameter[]): ParameterValues {
  const values: ParameterValues = {};
  for (const parameter of parameters) {
    if (parameter.default === null || parameter.default === undefined) continue;
    values[parameter.name] = String(parameter.default);
  }
  return values;
}

function windowLabel(value: string): string {
  return value
    .replace(/_/g, ' ')
    .replace(/^last (\d+) days$/, 'Last $1 days')
    .replace(/^(\w)/, (c) => c.toUpperCase());
}

export function ParameterBar({
  parameters,
  values,
  onChange,
  onApply,
  running,
}: {
  parameters: Parameter[];
  values: ParameterValues;
  onChange: (next: ParameterValues) => void;
  onApply: () => void;
  running: boolean;
}) {
  const { t } = useLocale();

  if (parameters.length === 0) return null;

  const set = (name: string, value: string) => onChange({ ...values, [name]: value });

  return (
    <form
      className="mb-4 flex flex-wrap items-end gap-3 rounded-md border border-border bg-surface-3 p-3"
      data-print="hide"
      onSubmit={(event) => {
        event.preventDefault();
        onApply();
      }}
    >
      {parameters.map((parameter) => {
        const id = `param-${parameter.name}`;
        const label = parameter.label || parameter.name;
        const value = values[parameter.name] ?? '';

        return (
          <div key={parameter.name} className="flex min-w-[160px] flex-col gap-1.5">
            <Label htmlFor={id}>{label}</Label>

            {parameter.type === 'enum' ? (
              <Select value={value} onValueChange={(next) => set(parameter.name, next)}>
                <SelectTrigger id={id}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {parameter.options.map((option) => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : parameter.type === 'date' ? (
              <Input
                id={id}
                type="date"
                value={value}
                onChange={(event) => set(parameter.name, event.target.value)}
              />
            ) : parameter.type === 'integer' ? (
              <Input
                id={id}
                type="number"
                // The declared bounds, so the browser refuses out-of-range before
                // a round trip. The server checks them again -- this is a
                // convenience, not the control.
                min={parameter.minimum ?? undefined}
                max={parameter.maximum ?? undefined}
                value={value}
                onChange={(event) => set(parameter.name, event.target.value)}
              />
            ) : (
              // date_range. Either a named window or an explicit start and end;
              // the two are offered together because the named ones mean nothing
              // for a warehouse whose data stops in 2008, and several of the
              // seeded workspaces are exactly that.
              <DateRangeControl
                id={id}
                value={value}
                onChange={(next) => set(parameter.name, next)}
              />
            )}
          </div>
        );
      })}

      <Button type="submit" variant="primary" disabled={running}>
        {running ? t('cubes.running') : t('dash.apply')}
      </Button>
    </form>
  );
}

function DateRangeControl({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
}) {
  const { t } = useLocale();
  const isNamed = (WINDOWS as readonly string[]).includes(value);
  const [mode, setMode] = React.useState<'named' | 'explicit'>(isNamed || !value ? 'named' : 'explicit');

  // `2026-01-01..2026-06-30` is how an explicit range travels as one value.
  const [start, end] = value.includes('..') ? value.split('..') : ['', ''];

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        value={mode}
        onValueChange={(next) => {
          setMode(next as 'named' | 'explicit');
          onChange(next === 'named' ? 'last_30_days' : '..');
        }}
      >
        <SelectTrigger id={id} className="w-28">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="named">{t('dash.rangeNamed')}</SelectItem>
          <SelectItem value="explicit">{t('dash.rangeExplicit')}</SelectItem>
        </SelectContent>
      </Select>

      {mode === 'named' ? (
        <Select value={isNamed ? value : 'last_30_days'} onValueChange={onChange}>
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {WINDOWS.map((window) => (
              <SelectItem key={window} value={window}>
                {windowLabel(window)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : (
        <>
          <Input
            type="date"
            className="w-40"
            aria-label={t('history.from')}
            value={start}
            onChange={(event) => onChange(`${event.target.value}..${end}`)}
          />
          <Input
            type="date"
            className="w-40"
            aria-label={t('history.to')}
            value={end}
            onChange={(event) => onChange(`${start}..${event.target.value}`)}
          />
        </>
      )}
    </div>
  );
}
