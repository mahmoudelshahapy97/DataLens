import {
  BadgeCheck,
  BarChart3,
  Blocks,
  BookOpen,
  Building2,
  CalendarClock,
  CircleUser,
  Coins,
  ClipboardCheck,
  Clock,
  CreditCard,
  Database,
  FileCog,
  FileText,
  Gauge,
  GitBranch,
  LayoutDashboard,
  MessageSquare,
  Bookmark,
  ScrollText,
  ShieldCheck,
  Sparkles,
  Table2,
  Users,
  EyeOff,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import type { Me } from '@/types';

/**
 * The whole navigation, in one list.
 *
 * The vanilla build had this in two places -- a hand-written tablist in
 * index.html and `tabsFor()` in console.js -- because the workspace and the
 * console were separate applications. They are one now, and the grouping is the
 * point: a reader looking for "who did what" should not have to know whether the
 * previous team filed audit under the workspace or the console.
 *
 * `visible` is *presentation only*. Every route behind these answers 404 for a
 * caller who may not have it, enforced in backend/vanna_app/authz.py. Hiding an
 * item the user cannot use is a courtesy; it is never the control.
 */

export interface NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  visible?: (me: Me) => boolean;
}

export interface NavGroup {
  /** Stable identifier, used for the collapsed-group set, auto-open, and the
   * per-section colour -- never derived from `labelKey`, which is a
   * translation key and must be free to change independent of identity. */
  id: string;
  /** Omitted for the first group, which needs no heading above the first item. */
  labelKey?: string;
  items: NavItem[];
}

const isWorkspaceAdmin = (me: Me) => me.is_admin || me.is_platform_admin;
const isPlatformAdmin = (me: Me) => me.is_platform_admin;

export const NAV: NavGroup[] = [
  {
    id: 'workspace',
    labelKey: 'nav.groupWorkspace',
    items: [
      { to: '/ask', labelKey: 'nav.ask', icon: MessageSquare },
      { to: '/schema', labelKey: 'nav.schema', icon: Table2 },
      { to: '/history', labelKey: 'nav.history', icon: Clock },
      { to: '/saved', labelKey: 'nav.saved', icon: Bookmark },
      { to: '/dashboards', labelKey: 'nav.dashboards', icon: LayoutDashboard },
      { to: '/reports', labelKey: 'nav.reports', icon: CalendarClock },
      // Not `/metrics`: nginx denies that path outright -- it is where the
      // Prometheus endpoint would be exposed -- so the SPA route could never
      // be reached. It 404'd at the edge, before React ever saw it.
      { to: '/cubes', labelKey: 'nav.metrics', icon: BarChart3 },
      // Sessions, API tokens, password, personal LLM key. It was routed but
      // absent from here and linked from nowhere else, so the only way to reach
      // it was to type the URL -- a whole screen nobody could find.
      { to: '/account', labelKey: 'nav.account', icon: CircleUser },
    ],
  },
  {
    id: 'governance',
    labelKey: 'nav.groupGovernance',
    items: [
      // First in the group, and the app's landing route. An operator opening the
      // console is asking "is anything wrong", and the answer used to require
      // reading every other tab.
      { to: '/console/overview', labelKey: 'nav.overview', icon: Gauge, visible: isWorkspaceAdmin },
      { to: '/console/permissions', labelKey: 'tab.permissions', icon: ShieldCheck, visible: isWorkspaceAdmin },
      { to: '/console/masking', labelKey: 'nav.masking', icon: EyeOff, visible: isWorkspaceAdmin },
      { to: '/console/audit', labelKey: 'nav.audit', icon: ScrollText, visible: isWorkspaceAdmin },
      { to: '/console/access-log', labelKey: 'nav.accessLog', icon: FileText, visible: isWorkspaceAdmin },
      { to: '/console/approvals', labelKey: 'nav.approvals', icon: ClipboardCheck, visible: isWorkspaceAdmin },
      { to: '/console/lineage', labelKey: 'nav.lineage', icon: GitBranch, visible: isWorkspaceAdmin },
      { to: '/console/compliance', labelKey: 'nav.compliance', icon: ShieldCheck, visible: isPlatformAdmin },
    ],
  },
  {
    id: 'admin',
    labelKey: 'nav.groupAdmin',
    items: [
      { to: '/console/workspaces', labelKey: 'tab.tenants', icon: Building2, visible: isPlatformAdmin },
      { to: '/console/accounts', labelKey: 'tab.accounts', icon: CircleUser, visible: isPlatformAdmin },
      { to: '/console/members', labelKey: 'tab.members', icon: Users, visible: isWorkspaceAdmin },
      { to: '/console/billing', labelKey: 'tab.billing', icon: CreditCard, visible: isWorkspaceAdmin },
      // Same page as Overview, opened on its cost tab. A different icon and a
      // different name because two entries sharing both is what made them read
      // as duplicates in the first place.
      { to: '/console/usage', labelKey: 'nav.cost', icon: Coins, visible: isWorkspaceAdmin },
      { to: '/console/review', labelKey: 'tab.review', icon: Sparkles, visible: isWorkspaceAdmin },
      { to: '/console/verified', labelKey: 'tab.verified', icon: BadgeCheck, visible: isWorkspaceAdmin },
      { to: '/console/domains', labelKey: 'tab.domains', icon: Blocks, visible: isWorkspaceAdmin },
      { to: '/console/rules', labelKey: 'tab.rules', icon: FileCog, visible: isWorkspaceAdmin },
      { to: '/console/library', labelKey: 'tab.library', icon: BookOpen, visible: isWorkspaceAdmin },
      { to: '/console/starters', labelKey: 'tab.starters', icon: Sparkles, visible: isWorkspaceAdmin },
      { to: '/console/datasources', labelKey: 'nav.datasources', icon: Database, visible: isPlatformAdmin },
      { to: '/console/configuration', labelKey: 'nav.configuration', icon: FileCog, visible: isPlatformAdmin },
    ],
  },
];

/** The groups this person can see, with empty groups dropped. */
export function navFor(me: Me): NavGroup[] {
  return NAV.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.visible || item.visible(me)),
  })).filter((group) => group.items.length > 0);
}

/**
 * Which group a route belongs to, by longest-prefix match against `NAV` --
 * not `navFor(me)`, because which group a path belongs to does not depend on
 * who is asking. Shared by the per-section colour (Part 3d) and the
 * sidebar's auto-open (Part 4): build once, use twice.
 *
 * The descendant clause is load-bearing: `/dashboards/:dashboardId` is a
 * real route with no entry of its own, and must resolve to Workspace -- which
 * is also exactly how `NavLink` decides `isActive`, so highlight and
 * auto-open agree by construction. A prefix test on the *group* itself is
 * not possible: `/console/overview` and `/console/usage` resolve to
 * different groups despite sharing that prefix.
 */
export function groupForPath(pathname: string): string | undefined {
  let best: { to: string; groupId: string } | undefined;
  for (const group of NAV) {
    for (const item of group.items) {
      const matches = pathname === item.to || pathname.startsWith(item.to + '/');
      if (matches && (!best || item.to.length > best.to.length)) {
        best = { to: item.to, groupId: group.id };
      }
    }
  }
  return best?.groupId;
}
