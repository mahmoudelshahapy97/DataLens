import * as React from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { PageBody } from '@/components/primitives/page';
import { EmptyState } from '@/components/primitives/states';
import { useLocale } from '@/i18n';

import { AppLayout } from './layout';

/**
 * Every destination the application can reach.
 *
 * Two tiers, and the split is the point. `/login` and `/password` sit *outside*
 * `AppLayout`, because the layout requires a session and these two are how you
 * get one -- nesting them inside it is a redirect loop. Everything else is
 * inside, behind the guard.
 *
 * Lazy, so a console screen's Plotly bundle is not in the entry chunk for
 * somebody who only ever opens the chat.
 */

const lazy = (loader: () => Promise<{ default: React.ComponentType }>) => React.lazy(loader);

// Activity and cost are one page with two tabs; both routes resolve to it so
// existing links keep working. See ActivityPage for why they were merged.
const ActivityOverview = lazy(() =>
  import('@/pages/console/ActivityPage').then((m) => ({ default: m.ActivityOverviewPage })),
);
const ActivityCost = lazy(() =>
  import('@/pages/console/ActivityPage').then((m) => ({ default: m.ActivityCostPage })),
);
const AuditPage = lazy(() => import('@/pages/console/AuditPage'));
const AccessLogPage = lazy(() => import('@/pages/console/AccessLogPage'));
const WorkspacesPage = lazy(() => import('@/pages/console/WorkspacesPage'));
const AccountsPage = lazy(() => import('@/pages/console/AccountsPage'));
const MembersPage = lazy(() => import('@/pages/console/MembersPage'));
const BillingPage = lazy(() => import('@/pages/console/BillingPage'));
const DatabasesPage = lazy(() => import('@/pages/console/DatabasesPage'));
const ReviewPage = lazy(() => import('@/pages/console/ExamplesPage'));
const VerifiedPage = lazy(() => import('@/pages/console/VerifiedPage'));
const RulesPage = lazy(() => import('@/pages/console/RulesPage'));
const LibraryPage = lazy(() => import('@/pages/console/LibraryPage'));
const StartersPage = lazy(() => import('@/pages/console/StartersPage'));
const DomainsPage = lazy(() => import('@/pages/console/DomainsPage'));
const PermissionsPage = lazy(() => import('@/pages/console/PermissionsPage'));
const ConfigurationPage = lazy(() => import('@/pages/console/ConfigurationPage'));
const ApprovalsPage = lazy(() => import('@/pages/console/ApprovalsPage'));
const MaskingPage = lazy(() => import('@/pages/console/MaskingPage'));
const LineagePage = lazy(() => import('@/pages/console/LineagePage'));
const CompliancePage = lazy(() => import('@/pages/console/CompliancePage'));

// The workspace screens -- the half of the product that is not the console.
const AskPage = lazy(() => import('@/pages/workspace/AskPage'));
const SchemaPage = lazy(() => import('@/pages/workspace/SchemaPage'));
const HistoryPage = lazy(() => import('@/pages/workspace/HistoryPage'));
const SavedPage = lazy(() => import('@/pages/workspace/SavedPage'));
const DashboardsPage = lazy(() => import('@/pages/workspace/DashboardsPage'));
const DashboardView = lazy(() => import('@/pages/workspace/DashboardView'));
const ReportsPage = lazy(() => import('@/pages/workspace/ReportsPage'));
const MetricsPage = lazy(() => import('@/pages/workspace/MetricsPage'));
const AccountPage = lazy(() => import('@/pages/workspace/AccountPage'));

// The two public routes. Not lazy: they are the entry point, so their chunk is
// wanted on the very first paint of a signed-out visit.
const LoginPage = lazy(() => import('@/pages/auth/LoginPage'));
const PasswordPage = lazy(() => import('@/pages/auth/PasswordPage'));

/** Every path inside the shell. */
const ROUTED: Record<string, React.LazyExoticComponent<React.ComponentType>> = {
  '/console/overview': ActivityOverview,
  '/console/audit': AuditPage,
  '/console/access-log': AccessLogPage,
  '/console/workspaces': WorkspacesPage,
  '/console/accounts': AccountsPage,
  '/console/members': MembersPage,
  '/console/usage': ActivityCost,
  '/console/billing': BillingPage,
  '/console/datasources': DatabasesPage,
  '/console/review': ReviewPage,
  '/console/verified': VerifiedPage,
  '/console/rules': RulesPage,
  '/console/library': LibraryPage,
  '/console/starters': StartersPage,
  '/console/domains': DomainsPage,
  '/console/permissions': PermissionsPage,
  '/console/configuration': ConfigurationPage,
  '/console/approvals': ApprovalsPage,
  '/console/masking': MaskingPage,
  '/console/lineage': LineagePage,
  '/console/compliance': CompliancePage,

  '/ask': AskPage,
  '/schema': SchemaPage,
  '/history': HistoryPage,
  '/saved': SavedPage,
  '/dashboards': DashboardsPage,
  '/reports': ReportsPage,
  '/cubes': MetricsPage,
  '/account': AccountPage,
};

export function AppRoutes() {
  return (
    <Routes>
      {/* Outside the shell: the shell's guard sends a sessionless visitor here,
          so a route rendered inside it could never be reached. */}
      <Route
        path="/login"
        element={
          <React.Suspense fallback={<Loading />}>
            <LoginPage />
          </React.Suspense>
        }
      />
      <Route
        path="/password"
        element={
          <React.Suspense fallback={<Loading />}>
            <PasswordPage />
          </React.Suspense>
        }
      />

      <Route element={<AppLayout />}>
        {/* Ask is the product. The console is where you go to administer it,
            not where you land -- and a viewer, who cannot open the console at
            all, would have been redirected straight into a 404. */}
        <Route index element={<Navigate to="/ask" replace />} />

        {/* Not in NAV: reached by opening one from the gallery. Declared before
            the generated routes so `/dashboards/:id` is matched by this rather
            than falling through to the catch-all. */}
        <Route
          path="/dashboards/:dashboardId"
          element={
            <React.Suspense fallback={<Loading />}>
              <DashboardView />
            </React.Suspense>
          }
        />

        {Object.entries(ROUTED).map(([path, Component]) => (
          <Route
            key={path}
            path={path}
            element={
              <React.Suspense fallback={<Loading />}>
                <Component />
              </React.Suspense>
            }
          />
        ))}

        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

function Loading() {
  const t = useLocale().t;
  return (
    <div className="grid h-full place-items-center text-[0.875rem] text-muted-foreground">
      {t('common.loading')}
    </div>
  );
}

// `NotMigrated` lived here, rendering a "this screen has not moved yet" panel
// for any NAV entry with no component. Every NAV path now has one, so the
// filter that fed it was always empty and the component was unreachable --
// removed rather than left as scaffolding that reads like a live state.

function NotFound() {
  const t = useLocale().t;
  const { pathname } = useLocation();
  return (
    <PageBody data-page-state="not-found">
      <EmptyState title={t('shell.notFound')} hint={pathname} />
    </PageBody>
  );
}
