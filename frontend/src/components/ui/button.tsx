import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import * as React from 'react';

import { cn } from '@/lib/utils';

/**
 * The variants carry over from `button.btn` in the old app.css, including the
 * ones that looked redundant and are not:
 *
 *   `outline`  the default there -- a bordered button on a surface, which is what
 *              most actions in this product are. Tailwind's instinct is a filled
 *              button; filling every action made the page read as a control panel.
 *   `danger`   red *border and text*, never a red fill. A filled red button next
 *              to a neutral one is the shape people click by accident.
 */
const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-[0.8125rem] ' +
    'font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 ' +
    "[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: 'bg-primary text-primary-foreground border border-primary hover:opacity-90',
        outline: 'border border-border bg-surface hover:border-primary',
        danger: 'border border-bad text-bad bg-surface hover:bg-bad/10',
        ghost: 'hover:bg-rail-hover text-muted-foreground hover:text-foreground',
        link: 'text-primary underline-offset-4 hover:underline',
      },
      size: {
        sm: 'h-8 px-2.5',
        md: 'h-9 px-3.5 py-1.5',
        lg: 'h-10 px-5',
        icon: 'size-8 p-0',
      },
    },
    defaultVariants: { variant: 'outline', size: 'md' },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, type, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button';
    return (
      <Comp
        ref={ref}
        // A <button> inside a <form> defaults to type=submit. Every toolbar button
        // in the old app carried an explicit type="button" for exactly this
        // reason; defaulting it here means nobody has to remember.
        type={asChild ? undefined : (type ?? 'button')}
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      />
    );
  },
);
Button.displayName = 'Button';

export { buttonVariants };
