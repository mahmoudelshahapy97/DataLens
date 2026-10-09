/**
 * Interface language.
 *
 * The contract is the one the vanilla pages had, deliberately: `t(key, values)`
 * where a value named `n` fills the literal `{n}` in the string. Keeping it
 * identical is what let 514 dictionary entries move across untouched -- a
 * different interpolation syntax would have meant editing every one of them, and
 * an Arabic string edited by somebody who does not read Arabic is a string nobody
 * can review.
 *
 * The dictionaries are *imported*, not fetched. See the note at the top of en.ts.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import ar from './ar';
import en from './en';

export const LOCALES: Record<string, string> = { en: 'English', ar: 'العربية' };

/** Locales written right-to-left. Only the interface flips; see the note in tailwind.css. */
const RTL = new Set(['ar']);

const DICTIONARIES: Record<string, Record<string, string>> = { en, ar };

const LOCALE_KEY = 'vanna.locale';

export type Translate = (key: string, values?: Record<string, string | number>) => string;

interface LocaleContextValue {
  locale: string;
  setLocale: (next: string) => void;
  dir: 'ltr' | 'rtl';
  t: Translate;
}

const LocaleContext = createContext<LocaleContextValue | null>(null);

/** The stored preference, or English. Never throws on a locale we dropped. */
function initialLocale(): string {
  try {
    const stored = localStorage.getItem(LOCALE_KEY);
    if (stored && DICTIONARIES[stored]) return stored;
  } catch {
    /* localStorage unavailable (private mode, embedded webview) */
  }
  return 'en';
}

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState(initialLocale);

  const dir: 'ltr' | 'rtl' = RTL.has(locale) ? 'rtl' : 'ltr';

  // The interface mirrors; the *content* does not. SQL, result grids, charts and
  // identifiers read left-to-right in every language -- a mirrored SELECT is not
  // Arabic, it is unreadable. The opt-outs live in tailwind.css keyed on [dir=rtl].
  useEffect(() => {
    document.documentElement.setAttribute('dir', dir);
    document.documentElement.setAttribute('lang', locale);
  }, [dir, locale]);

  const setLocale = useCallback((next: string) => {
    const resolved = DICTIONARIES[next] ? next : 'en';
    try {
      localStorage.setItem(LOCALE_KEY, resolved);
    } catch {
      /* preference is not worth failing a render over */
    }
    setLocaleState(resolved);
  }, []);

  const t = useCallback<Translate>(
    (key, values) => {
      const dict = DICTIONARIES[locale] ?? en;
      let text = dict[key];
      if (text === undefined) {
        // English is the source dictionary, so a miss there is a genuine bug in the
        // caller rather than an untranslated string; warn only for the others.
        if (locale !== 'en') console.warn('[i18n] missing', locale, key);
        text = en[key] ?? key;
      }
      if (values) {
        for (const name of Object.keys(values)) {
          text = text.split(`{${name}}`).join(String(values[name]));
        }
      }
      return text;
    },
    [locale],
  );

  const value = useMemo(() => ({ locale, setLocale, dir, t }), [locale, setLocale, dir, t]);

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale(): LocaleContextValue {
  const context = useContext(LocaleContext);
  if (!context) throw new Error('useLocale must be used inside <LocaleProvider>');
  return context;
}

/** The common case: just the translate function. */
export function useT(): Translate {
  return useLocale().t;
}
