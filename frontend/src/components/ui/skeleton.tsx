import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * A placeholder with the shape of the thing that is coming.
 *
 * Worth its own component: a fresh install renders its empty state everywhere, so
 * "no data yet" is indistinguishable from a fetch that quietly failed. A skeleton
 * says *loading* unambiguously, which is what lets EmptyState mean what it says.
 */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn('animate-pulse rounded-md bg-muted-foreground/15', className)}
      {...props}
    />
  );
}
