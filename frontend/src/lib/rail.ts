/**
 * The sidebar's collapsed/expanded rail width.
 *
 * Modelled on `theme.ts` and reusing the vanilla build's own key and values
 * (`vanna.rail`, `'mini' | 'full'`) -- `frontend/public/assets/app.js` had
 * this rail already; matching it is worth more than reinventing it.
 *
 * Split into two functions on purpose, so the bug in the model this is
 * copied from (`applyTheme` paints but never persists; only the unused
 * `toggleTheme` did) cannot be copied here: `applyRail` only paints,
 * `setRail` persists *then* paints, and the toggle always calls `setRail`.
 */

export type Rail = 'mini' | 'full';

const RAIL_KEY = 'vanna.rail';

function stored(): Rail | null {
  try {
    const value = localStorage.getItem(RAIL_KEY);
    return value === 'mini' || value === 'full' ? value : null;
  } catch {
    return null;
  }
}

export function applyRail(rail?: Rail): Rail {
  const next = rail || stored() || 'full';
  document.documentElement.setAttribute('data-rail', next);
  return next;
}

export function currentRail(): Rail {
  return document.documentElement.getAttribute('data-rail') === 'mini' ? 'mini' : 'full';
}

export function setRail(next: Rail): Rail {
  try {
    localStorage.setItem(RAIL_KEY, next);
  } catch {
    /* preference is not worth failing a render over */
  }
  return applyRail(next);
}
