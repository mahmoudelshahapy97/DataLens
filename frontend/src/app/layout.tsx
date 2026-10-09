import {
  ChevronLeft,
  ChevronRight,
  Languages,
  LayoutDashboard,
  LogOut,
  Moon,
  ShieldCheck,
  Sun,
  Building2,
} from 'lucide-react';
import * as React from 'react';
import { Navigate, NavLink, Outlet, useLocation } from 'react-router-dom';

import { SectionLabel } from '@/components/primitives/page';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { LOCALES, useLocale } from '@/i18n';
import { announce } from '@/lib/a11y';
import { currentRail, setRail, type Rail } from '@/lib/rail';
import { toastError } from '@/lib/toast';
import { cn } from '@/lib/utils';
import { ConsoleScopeProvider } from '@/pages/console/scope';
import type { LucideIcon } from 'lucide-react';

import { useAppearance } from './appearance';
import { groupForPath, navFor, type NavGroup } from './nav';
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

/** A representative icon per group, for the collapsed rail's flyout trigger --
 * a 60px column cannot show "GOVERNANCE", so a closed group collapses to one
 * icon rather than to nothing. */
const GROUP_ICON: Record<string, LucideIcon> = {
  workspace: LayoutDashboard,
  governance: ShieldCheck,
  admin: Building2,
};

const CLOSED_KEY = 'vanna.nav.closed';

function storedClosed(): Set<string> {
  try {
    const raw = localStorage.getItem(CLOSED_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string') : []);
  } catch {
    return new Set();
  }
}

function persistClosed(closed: Set<string>): void {
  try {
    localStorage.setItem(CLOSED_KEY, JSON.stringify([...closed]));
  } catch {
    /* preference is not worth failing a render over */
  }
}

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
          own overflow can take over. The track *count* is unchanged here and
          `minmax(0,1fr)` still absorbs the remainder -- only the first track's
          width now reads from `--sidebar` (toggled by `data-rail`) instead of a
          literal, and transitions when it changes. */}
      <div
        className={cn(
          'grid h-dvh grid-cols-[var(--sidebar)_minmax(0,1fr)] overflow-hidden',
          'transition-[grid-template-columns] duration-200 ease-out motion-reduce:transition-none',
          'max-lg:grid-cols-[minmax(0,1fr)]',
        )}
      >
        <Sidebar />
        <div className="flex min-h-0 min-w-0 flex-col">
          <TopBar />
          <main
            id="content"
            data-section={groupForPath(location.pathname)}
            className="min-h-0 flex-1 overflow-hidden"
          >
            <Outlet />
          </main>
        </div>
      </div>
    </ConsoleScopeProvider>
  );
}

