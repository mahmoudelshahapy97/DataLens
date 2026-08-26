import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * `table.data` + `.scroll-x` from app.css.
 *
 * Two details are load-bearing and easy to lose in a rewrite:
 *
 *   `text-align: start`  not `left`. In Arabic the whole grid mirrors, and a
 *                        hard `left` leaves every column hugging the wrong edge.
 *   `.data-table`        the class the RTL block in tailwind.css keys on to force
 *                        the *grid* back to ltr while letting each cell lay out
 *                        per its own content (`unicode-bidi: plaintext`), so a
 *                        row of Arabic labels and Latin ids reads correctly.
 *
 * The horizontal scroll is on a wrapper rather than the table: a result set is
 * arbitrarily wide, and a table that widens its own page pushes the navigation
 * off-screen.
 */

export function ScrollX({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('w-full overflow-x-auto', className)} {...props} />;
}

export function DataTable({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <table
      className={cn('data-table w-full border-collapse text-[0.8125rem]', className)}
      {...props}
    />
  );
}

export function Th({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        'border-b border-border px-2.5 py-2 text-start align-top',
        'font-semibold whitespace-nowrap text-muted-foreground',
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td
      className={cn('border-b border-border-soft px-2.5 py-2 text-start align-top', className)}
      {...props}
    />
  );
}

/** A header that stays put while a long result scrolls under it. */
export function StickyThead({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn('sticky top-0 z-10 bg-surface', className)} {...props} />;
}

export function Tbody({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn('[&_tr:last-child_td]:border-b-0', className)} {...props} />;
}

export function Tr({ className, ...props }: React.HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn('hover:bg-surface-2/60', className)} {...props} />;
}

/**
 * A cell holding an identifier, a value from a warehouse, or SQL.
 *
 * `dir=auto` rather than a fixed direction: the content decides. A customer name
 * in Arabic should read right-to-left inside a grid that is otherwise ltr.
 */
export function ValueCell({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <Td dir="auto" className={cn('font-mono text-[0.78rem]', className)} {...props} />;
}
