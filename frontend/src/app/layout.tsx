import { Languages, LogOut, Moon, Sun } from 'lucide-react';
import * as React from 'react';
import { Navigate, NavLink, Outlet, useLocation } from 'react-router-dom';

import { SectionLabel } from '@/components/primitives/page';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { LOCALES, useLocale } from '@/i18n';
import { applyTheme, currentTheme, type Theme } from '@/lib/theme';
import { toastError } from '@/lib/toast';
import { cn } from '@/lib/utils';
import { ConsoleScopeProvider } from '@/pages/console/scope';

import { navFor } from './nav';
import { useSession } from './session';

/**
 * The shell every screen renders inside.
 *
 * The vanilla build had two of these -- an app page and a console page, each with
 * its own header, its own theme button and its own copy of the locale picker --
 * and they had already drifted: the console's picker wrote the same localStorage
 * key with a different set of options. One shell, one set of controls.
 *
 * Nothing here is an access control. `navFor` hides what the caller cannot use so
 * the sidebar matches reality, but every route behind these items answers 404 on
 * its own; `backend/vanna_app/authz.py` is where that is enforced and the only
 * place it is enforced.
 */

export function AppLayout() {
  const { t } = useLocale();
  const { me, loading } = useSession();
  const location = useLocation();

  if (loading) {
    return (
      <div className="grid h-dvh place-items-center text-[0.875rem] text-muted-foreground">
        {t('common.loading')}
      </div>
    );
  }

  // This used to render a button pointing at `/`, on the theory that the vanilla
  // page still owned sign-in. It does not: nginx serves `/` from this bundle, so
  // the button returned the user to this same screen. Send them to the real form,
  // remembering where they were headed so signing in resumes it.
  if (!me) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }

  return (
    <ConsoleScopeProvider>
      {/* `overflow-hidden` and `min-h-0` are what make the two columns scroll
          independently, and neither is optional.

          A grid item defaults to `min-height: auto` -- it will not shrink below
          its content. So the row grew to whatever the *taller* column needed
          (10,116px on a long history page), the nav stretched to match, its
          `overflow-y: auto` never engaged because there was nothing to overflow,
          and the document scrolled instead. Scrolling the content therefore
          dragged the whole navigation off the top of the screen.

          `overflow-hidden` on the container stops the grid growing past the
          viewport; `min-h-0` on each column lets it shrink to that height so its
          own overflow can take over. */}
      <div className="grid h-dvh grid-cols-[248px_minmax(0,1fr)] overflow-hidden max-lg:grid-cols-[minmax(0,1fr)]">
        <Sidebar />
        <div className="flex min-h-0 min-w-0 flex-col">
          <TopBar />
          <main id="content" className="min-h-0 flex-1 overflow-hidden">
            <Outlet />
          </main>
        </div>
      </div>
    </ConsoleScopeProvider>
  );
}

function Sidebar() {
  const { t } = useLocale();
  const { me } = useSession();
  if (!me) return null;

  return (
    <nav
      aria-label={t('a11y.sections')}
      className="flex min-h-0 flex-col border-e border-border bg-surface max-lg:hidden"
    >
      {/* Pinned. Which workspace you are looking at is the one thing that must
          not scroll away -- every number on every screen is scoped to it. */}
      <div data-pin="brand" className="shrink-0 px-4 pb-2 pt-3">
        <p className="text-[0.9rem] font-semibold leading-tight">DataLens</p>
        <p className="truncate text-[0.75rem] text-muted-foreground" dir="auto">
          {me.tenant?.name ?? me.tenant?.id}
        </p>
      </div>

      {/* The list, and only the list, scrolls. `min-h-0` again for the same
          reason as above: a flex child will not shrink below its content
          without it, so the scrollbar would never appear.

          `overscroll-contain` stops a scroll that reaches the end of this list
          from continuing into the page behind it -- which, with the content
          column scrolling separately, reads as the wrong panel moving. */}
      <div
        data-scroll="nav"
        className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto overscroll-contain px-2 pb-3"
      >
      {navFor(me).map((group) => (
        <React.Fragment key={group.labelKey ?? 'first'}>
          {group.labelKey ? <SectionLabel>{t(group.labelKey)}</SectionLabel> : null}
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[0.8125rem]',
                  'hover:bg-rail-hover',
                  isActive ? 'bg-primary-soft font-medium text-primary' : 'text-foreground',
                )
              }
            >
              <item.icon className="size-4 shrink-0" aria-hidden />
              <span className="truncate">{t(item.labelKey)}</span>
            </NavLink>
          ))}
        </React.Fragment>
      ))}
      </div>
    </nav>
  );
}

function TopBar() {
  const { t, locale, setLocale } = useLocale();
  const { me, isPlatformAdmin, signOut } = useSession();
  const [theme, setTheme] = React.useState<Theme>(currentTheme);
  const [leaving, setLeaving] = React.useState(false);

  /**
   * Sign out.
   *
   * There was no button for this anywhere in the shell -- the label
   * (`nav.signOut`) has been in the dictionary all along, and the only way to
   * end a session was to find the current one in the Account screen's session
   * list and revoke it. `signOut` clears local state in a `finally` and rethrows
   * a genuine server failure, so a session that may still be live on the server
   * says so rather than looking closed.
   */
  async function leave() {
    setLeaving(true);
    try {
      await signOut();
    } catch (caught) {
      toastError((caught as Error).message);
    } finally {
      setLeaving(false);
    }
  }

  return (
    <header className="flex shrink-0 items-center gap-2 border-b border-border px-6 py-2.5">
      <span className="truncate text-[0.8125rem] text-muted-foreground" dir="auto">
        {me?.user.email}
      </span>
      {isPlatformAdmin ? <Badge tone="admin">{t('shell.platformAdmin')}</Badge> : null}

      <span className="flex-1" />

      <label className="sr-only" htmlFor="locale">
        {t('console.language')}
      </label>
      <div className="flex items-center gap-1.5">
        <Languages className="size-4 text-muted-foreground" aria-hidden />
        <select
          id="locale"
          value={locale}
          onChange={(event) => setLocale(event.currentTarget.value)}
          className="h-8 rounded-md border border-border bg-surface px-2 text-[0.8125rem]"
        >
          {Object.entries(LOCALES).map(([code, name]) => (
            <option key={code} value={code}>
              {name}
            </option>
          ))}
        </select>
      </div>

      <Button
        variant="ghost"
        size="icon"
        aria-label={t('console.theme')}
        onClick={() => setTheme(applyTheme(theme === 'dark' ? 'light' : 'dark'))}
      >
        {theme === 'dark' ? <Sun /> : <Moon />}
      </Button>

      <Button variant="ghost" disabled={leaving} onClick={() => void leave()}>
        <LogOut />
        {t('nav.signOut')}
      </Button>
    </header>
  );
}
