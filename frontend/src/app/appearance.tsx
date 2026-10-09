/**
 * Theme and accent, shared reactively across the shell.
 *
 * Modelled line-for-line on `LocaleProvider` (`i18n/index.tsx`) -- the house
 * pattern for "a preference painted onto <html> that other components need
 * to react to, not just read once."
 *
 * Two bugs this closes, both verified against the code before this file
 * existed:
 *
 * 1. **The theme choice was never persisted.** `applyTheme` (`lib/theme.ts`)
 *    only paints the `data-theme` attribute; only `toggleTheme` wrote
 *    `localStorage`, and nothing called `toggleTheme` -- `layout.tsx` called
 *    `applyTheme` directly. Dark mode was lost on reload.
 * 2. **Theme changes never reached charts or the chat.** `AskPage`,
 *    `DashboardView` and `MetricsPage` called `currentTheme()` once during
 *    render, with no subscription -- so toggling the theme without
 *    navigating away left every open chart on the old theme. Reading
 *    `theme` from this context instead makes those components subscribers.
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

import { applyTheme, currentTheme, type Theme } from '@/lib/theme';

const THEME_KEY = 'vanna.theme';
const ACCENT_KEY = 'vanna.accent';

export type Accent = 'indigo' | 'blue' | 'teal' | 'violet';
const ACCENTS: Accent[] = ['indigo', 'blue', 'teal', 'violet'];
const DEFAULT_ACCENT: Accent = 'indigo';

function storedAccent(): Accent {
  try {
    const value = localStorage.getItem(ACCENT_KEY);
    return (ACCENTS as string[]).includes(value ?? '') ? (value as Accent) : DEFAULT_ACCENT;
  } catch {
    return DEFAULT_ACCENT;
  }
}

function paintAccent(accent: Accent): void {
  document.documentElement.setAttribute('data-accent', accent);
}

interface AppearanceContextValue {
  theme: Theme;
  setTheme: (next: Theme) => void;
  toggleTheme: () => void;
  accent: Accent;
  setAccent: (next: Accent) => void;
}

const AppearanceContext = createContext<AppearanceContextValue | null>(null);

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(currentTheme);
  const [accent, setAccentState] = useState<Accent>(storedAccent);

  // `main.tsx` already painted `data-theme` from storage before React
  // mounted (avoiding a flash), but `data-accent` has never been set --
  // there was no accent before this file. Paint it once on mount.
  useEffect(() => {
    paintAccent(accent);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once, at mount
  }, []);

  const setTheme = useCallback((next: Theme) => {
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* preference is not worth failing a render over */
    }
    setThemeState(applyTheme(next));
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === 'dark' ? 'light' : 'dark');
  }, [theme, setTheme]);

  const setAccent = useCallback((next: Accent) => {
    try {
      localStorage.setItem(ACCENT_KEY, next);
    } catch {
      /* preference is not worth failing a render over */
    }
    paintAccent(next);
    setAccentState(next);
  }, []);

  const value = useMemo(
    () => ({ theme, setTheme, toggleTheme, accent, setAccent }),
    [theme, setTheme, toggleTheme, accent, setAccent],
  );

  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

export function useAppearance(): AppearanceContextValue {
  const context = useContext(AppearanceContext);
  if (!context) throw new Error('useAppearance must be used inside <AppearanceProvider>');
  return context;
}

export { ACCENTS };