function Sidebar() {
  const { t, dir } = useLocale();
  const { me } = useSession();
  const location = useLocation();

  const [rail, setRailState] = React.useState<Rail>(currentRail);
  const [closed, setClosed] = React.useState<Set<string>>(storedClosed);
  const [forced, setForced] = React.useState<string | null>(null);
  const [openFlyout, setOpenFlyout] = React.useState<string | null>(null);

  const activeGroup = groupForPath(location.pathname);

  // Live value for the effect below, so it can depend on `activeGroup` alone
  // (see the comment there) without reading a stale `closed`.
  const closedRef = React.useRef(closed);
  closedRef.current = closed;

  // A forced-open override, not a state sync: re-set only when the *group*
  // changes, not the pathname, so moving between two Governance pages does
  // not re-fire this. `/dashboards/:id` has no NAV entry of its own and
  // resolves to Workspace via groupForPath's descendant clause -- the same
  // rule NavLink uses for `isActive`, so highlight and auto-open agree by
  // construction.
  React.useEffect(() => {
    setForced(activeGroup && closedRef.current.has(activeGroup) ? activeGroup : null);
  }, [activeGroup]);

  const isOpen = React.useCallback(
    (id: string) => !closed.has(id) || forced === id,
    [closed, forced],
  );

  // Written only here, in the click handler -- never in an effect, which is
  // what stops navigation-driven auto-open from silently overwriting a
  // person's stored preference.
  function toggleGroup(id: string) {
    setClosed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
        // A toggle that visibly refuses to close reads as broken: closing the
        // group you are standing in must stick, not spring back open on the
        // next render via the forced override.
        if (forced === id) setForced(null);
      }
      persistClosed(next);
      return next;
    });
  }

  function toggleRail() {
    const next: Rail = rail === 'mini' ? 'full' : 'mini';
    setRailState(setRail(next));
    announce(next === 'mini' ? t('shell.railCollapsed') : t('shell.railExpanded'), 'polite');
  }

  if (!me) return null;

  const groups = navFor(me);
  const collapsed = rail === 'mini';
  // RTL mirrors the icon glyph rather than transforming it: the sidebar sits
  // on the opposite edge, so "collapse" points at the opposite screen edge too.
  const ToggleIcon = collapsed
    ? dir === 'rtl'
      ? ChevronLeft
      : ChevronRight
    : dir === 'rtl'
      ? ChevronRight
      : ChevronLeft;

  return (
    <nav
      id="sidebar-nav"
      aria-label={t('a11y.sections')}
      className="flex min-h-0 flex-col border-e border-border bg-surface max-lg:hidden"
    >
      {/* Pinned. Which workspace you are looking at is the one thing that must
          not scroll away -- every number on every screen is scoped to it. At
          60px there is no room for the name, so it collapses to a mark with
          the tenant name carried as an accessible name instead. */}
      <div
        data-pin="brand"
        className={cn(
          'flex shrink-0 items-center gap-2 px-4 pb-2 pt-3',
          collapsed && 'justify-center px-0',
        )}
      >
        {collapsed ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="grid size-6 place-items-center rounded-md bg-primary-soft text-[0.7rem] font-bold text-primary-ink"
                aria-label={me.tenant?.name ?? me.tenant?.id}
                dir="auto"
              >
                {(me.tenant?.name ?? me.tenant?.id ?? '?').slice(0, 1).toUpperCase()}
              </span>
            </TooltipTrigger>
            <TooltipContent side={dir === 'rtl' ? 'left' : 'right'}>
              {me.tenant?.name ?? me.tenant?.id}
            </TooltipContent>
          </Tooltip>
        ) : (
          <div className="min-w-0">
            <p className="text-[0.9rem] font-semibold leading-tight">DataLens</p>
            <p className="truncate text-[0.75rem] text-muted-foreground" dir="auto">
              {me.tenant?.name ?? me.tenant?.id}
            </p>
          </div>
        )}

        {!collapsed ? (
          <button
            type="button"
            title={t('shell.railCollapse')}
            aria-label={t('shell.railCollapse')}
            aria-expanded={!collapsed}
            aria-controls="sidebar-nav"
            onClick={toggleRail}
            className="ms-auto grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-rail-hover hover:text-foreground max-lg:hidden"
          >
            <ToggleIcon className="size-4" aria-hidden />
          </button>
        ) : null}
      </div>

      {collapsed ? (
        <button
          type="button"
          title={t('shell.railExpand')}
          aria-label={t('shell.railExpand')}
          aria-expanded={!collapsed}
          aria-controls="sidebar-nav"
          onClick={toggleRail}
          className="mx-auto mb-1 grid size-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-rail-hover hover:text-foreground max-lg:hidden"
        >
          <ToggleIcon className="size-4" aria-hidden />
        </button>
      ) : null}

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
        {groups.map((group) =>
          collapsed ? (
            <CollapsedGroup
              key={group.id}
              group={group}
              open={isOpen(group.id)}
              flyoutOpen={openFlyout === group.id}
              onFlyoutOpenChange={(next) => setOpenFlyout(next ? group.id : null)}
              onNavigate={() => setOpenFlyout(null)}
              dir={dir}
            />
          ) : (
            <ExpandedGroup
              key={group.id}
              group={group}
              open={isOpen(group.id)}
              onToggle={() => toggleGroup(group.id)}
            />
          ),
        )}
      </div>
    </nav>
  );
}

