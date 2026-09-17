import * as React from 'react';

import { Card } from '@/components/ui/card';
import { TONE_TEXT_CLASSES, type Tone } from '@/lib/tone';
import { cn } from '@/lib/utils';

/**
 * One number, large.
 *
 * The most-read tile type there is, per the comment on `TileKind.METRIC`. It
 * exists here as well as in the chart builder because the governance screens want
 * the same object -- queries this month, spend, denied tool calls -- and the
 * alternative was each of them inventing a card.
 *
 * `tabular-nums` so a value that ticks upward does not shuffle its own digits.
 */
export function StatTile({
  label,
  value,
  hint,
  tone = 'neutral',
  className,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: Tone;
  className?: string;
}) {
  const toneClass = tone === 'neutral' ? 'text-foreground' : TONE_TEXT_CLASSES[tone];

  return (
    <Card className={cn('p-4', className)}>
      <p className="text-[0.8125rem] text-muted-foreground">{label}</p>
      <p className={cn('mt-1 text-[1.75rem] font-semibold leading-none tabular-nums', toneClass)}>
        {value}
      </p>
      {hint ? <p className="mt-1.5 text-[0.75rem] text-muted-foreground">{hint}</p> : null}
    </Card>
  );
}
