/**
 * Relative times.
 *
 * Two functions rather than one that handles both directions. A helper that
 * quietly signs the difference is how "expires just now" shipped on every live
 * session -- the same code path rendered a past instant and a future one, and the
 * wrong branch was only wrong for people whose session had not expired yet.
 */

import type { Translate } from '../i18n';

/** How long ago. Past instants only. */
export function relative(iso: string | null | undefined, t: Translate, locale: string): string {
  if (!iso) return '';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return t('time.justNow');
  if (seconds < 3600) return t('time.minutesAgo', { n: Math.floor(seconds / 60) });
  if (seconds < 86400) return t('time.hoursAgo', { n: Math.floor(seconds / 3600) });
  if (seconds < 604800) return t('time.daysAgo', { n: Math.floor(seconds / 86400) });
  return new Date(iso).toLocaleDateString(locale);
}

/** How long until. Future instants only -- expiries, renewals, next scheduled run. */
export function until(iso: string | null | undefined, t: Translate, locale: string): string {
  if (!iso) return '';
  const seconds = (new Date(iso).getTime() - Date.now()) / 1000;
  if (seconds <= 0) return t('time.expired');
  if (seconds < 3600) return t('time.inMinutes', { n: Math.max(1, Math.floor(seconds / 60)) });
  if (seconds < 86400) return t('time.inHours', { n: Math.floor(seconds / 3600) });
  if (seconds < 604800) return t('time.inDays', { n: Math.floor(seconds / 86400) });
  return new Date(iso).toLocaleDateString(locale);
}
