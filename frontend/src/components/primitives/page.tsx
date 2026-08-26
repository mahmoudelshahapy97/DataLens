import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * The heading block every screen opens with.
 *
 * `h2.view-title` + `p.view-sub` from app.css, made a component so the sub-line
 * is not optional-by-omission. Several screens had a title and no explanation,
 * and the Dashboards list is the reason this matters: "two people can open the
 * same dashboard and see different numbers" is the single most surprising
 * property of the product, and it was one line of prose under a heading.
 */
export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('mb-5 flex flex-wrap items-start justify-between gap-3', className)}>
      <div className="min-w-0">
        <h2 className="text-[1.15rem] font-semibold leading-tight tracking-[-0.01em]">{title}</h2>
        {description ? (
          <p className="mt-1 max-w-3xl text-[0.8125rem] text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** The scroll container a screen's content lives in. */
export function PageBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('h-full overflow-auto px-6 py-5', className)} {...props} />;
}

/** `.toolbar` -- a wrapping row of filters and actions above a list. */
export function Toolbar({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mb-4 flex flex-wrap items-center gap-2.5', className)} {...props} />;
}

/**
 * A sidebar group heading. Uppercase and tracked, sized down -- it is furniture,
 * not content, and must not compete with the items under it.
 */
export function SectionLabel({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p
      className={cn(
        'px-2 pb-1 pt-3 text-[0.6875rem] font-semibold uppercase tracking-[0.06em]',
        'text-muted-foreground/80',
        className,
      )}
      {...props}
    />
  );
}
