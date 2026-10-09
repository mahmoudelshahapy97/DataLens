import * as React from 'react';

import { cn } from '@/lib/utils';

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<'textarea'>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        'flex min-h-[110px] w-full resize-y rounded-lg border border-input bg-surface-2 px-3 py-2',
        'text-[0.8125rem] placeholder:text-muted-foreground disabled:opacity-50',
        'focus-visible:border-primary',
        className,
      )}
      {...props}
    />
  ),
);
Textarea.displayName = 'Textarea';

/**
 * A textarea that holds SQL.
 *
 * Monospace and `dir=ltr` unconditionally: a mirrored SELECT is not Arabic, it is
 * unreadable. The stylesheet opts `.sql` out of RTL for the same reason; this is
 * the editable half of that pair.
 */
export const SqlEditor = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<'textarea'>>(
  ({ className, ...props }, ref) => (
    <Textarea
      ref={ref}
      dir="ltr"
      spellCheck={false}
      className={cn('sql font-mono', className)}
      {...props}
    />
  ),
);
SqlEditor.displayName = 'SqlEditor';
