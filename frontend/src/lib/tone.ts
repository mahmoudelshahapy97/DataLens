/**
 * Canonical tone vocabulary, shared by every component and page that colours
 * something by state or category.
 *
 * Before this, three different unions existed at once -- `Badge`'s
 * `neutral | admin | ok | err | warn | outline`, `StatTile`'s
 * `neutral | good | bad | warn | primary`, and a private third set in
 * `CompliancePage.tsx` -- and consumers picked whichever they remembered.
 * Naming tones after the tokens they resolve to (`good` resolves to
 * `--good`, not to an arbitrarily different word like `ok`) is what stops
 * that divergence recurring. The migration landed in three steps -- add this
 * file with aliases, retype every call site, delete the aliases -- and this
 * is the code after the third: `ok`/`err`/`admin`/`primary` no longer exist
 * anywhere in the tree.
 *
 * `info` is a category, not a severity: several places were overloading
 * `warn` for something that is not a warning (a column *being* masked --
 * the desired state; a schedule being paused; a lineage node's *kind*). A
 * category rendered as an amber warning teaches people to ignore real
 * warnings.
 */
export type Tone = 'neutral' | 'accent' | 'good' | 'bad' | 'warn' | 'info';

/** Ink-only classes, for a value or icon coloured by tone with no chip background. */
export const TONE_TEXT_CLASSES: Record<Tone, string> = {
  neutral: 'text-muted-foreground',
  accent: 'text-primary-ink',
  good: 'text-good',
  bad: 'text-bad',
  warn: 'text-warn',
  // No dedicated --info token: this reuses the categorical chart-5 (cyan),
  // which is already the palette's "this is a kind, not a verdict" colour
  // (Governance's section colour, Part 3d) and is visually distinct from
  // --warn, --good, --bad and --primary at a glance.
  info: 'text-[var(--chart-5)]',
};

/** Tinted chip classes, for Badge-style components. */
export const TONE_BADGE_CLASSES: Record<Tone, string> = {
  neutral: 'bg-muted-foreground/15 text-muted-foreground',
  accent: 'bg-primary-soft text-primary-ink',
  good: 'bg-good/15 text-good',
  bad: 'bg-bad/15 text-bad',
  warn: 'bg-warn/15 text-warn',
  info: 'bg-[color-mix(in_srgb,var(--chart-5)_15%,transparent)] text-[var(--chart-5)]',
};
