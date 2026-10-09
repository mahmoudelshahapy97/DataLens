/**
 * Light and dark.
 *
 * The attribute and the storage key are the ones the vanilla pages used, so a
 * browser that has been using DataLens keeps its choice across the rewrite:
 * `data-theme` on <html>, `vanna.theme` in localStorage. Tailwind's `dark:`
 * variant is redefined in tailwind.css to key off the same attribute rather than
 * a `.dark` class, so nothing had to change on the storage side to gain the
 * utility classes.
 */

export type Theme = 'light' | 'dark';

const THEME_KEY = 'vanna.theme';

function stored(): Theme | null {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === 'dark' || value === 'light' ? value : null;
  } catch {
    return null;
  }
}

export function applyTheme(theme?: Theme): Theme {
  const next = theme || stored() || 'light';
  document.documentElement.setAttribute('data-theme', next);
  return next;
}

export function currentTheme(): Theme {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

export function toggleTheme(): Theme {
  const next: Theme = currentTheme() === 'dark' ? 'light' : 'dark';
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* preference is not worth failing on */
  }
  return applyTheme(next);
}
