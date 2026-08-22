"""Reading, filtering and forgetting past turns.

History was write-only from the user's side: one route, no delete, a hard-coded
hundred rows, and every filter applied in the browser over whatever those hundred
happened to be. Three properties are pinned here.

**Shared for reading is not shared for writing.** ``GET /history`` deliberately
returns the whole workspace -- seeing what a colleague already asked is the point
of it -- which means every member holds the ids and ``request_id``s of everybody
else's rows. So each mutation is scoped to the owner in the *store*, not by a check
at the route: there is no path to another member's row that skips it.

**A rating is not inert.** A positive one makes the turn a candidate example, which
then steers the model for the whole workspace. ``set_feedback`` had a tenant-only
predicate, so any member could rate anybody's turn.

**Filtering belongs in the database.** "The failures from last Tuesday" is not
reachable by narrowing a page of recent rows in the browser.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

ADA = "ada@acme.example"
BOB = "bob@acme.example"

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def generations(app_db):
    from vanna_app.stores import PostgresGenerationStore

    for tenant in ("acme", "globex"):
        await app_db.execute(
            "INSERT INTO vanna_app.tenants (id, name) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tenant, tenant.title()),
        )
    return PostgresGenerationStore(app_db)


def a_generation(
    identifier,
    *,
    user_id=ADA,
    tenant_id="acme",
    question="how many tracks?",
    status="valid",
    created_at=NOW,
    request_id=None,
):
    from vanna.core.generation import GenerationStatus, SqlGeneration

    return SqlGeneration(
        id=identifier,
        tenant_id=tenant_id,
        user_id=user_id,
        question=question,
        sql="SELECT count(*) FROM tracks",
        status=GenerationStatus(status),
        request_id=request_id or f"req-{identifier}",
        created_at=created_at,
    )


class Ctx:
    """The two attributes ``set_feedback`` reads off a ``ToolContext``."""

    def __init__(self, tenant_id="acme", user_id=ADA):
        self.tenant_id = tenant_id
        self.user = type("U", (), {"id": user_id})()


class TestFilteringHappensInTheDatabase:
    async def test_status_narrows_the_rows(self, generations):
        await generations.record(await _ctx(), a_generation("g1", status="valid"))
        await generations.record(await _ctx(), a_generation("g2", status="invalid"))

        rows = await generations.history("acme", status="invalid")

        assert [row["id"] for row in rows] == ["g2"]

    async def test_a_date_window_narrows_the_rows(self, generations):
        await generations.record(
            await _ctx(), a_generation("old", created_at=NOW - timedelta(days=10))
        )
        await generations.record(await _ctx(), a_generation("new", created_at=NOW))

        rows = await generations.history("acme", since=NOW - timedelta(days=1))

        assert [row["id"] for row in rows] == ["new"]

    async def test_until_is_exclusive_so_two_windows_do_not_overlap(self, generations):
        await generations.record(await _ctx(), a_generation("boundary", created_at=NOW))

        before = await generations.history("acme", until=NOW)
        after = await generations.history("acme", since=NOW)

        assert before == []
        assert [row["id"] for row in after] == ["boundary"]

    async def test_offset_pages_without_repeating_a_row(self, generations):
        for index in range(5):
            await generations.record(
                await _ctx(),
                a_generation(f"g{index}", created_at=NOW - timedelta(minutes=index)),
            )

        first = await generations.history("acme", limit=2, offset=0)
        second = await generations.history("acme", limit=2, offset=2)

        assert [row["id"] for row in first] == ["g0", "g1"]
        assert [row["id"] for row in second] == ["g2", "g3"]

    async def test_an_unknown_status_matches_nothing_rather_than_everything(
        self, generations
    ):
        """A filter that silently stops filtering is worse than an empty list."""
        await generations.record(await _ctx(), a_generation("g1"))

        assert await generations.history("acme", status="not-a-status") == []


class TestForgettingYourOwnTurns:
    async def test_deleting_one_of_your_own_removes_it(self, generations):
        await generations.record(await _ctx(), a_generation("mine"))

        assert await generations.delete_history_row("acme", ADA, "mine") == 1
        assert await generations.history("acme") == []

    async def test_another_members_row_is_untouched(self, generations):
        """Every member is handed everybody's row ids by ``GET /history``."""
        await generations.record(await _ctx(), a_generation("theirs", user_id=BOB))

        assert await generations.delete_history_row("acme", ADA, "theirs") == 0
        assert [row["id"] for row in await generations.history("acme")] == ["theirs"]

    async def test_another_workspaces_row_is_untouched(self, generations):
        await generations.record(
            await _ctx("globex"),
            a_generation("elsewhere", tenant_id="globex", user_id=ADA),
        )

        assert await generations.delete_history_row("acme", ADA, "elsewhere") == 0
        assert len(await generations.history("globex")) == 1

    async def test_clearing_takes_only_your_own(self, generations):
        await generations.record(await _ctx(), a_generation("mine-1"))
        await generations.record(await _ctx(), a_generation("mine-2"))
        await generations.record(await _ctx(), a_generation("theirs", user_id=BOB))

        assert await generations.delete_history("acme", ADA) == 2

        left = await generations.history("acme")
        assert [row["id"] for row in left] == ["theirs"]

    async def test_clearing_does_not_reach_another_workspace(self, generations):
        await generations.record(
            await _ctx("globex"), a_generation("elsewhere", tenant_id="globex")
        )

        assert await generations.delete_history("acme", ADA) == 0
        assert len(await generations.history("globex")) == 1


class TestRatingIsOwnerOnly:
    async def test_the_author_can_rate_their_own_turn(self, generations):
        from vanna.core.generation import Feedback

        await generations.record(await _ctx(), a_generation("mine", request_id="r1"))

        updated = await generations.set_feedback(Ctx(), "r1", Feedback.POSITIVE)

        assert updated == 1
        assert (await generations.history("acme"))[0]["feedback"] == "positive"

    async def test_another_member_cannot_rate_it(self, generations):
        """A positive rating promotes the turn to a candidate example, so this is
        not a cosmetic write -- it steers what the model is shown next."""
        from vanna.core.generation import Feedback

        await generations.record(await _ctx(), a_generation("mine", request_id="r1"))

        updated = await generations.set_feedback(
            Ctx(user_id=BOB), "r1", Feedback.NEGATIVE
        )

        assert updated == 0
        assert (await generations.history("acme"))[0]["feedback"] is None

    async def test_another_workspace_cannot_rate_it(self, generations):
        from vanna.core.generation import Feedback

        await generations.record(await _ctx(), a_generation("mine", request_id="r1"))

        updated = await generations.set_feedback(
            Ctx(tenant_id="globex"), "r1", Feedback.NEGATIVE
        )

        assert updated == 0


async def _ctx(tenant_id="acme"):
    """Minimal context for ``record``, which only reads the tenant off it."""
    return Ctx(tenant_id=tenant_id)
