import * as React from 'react';

import { cn } from '@/lib/utils';
import { TONE_BADGE_CLASSES, type Tone } from '@/lib/tone';

/**
 * `.chip` from app.css. Uppercase and tracked, because these are labels for a
 * *state* (admin, ok, failed) rather than words to read in a sentence.
 *
 * `outline` is not a tone -- it carries no colour of its own, so it stays
 * outside `Tone` and is handled separately below.
 */
const BASE_CLASSES =
  'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[0.6875rem] font-semibold ' +
  'uppercase tracking-[0.03em] whitespace-nowrap';
const OUTLINE_CLASSES = 'border border-border text-muted-foreground';

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone | 'outline';
}

export function Badge({ className, tone = 'neutral', ...props }: BadgeProps) {
  const toneClasses = tone === 'outline' ? OUTLINE_CLASSES : TONE_BADGE_CLASSES[tone];
  return <span className={cn(BASE_CLASSES, toneClasses, className)} {...props} />;
}
