"""The admin overview and the audit export.

Two properties, and they need different tiers.

**The shaping is pure.** Success rate, the platform roll-up and the empty-window
case are arithmetic over dicts, so they are asserted without a database and run in
the fast tier on every commit.

**The scoping is not.** "A workspace admin sees their own workspace and gets a 404
for anybody else's" is a claim about the real routes with real sessions, and the
only honest way to assert it is to drive the application. Those tests reuse the
world that ``test_tenant_isolation`` already builds -- two workspaces, a full cast
in each, a signed-in client per person.

The overview is the first admin surface whose workspace is a *query* parameter
rather than a path segment, so it is invisible to the ``CROSS_TENANT_ROUTES``
matrix next door. That is exactly why it is written out here: a route that opts
out of the matrix by its shape is a route with no isolation proof at all.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest

# Fixtures, imported for their side effect of being in this module's namespace --
# pytest resolves them by name from here. Re-declaring a hundred lines of database
# and login setup so the import list looks tidy would be the worse trade.
# ``tests/`` is not a package, so this is the flat import pytest's rootdir
# insertion makes available -- not a relative one.
from test_tenant_isolation import app_env, world  # noqa: F401

OVERVIEW = "/api/vanna/v2/admin/overview"
AUDIT_CSV = "/api/vanna/v2/admin/audit.csv"
ACCESS_LOG = "/api/vanna/v2/admin/access-log"


# ----------------------------------------------------------------------
# Shaping, without a database
# ----------------------------------------------------------------------


class TestShaping:
    def test_an_empty_window_has_no_success_rate(self):
        """Not zero.

        A window nobody asked a question in has no success rate, and rendering
        one as "0%" reads as a platform failing every request it received.
        """
        from vanna_app.routes.overview import _rate

        assert _rate(0, 0) is None
        assert _rate(0, 4) == 0.0
        assert _rate(3, 4) == 0.75

    def test_tenant_kpis_survive_nulls(self):
        """``tenant_usage`` returns NULLs for a workspace with no generations."""
        from vanna_app.routes.overview import _tenant_kpis

        kpis = _tenant_kpis(
            {"questions": None, "succeeded": None, "members": 3, "liked": None}
        )
        assert kpis["questions"] == 0
        assert kpis["members"] == 3
        assert kpis["success_rate"] is None
        assert kpis["workspaces"] == 1

    def test_platform_kpis_add_up(self):
        """The counts are nested under `usage`, which is the shape that bit.

        The first version of this test hand-built a flat row, so it agreed with
        the bug rather than catching it: read at the wrong level every total is
        `0`, and a platform with no traffic looks exactly the same. It took a
        call against a real database to notice.
        """
        from vanna_app.routes.overview import _platform_kpis

        kpis = _platform_kpis(
            [
                {"id": "acme", "name": "Acme", "is_active": True,
                 "usage": {"questions": 10, "succeeded": 9, "members": 2,
                           "active_users": 2, "liked": 1, "disliked": 0,
                           "last_activity": "2026-08-01T00:00:00"}},
                {"id": "globex", "name": "Globex", "is_active": False,
                 "usage": {"questions": 30, "succeeded": 21, "members": 5,
                           "active_users": 4, "liked": 2, "disliked": 1,
                           "last_activity": "2026-08-20T00:00:00"}},
            ]
        )
        assert kpis["workspaces"] == 2
        assert kpis["active_workspaces"] == 1
        assert kpis["questions"] == 40
        assert kpis["success_rate"] == 0.75
        # Distinct-per-workspace counts, added up. Somebody in both workspaces is
        # counted twice, which is what the per-workspace table below it shows.
        assert kpis["active_users"] == 6
        assert kpis["last_activity"] == "2026-08-20T00:00:00"

    def test_a_workspace_with_no_usage_key_is_not_a_crash(self):
        """`usage` is always present in practice; absent must still mean zero."""
        from vanna_app.routes.overview import _platform_kpis

        kpis = _platform_kpis([{"id": "acme", "name": "Acme", "is_active": True}])
        assert kpis["workspaces"] == 1
        assert kpis["questions"] == 0
        assert kpis["success_rate"] is None

    def test_platform_kpis_of_nothing(self):
        from vanna_app.routes.overview import _platform_kpis

        kpis = _platform_kpis([])
        assert kpis["workspaces"] == 0
        assert kpis["success_rate"] is None
        assert kpis["last_activity"] is None


# ----------------------------------------------------------------------
# The series, against the real schema
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestActivitySeries:
    @pytest.fixture
    async def seeded(self, app_db, directory):
        await directory.create_tenant("acme", name="Acme")
        await directory.create_tenant("globex", name="Globex")
        # Two questions today in acme, one three days ago; one today in globex.
        # `id` is a text primary key with no default -- the store supplies one.
        for index, (tenant, days_ago, status) in enumerate((
            ("acme", 0, "valid"),
            ("acme", 0, "error"),
            ("acme", 3, "valid"),
            ("globex", 0, "valid"),
        )):
            await app_db.execute(
                """INSERT INTO vanna_app.generations
                       (id, tenant_id, user_id, question, sql, status, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s,
                           now() - make_interval(days => %s))""",
                (f"gen-{index}", tenant, "ada@acme.test", "q", "select 1",
                 status, days_ago),
            )
        return directory

    async def test_quiet_days_are_zeros_not_gaps(self, seeded):
        """A line chart plots what it is given against an even axis.

        A series that omits the days nobody asked anything draws the gap as a
        straight line between two busy days -- a chart wrong about its own x-axis.

        Asserted against the shape rather than against today's date: the bucket a
        row lands in is the *database's* day, and this suite runs on machines
        whose local date differs from it for part of every night. An earlier
        version of this test compared to ``date.today()`` and failed at 02:00
        local for exactly that reason -- which is the bug, not the test.
        """
        series = await seeded.activity_series("acme", days=7)

        assert len(series) == 7
        days = [date.fromisoformat(row["day"]) for row in series]
        assert days == sorted(days), "oldest first"
        assert all(
            later - earlier == timedelta(days=1)
            for earlier, later in zip(days, days[1:])
        ), "consecutive, with no day missing"

        assert sum(row["questions"] for row in series) == 3
        assert sum(row["succeeded"] for row in series) == 2

        busiest = max(series, key=lambda row: row["questions"])
        assert busiest["questions"] == 2
        # One of the two errored, so it is a question but not a success.
        assert busiest["succeeded"] == 1

        assert any(row["questions"] == 0 for row in series), "quiet days are present"

    async def test_an_empty_tenant_id_spans_the_platform(self, seeded):
        scoped = await seeded.activity_series("acme", days=7)
        every = await seeded.activity_series("", days=7)

        assert sum(row["questions"] for row in scoped) == 3
        assert sum(row["questions"] for row in every) == 4

    async def test_the_window_is_clamped(self, seeded):
        assert len(await seeded.activity_series("acme", days=0)) == 1
        assert len(await seeded.activity_series("acme", days=10_000)) == 365

    async def test_platform_spend_covers_every_workspace(self, seeded):
        spend = await seeded.platform_spend(days=30)

        assert spend["questions"] == 4
        # Nothing populated cost on these rows; the sum is 0.0, not None.
        assert spend["cost_usd"] == 0.0


@pytest.mark.integration
class TestUnhealthySources:
    async def test_unknown_and_failing_are_both_reported(self, app_db, settings):
        """A source nobody has checked must not render as healthy."""
        from vanna_app.datasources import DataSourceRegistry
        from vanna_app.secrets import Cipher

        registry = DataSourceRegistry(app_db, Cipher(settings.secret_key))
        for tenant in ("acme", "globex"):
            await app_db.execute(
                "INSERT INTO vanna_app.tenants (id, name) VALUES (%s, %s)",
                (tenant, tenant.title()),
            )
        await registry.register("acme", "postgresql://u:p@a:5432/one", label="one")
        await registry.register("globex", "postgresql://u:p@b:5432/two", label="two")

        healthy = (await registry.list_sources("acme"))[0]["data_source_id"]
        await registry.record_health("acme", healthy, ok=True)

        broken = (await registry.list_sources("globex"))[0]["data_source_id"]
        await registry.record_health("globex", broken, ok=False, error="no route")

        rows = await registry.unhealthy_sources()
        reported = {row["data_source_id"]: row for row in rows}

        assert healthy not in reported, "a source known to be working is not a problem"
        assert reported[broken]["last_ok"] is False
        assert reported[broken]["last_error"] == "no route"
        assert reported[broken]["tenant_id"] == "globex"
        assert "database_url" not in reported[broken]


# ----------------------------------------------------------------------
# Who may see what
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestOverviewScoping:
    async def test_a_platform_admin_naming_nobody_gets_the_platform(self, world):
        response = await world["clients"]["platform"].get(OVERVIEW)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["scope"] == ""
        assert body["kpis"]["workspaces"] == 2
        assert {row["id"] for row in body["workspaces"]} == {"acme", "globex"}
        assert "cost_usd" in body["kpis"]
        assert len(body["series"]) == 30

        # The roll-up has to agree with the rows it is a roll-up of. Asserting
        # only that the key exists is what let a total of zero ship.
        assert body["kpis"]["members"] == sum(
            row["usage"]["members"] for row in body["workspaces"]
        )
        assert body["kpis"]["members"] > 0

    async def test_a_platform_admin_may_name_any_workspace(self, world):
        response = await world["clients"]["platform"].get(
            OVERVIEW, params={"tenant_id": "globex"}
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["scope"] == "globex"
        assert "workspaces" not in body

    async def test_a_workspace_admin_sees_only_their_own(self, world):
        response = await world["clients"]["acme.admin"].get(OVERVIEW)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["scope"] == "acme"
        assert "workspaces" not in body
        # Cost and datasource health are platform-admin surfaces. Omitted, not
        # zeroed: "we do not show you this" and "you spent nothing" differ.
        assert "cost_usd" not in body["kpis"]
        assert "data_sources" not in body
        assert "spend" not in body

    async def test_naming_somebody_elses_workspace_is_a_404(self, world):
        """404, not 403. A 403 confirms the workspace exists."""
        response = await world["clients"]["acme.admin"].get(
            OVERVIEW, params={"tenant_id": "globex"}
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("who", ["acme.analyst", "acme.viewer"])
    async def test_non_admins_are_refused_in_their_own_workspace(self, world, who):
        response = await world["clients"][who].get(OVERVIEW)
        assert response.status_code == 404

    async def test_the_window_is_clamped(self, world):
        response = await world["clients"]["acme.admin"].get(
            OVERVIEW, params={"days": 9999}
        )
        assert response.status_code == 200
        assert response.json()["window_days"] == 365

    async def test_the_action_vocabulary_is_served_not_guessed(self, world):
        """The console builds its filter from this, so it cannot drift."""
        from vanna_app.audit import ACTIONS

        response = await world["clients"]["acme.admin"].get(OVERVIEW)
        assert response.json()["actions"] == list(ACTIONS)


@pytest.mark.integration
class TestAuditExport:
    async def test_it_is_a_csv_with_a_header(self, world):
        # Make something worth exporting.
        created = await world["clients"]["acme.admin"].post(
            "/api/vanna/v2/admin/tenants/acme/users",
            json={"email": "grace@acme.test", "role": "analyst"},
        )
        assert created.status_code in (200, 201), created.text

        response = await world["clients"]["acme.admin"].get(AUDIT_CSV)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert "audit-acme.csv" in response.headers["content-disposition"]

        rows = list(csv.reader(io.StringIO(response.text)))
        assert rows[0] == [
            "created_at", "actor_email", "action", "tenant_id", "target",
            "actor_ip", "details",
        ]
        actions = [row[2] for row in rows[1:]]
        assert "member.add" in actions
        assert all(len(row) == len(rows[0]) for row in rows[1:])

    async def test_it_is_scoped_like_the_overview(self, world):
        response = await world["clients"]["acme.admin"].get(
            AUDIT_CSV, params={"tenant_id": "globex"}
        )
        assert response.status_code == 404

    async def test_a_platform_export_is_named_for_the_platform(self, world):
        response = await world["clients"]["platform"].get(AUDIT_CSV)
        assert response.status_code == 200
        assert "audit-platform.csv" in response.headers["content-disposition"]


@pytest.mark.integration
class TestAccessLogScoping:
    async def test_a_platform_admin_may_read_another_workspaces_log(self, world):
        """It used to ignore the parameter and answer about the caller's own.

        A platform admin administering globex was shown acme's access log under a
        globex heading, with nothing on the screen to say so.
        """
        response = await world["clients"]["platform"].get(
            ACCESS_LOG, params={"tenant_id": "globex"}
        )
        assert response.status_code == 200, response.text
        assert "events" in response.json()

    async def test_a_workspace_admin_may_not(self, world):
        response = await world["clients"]["acme.admin"].get(
            ACCESS_LOG, params={"tenant_id": "globex"}
        )
        assert response.status_code == 404

    async def test_it_still_defaults_to_your_own(self, world):
        response = await world["clients"]["acme.admin"].get(ACCESS_LOG)
        assert response.status_code == 200, response.text
