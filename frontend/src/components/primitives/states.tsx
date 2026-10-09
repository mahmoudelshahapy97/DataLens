import { AlertTriangle, Inbox, RefreshCw } from 'lucide-react';
import * as React from 'react';

import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';

/**
 * The three things a list can be, told apart.
 *
 * The vanilla app rendered "no data yet" a dozen different ways and rendered
 * *nothing* for a failed fetch, so an empty list and a broken endpoint looked
 * identical -- which is why `make seed` exists in the first place: a screenshot
 * run of a fresh install photographed a dozen variations on emptiness and nobody
 * could tell which of them were bugs.
 *
 * Three components, three unambiguous meanings: loading, empty, failed.
 */

export function EmptyState({
  title,
  hint,
  icon,
  action,
  className,
}: {
  title: string;
  hint?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-col items-center gap-2 px-5 py-12 text-center', className)}>
      <div className="text-muted-foreground/60" aria-hidden>
        {icon ?? <Inbox className="size-7" />}
      </div>
      <p className="text-[0.9rem] font-medium">{title}</p>
      {hint ? <p className="max-w-md text-[0.8125rem] text-muted-foreground">{hint}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  title,
  detail,
  onRetry,
  retryLabel = 'Try again',
  className,
}: {
  title: string;
  detail?: string;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}) {
  return (
    // role=alert: a failure that appears after the page has settled is not
    // announced otherwise, and the user is left looking at a stale list.
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center gap-2 rounded-md border border-bad/30 bg-bad/5 px-5 py-10 text-center',
        className,
      )}
    >
      <AlertTriangle className="size-6 text-bad" aria-hidden />
      <p className="text-[0.9rem] font-medium">{title}</p>
      {detail ? (
        <p className="max-w-lg text-[0.8125rem] text-muted-foreground" dir="auto">
          {detail}
        </p>
      ) : null}
      {onRetry ? (
        <Button className="mt-2" onClick={onRetry}>
          <RefreshCw />
          {retryLabel}
        </Button>
      ) : null}
    </div>
  );
}

/** Rows of the shape a table is about to have. */
export function LoadingRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2 p-1', className)} aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
    </div>
  );
}

/** Cards of the shape a gallery is about to have. */
export function LoadingCards({ count = 6, className }: { count?: number; className?: string }) {
  return (
    <div className={cn('grid gap-3 sm:grid-cols-2 lg:grid-cols-3', className)} aria-busy="true">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className="h-28 w-full" />
      ))}
    </div>
  );
}
