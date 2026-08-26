import { cva, type VariantProps } from 'class-variance-authority';
import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * `.chip` from app.css. Uppercase and tracked, because these are labels for a
 * *state* (admin, ok, failed) rather than words to read in a sentence.
 */
const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[0.6875rem] font-semibold ' +
    'uppercase tracking-[0.03em] whitespace-nowrap',
  {
    variants: {
      tone: {
        neutral: 'bg-muted-foreground/15 text-muted-foreground',
        admin: 'bg-primary-soft text-primary',
        ok: 'bg-good/15 text-good',
        err: 'bg-bad/15 text-bad',
        warn: 'bg-warn/15 text-warn',
        outline: 'border border-border text-muted-foreground',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />;
}

export { badgeVariants };