function ExpandedGroup({
  group,
  open,
  onToggle,
}: {
  group: NavGroup;
  open: boolean;
  onToggle: () => void;
}) {
  const { t } = useLocale();
  const panelId = `nav-group-${group.id}`;

  return (
    <div>
      {group.labelKey ? (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={onToggle}
          className="flex w-full items-center justify-between rounded-md hover:bg-rail-hover"
        >
          <SectionLabel data-section={group.id} className="pointer-events-none">
            {t(group.labelKey)}
          </SectionLabel>
          <ChevronRight
            className={cn('me-2 size-3.5 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')}
            aria-hidden
          />
        </button>
      ) : null}
      {/* `hidden`, not unmounted -- `aria-controls` must always point at a real
          element, and NavLink's own active-state calculation should not be
          torn down and rebuilt every time a group is toggled. */}
      <div id={panelId} hidden={!open} className="flex flex-col gap-0.5">
        {group.items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[0.8125rem]',
                'hover:bg-rail-hover',
                isActive ? 'bg-primary-soft font-medium text-primary-ink' : 'text-foreground',
              )
            }
          >
            <item.icon className="size-4 shrink-0" aria-hidden />
            <span className="truncate">{t(item.labelKey)}</span>
          </NavLink>
        ))}
      </div>
    </div>
  );
}

function CollapsedGroup({
  group,
  open,
  flyoutOpen,
  onFlyoutOpenChange,
  onNavigate,
  dir,
}: {
  group: NavGroup;
  open: boolean;
  flyoutOpen: boolean;
  onFlyoutOpenChange: (open: boolean) => void;
  onNavigate: () => void;
  dir: 'ltr' | 'rtl';
}) {
  const { t } = useLocale();
  const GroupIcon = GROUP_ICON[group.id] ?? group.items[0]?.icon;
  const label = group.labelKey ? t(group.labelKey) : '';

  // Open: icons only, each carrying its own name via aria-label + Tooltip.
  // Closed: a single trigger whose one job is the flyout -- group open/closed
  // is editable only in the expanded rail.
  if (open) {
    return (
      <div className="flex flex-col items-center gap-0.5">
        {group.labelKey ? <div className="my-1 h-px w-6 bg-border" role="separator" /> : null}
        {group.items.map((item) => (
          <Tooltip key={item.to}>
            <TooltipTrigger asChild>
              <NavLink
                to={item.to}
                aria-label={t(item.labelKey)}
                className={({ isActive }) =>
                  cn(
                    'grid size-9 shrink-0 place-items-center rounded-md',
                    'hover:bg-rail-hover',
                    isActive ? 'bg-primary-soft text-primary-ink' : 'text-foreground',
                  )
                }
              >
                <item.icon className="size-4" aria-hidden />
              </NavLink>
            </TooltipTrigger>
            <TooltipContent side={dir === 'rtl' ? 'left' : 'right'}>
              {t(item.labelKey)}
            </TooltipContent>
          </Tooltip>
        ))}
      </div>
    );
  }

  return (
    <Popover open={flyoutOpen} onOpenChange={onFlyoutOpenChange}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={label}
              aria-expanded={flyoutOpen}
              className="mx-auto grid size-9 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-rail-hover hover:text-foreground"
            >
              {GroupIcon ? <GroupIcon className="size-4" aria-hidden /> : null}
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent side={dir === 'rtl' ? 'left' : 'right'}>{label}</TooltipContent>
      </Tooltip>
      {/* Portals past the nav's own `overflow-y-auto` and its grandparent's
          `overflow-hidden` -- an absolutely-positioned panel here would be
          clipped twice. A link inside is not an outside click, so `onNavigate`
          closes it explicitly rather than relying on Radix's dismiss logic. */}
      <PopoverContent side={dir === 'rtl' ? 'left' : 'right'} align="start" className="w-56 p-1.5">
        <p className="px-2 py-1 text-[0.6875rem] font-semibold uppercase tracking-[0.06em] text-muted-foreground/80">
          {label}
        </p>
        {group.items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[0.8125rem]',
                'hover:bg-rail-hover',
                isActive ? 'bg-primary-soft font-medium text-primary-ink' : 'text-foreground',
              )
            }
          >
            <item.icon className="size-4 shrink-0" aria-hidden />
            <span className="truncate">{t(item.labelKey)}</span>
          </NavLink>
        ))}
      </PopoverContent>
    </Popover>
  );
}

function TopBar() {
  const { t, locale, setLocale } = useLocale();
  const { me, isPlatformAdmin, signOut } = useSession();
  const { theme, toggleTheme } = useAppearance();
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
      {isPlatformAdmin ? <Badge tone="accent">{t('shell.platformAdmin')}</Badge> : null}

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
        onClick={toggleTheme}
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
