/**
 * Interface strings for the chat component.
 *
 * Bundled rather than fetched. The component is distributed as a single file
 * that a host page drops in, and a runtime fetch would mean it depends on being
 * served from a path it does not control -- so a working chat would silently
 * lose its labels depending on where it was embedded. Two small dictionaries
 * cost less than that failure mode.
 *
 * Only strings a *user reads* are here. Console warnings and thrown errors stay
 * in English: they are read by whoever is debugging, and a translated stack
 * trace is harder to search for, not easier.
 */

import ar from './ar.json';
import en from './en.json';

export type LocaleCode = 'en' | 'ar';

const DICTIONARIES: Record<string, Record<string, string>> = { en, ar };

/** Locales written right-to-left. */
export const RTL_LOCALES = new Set<string>(['ar']);

export function isRtl(locale: string): boolean {
  return RTL_LOCALES.has(locale);
}

/**
 * Look up a string.
 *
 * Falls back to English, then to the key. A missing translation should degrade
 * to a language most readers can act on rather than to a blank label -- an
 * empty button is worse than an untranslated one.
 */
export function translate(locale: string, key: string): string {
  const dictionary = DICTIONARIES[locale] || DICTIONARIES.en;
  return dictionary[key] ?? DICTIONARIES.en[key] ?? key;
}
