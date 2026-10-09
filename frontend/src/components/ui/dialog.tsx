import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * The `.overlay` / `.sheet` pair from app.css, on Radix.
 *
 * Radix is what let `trapFocus()` be deleted rather than ported: it implements
 * the focus trap, `aria-modal`, the Escape handler and the focus restore, and it
 * marks the page behind as inert for assistive technology. The hand-rolled trap
 * did the first three and not the fourth.
 */

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;
export const DialogPortal = DialogPrimitive.Portal;

export const DialogOverlay = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'fixed inset-0 z-40 bg-[rgb(15_23_42/0.55)]',
      'data-[state=open]:animate-in data-[state=closed]:animate-out',
      'data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0',
      className,
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

interface DialogContentProps
  extends React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> {
  /** `.sheet.wide` -- the permissions matrix and the tile editor need the room. */
  wide?: boolean;
}

export const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  DialogContentProps
>(({ className, children, wide, ...props }, ref) => (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        'fixed left-1/2 top-1/2 z-50 w-full -translate-x-1/2 -translate-y-1/2',
        'max-h-[86vh] overflow-auto rounded-lg border border-border bg-surface p-6 shadow-sheet',
        'focus:outline-none',
        wide ? 'max-w-[1280px]' : 'max-w-[760px]',
        'data-[state=open]:animate-in data-[state=closed]:animate-out',
        className,
      )}
      {...props}
    >
      {children}
      <DialogPrimitive.Close
        className={cn(
          'absolute end-4 top-4 rounded-md p-1 text-muted-foreground',
          'transition-colors hover:bg-rail-hover hover:text-foreground',
        )}
      >
        <X className="size-4" />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    </DialogPrimitive.Content>
  </DialogPortal>
));
DialogContent.displayName = DialogPrimitive.Content.displayName;

export function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mb-4 flex flex-col gap-1 text-start', className)} {...props} />;
}

/**
 * The action row. `justify-end` in a logical direction, so in Arabic the
 * confirm button sits where the eye ends up rather than where it started.
 */
export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mt-5 flex justify-end gap-2', className)} {...props} />;
}

export const DialogTitle = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn('text-[1.05rem] font-semibold leading-tight', className)}
    {...props}
  />
));
DialogTitle.displayName = DialogPrimitive.Title.displayName;

export const DialogDescription = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn('text-[0.8125rem] text-muted-foreground', className)}
    {...props}
  />
));
DialogDescription.displayName = DialogPrimitive.Description.displayName;
