import { useLocation, useNavigate } from 'react-router-dom';

import { PageBody, PageHeader } from '@/components/primitives/page';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useLocale } from '@/i18n';

import { OverviewScreen } from './OverviewPage';
import { UsageScreen } from './UsagePage';

/**
 * Activity and cost, on one page.
 *
 * ## Why these were merged
 *
 * They were two nav entries with the same icon rendering the same five KPI tiles
 * -- questions, success rate, members, active users, spend -- from two different
 * endpoints. `/admin/overview` answers for whatever the scope picker names,
 * including platform-wide; `/admin/tenants/{id}/usage` answers for exactly one
 * workspace. So the two screens could show *different numbers under identical
 * labels*, and nothing on either said which scope it meant. Somebody comparing
 * them would reasonably conclude one was wrong.
 *
 * Merging does not average them or pick a winner. Both readings are correct and
 * both are kept; putting them behind tabs on one page is what makes the
 * distinction visible, because you now switch between them deliberately instead
 * of arriving at one and not knowing the other existed.
 *
 * ## Nothing was removed
 *
 * `/console/overview` and `/console/usage` both still resolve -- they open this
 * page on their respective tab, so existing links and bookmarks land where they
 * always did. Switching tabs rewrites the URL, so a link copied from here is
 * stable too.
 */

type TabName = 'activity' | 'cost';

const ROUTES: Record<TabName, string> = {
  activity: '/console/overview',
  cost: '/console/usage',
};

export function ActivityPage({ tab }: { tab: TabName }) {
  const { t } = useLocale();
  const navigate = useNavigate();
  const { pathname } = useLocation();

  // The route is the source of truth, not local state: arriving at
  // /console/usage must open the cost tab even on a fresh load, and the back
  // button should move between tabs the way it moves between pages.
  const active: TabName = pathname === ROUTES.cost ? 'cost' : tab;

  return (
    <PageBody>
      <PageHeader
        title={t('activity.title')}
        description={
          active === 'activity' ? t('ov.blurb') : t('use.blurb', { workspace: '' })
        }
      />

      <Tabs
        value={active}
        onValueChange={(next) => navigate(ROUTES[next as TabName], { replace: true })}
      >
        <TabsList className="mb-4">
          <TabsTrigger value="activity">{t('activity.tabActivity')}</TabsTrigger>
          <TabsTrigger value="cost">{t('activity.tabCost')}</TabsTrigger>
        </TabsList>

        {/* Only the visible tab is mounted. Both fetch on mount, and rendering
            the hidden one would double the request count on every page load for
            numbers nobody is looking at. */}
        <TabsContent value="activity">
          {active === 'activity' ? <OverviewScreen embedded /> : null}
        </TabsContent>

        <TabsContent value="cost">
          {active === 'cost' ? <UsageScreen embedded /> : null}
        </TabsContent>
      </Tabs>
    </PageBody>
  );
}

export function ActivityOverviewPage() {
  return <ActivityPage tab="activity" />;
}

export function ActivityCostPage() {
  return <ActivityPage tab="cost" />;
}

export default ActivityOverviewPage;
