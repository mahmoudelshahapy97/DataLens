import * as React from 'react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useT } from '@/i18n';

/**
 * Confirm and prompt, as components rather than as promises.
 *
 * The vanilla `confirmSheet()` / `promptSheet()` in shared/dialogs.js returned a
 * promise, which reads beautifully at the call site and does not survive React:
 * the dialog has to be part of the tree to be part of the focus order, and a
 * promise-returning helper renders it outside. `useConfirm` keeps the ergonomics
 * -- one hook, an `ask()` call, an awaited boolean -- while the dialog itself is
 * mounted where it belongs.
 */

interface ConfirmOptions {
  title: string;
  body?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

export function useConfirm() {
  const t = useT();
  const [options, setOptions] = React.useState<ConfirmOptions | null>(null);
  const resolver = React.useRef<((value: boolean) => void) | null>(null);

  const ask = React.useCallback((next: ConfirmOptions) => {
    setOptions(next);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const settle = React.useCallback((value: boolean) => {
    setOptions(null);
    resolver.current?.(value);
    resolver.current = null;
  }, []);

  const dialog = (
    <Dialog
      open={options !== null}
      // Escape, the close button and a click outside all land here, and all three
      // mean "no". Resolving rather than leaving the promise pending matters: an
      // abandoned promise is a caller stuck forever on `await`.
      onOpenChange={(open) => {
        if (!open) settle(false);
      }}
    >
      <DialogContent className="max-w-[460px]">
        <DialogHeader>
          <DialogTitle>{options?.title}</DialogTitle>
          {options?.body ? <DialogDescription asChild><div>{options.body}</div></DialogDescription> : null}
        </DialogHeader>
        <DialogFooter>
          <Button onClick={() => settle(false)}>
            {options?.cancelLabel ?? t('common.cancel')}
          </Button>
          <Button
            variant={options?.danger ? 'danger' : 'primary'}
            onClick={() => settle(true)}
            autoFocus
          >
            {options?.confirmLabel ?? t('common.confirm')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );

  return { ask, dialog };
}

interface PromptOptions {
  title: string;
  label: string;
  body?: React.ReactNode;
  initial?: string;
  placeholder?: string;
  confirmLabel?: string;
}

export function usePrompt() {
  const t = useT();
  const [options, setOptions] = React.useState<PromptOptions | null>(null);
  const [value, setValue] = React.useState('');
  const resolver = React.useRef<((value: string | null) => void) | null>(null);

  const ask = React.useCallback((next: PromptOptions) => {
    setOptions(next);
    setValue(next.initial ?? '');
    return new Promise<string | null>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const settle = React.useCallback((result: string | null) => {
    setOptions(null);
    resolver.current?.(result);
    resolver.current = null;
  }, []);

  const dialog = (
    <Dialog
      open={options !== null}
      onOpenChange={(open) => {
        if (!open) settle(null);
      }}
    >
      <DialogContent className="max-w-[460px]">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            settle(value);
          }}
        >
          <DialogHeader>
            <DialogTitle>{options?.title}</DialogTitle>
            {options?.body ? (
              <DialogDescription asChild>
                <div>{options.body}</div>
              </DialogDescription>
            ) : null}
          </DialogHeader>

          <div className="mt-3 flex flex-col gap-1.5">
            <Label htmlFor="prompt-value">{options?.label}</Label>
            <Input
              id="prompt-value"
              value={value}
              placeholder={options?.placeholder}
              onChange={(event) => setValue(event.target.value)}
              autoFocus
            />
          </div>

          <DialogFooter>
            <Button onClick={() => settle(null)}>{t('common.cancel')}</Button>
            <Button type="submit" variant="primary">
              {options?.confirmLabel ?? t('common.save')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );

  return { ask, dialog };
}
