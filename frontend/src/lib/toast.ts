/**
 * Transient messages.
 *
 * `toast()` in the vanilla app wrote into a single fixed div and announced through
 * the polite live region. Sonner does the presentation; the announcement is kept
 * explicit here rather than trusted to the library, because the two priorities
 * matter: a failure interrupts, a confirmation waits for a pause in speech.
 */

import { toast as sonner } from 'sonner';

import { announce } from './a11y';

export function toast(message: string): void {
  sonner(message);
  announce(message, 'polite');
}

export function toastError(message: string): void {
  sonner.error(message);
  announce(message, 'assertive');
}

export function toastSuccess(message: string): void {
  sonner.success(message);
  announce(message, 'polite');
}
