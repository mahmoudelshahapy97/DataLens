"""Agent memory: the store, and the routes over it.

The memory landed with no test of its own, which is a bad place for it to be:
its whole value is that it *persists*, and its whole risk is that it persists
under the wrong scope. A memory that leaks between workspaces is worse than no
memory at all -- it puts one customer's vocabulary into another's answers.

Three properties, at the tier each honestly needs:

**Scope is a database fact**, so the store's isolation is asserted against a
real Postgres. A fake store that filters a dict proves the test's filter works,
not the SQL's.

**The routes carry the caller's scope and nothing else.** There is no parameter
by which one person can name another's memories, so the test is that the shape
of the request cannot express the question -- plus the 404 that covers guessing
an id outright.

**Refusals are 404.** The house rule everywhere else here. A 403 on somebody
else's memory id confirms the id exists.
"""

from __future__ import annotations

import pytest

# Fixtures resolved by name from this module's namespace; ``tests/`` is not a
# package, so this is the flat import pytest's rootdir insertion provides.
from test_tenant_isolation import app_env, world  # noqa: F401

MEMORIES = "/api/vanna/v2/memories"


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestStoreScope:
    """What one workspace saves, another must not be able to read."""

    @staticmethod
    def _memory(app_db, tenant: str):
        from vanna_app.memory_store import PostgresAgentMemory

        # Pinned at construction, the way `TenantPartitionedAgentMemory` builds
        # them -- so the tenant cannot be talked out of it by a crafted context.
        return PostgresAgentMemory(app_db, tenant_id=tenant)

    @staticmethod
    def _context(tenant: str, email: str, memory=None):
        from vanna.core.tool import ToolContext
        from vanna.core.user import User

        # `agent_memory` is required on a ToolContext. Which store is attached
        # is irrelevant to these assertions -- the store under test is the one
        # the call is made *on* -- but the field has to be a store, so it gets
        # the same one rather than a mock that would read as significant.
        return ToolContext(
            user=User(id=email, email=email, tenant_id=tenant),
            conversation_id="test",
            request_id="test",
            tenant_id=tenant,
            agent_memory=memory,
        )

    async def test_a_memory_does_not_cross_workspaces(self, app_db):
        acme = self._memory(app_db, "acme")
        globex = self._memory(app_db, "globex")

        saved = await acme.save_text_memory(
            "Revenue means net of refunds", self._context("acme", "ada@acme.test", acme)
        )

        mine = await acme.get_recent_text_memories(self._context("acme", "ada@acme.test", acme))
        assert [m.content for m in mine] == ["Revenue means net of refunds"]

        # The same person's email in the other workspace: the user matches and
        # the tenant does not, which is the case a user-only filter would miss.
        theirs = await globex.get_recent_text_memories(
            self._context("globex", "ada@acme.test", globex)
        )
        assert theirs == []

        # Nor can it be deleted from there, even knowing the id exactly.
        assert (
            await globex.delete_text_memory(
                self._context("globex", "ada@acme.test", globex), saved.memory_id
            )
            is False
        )
        assert len(await acme.get_recent_text_memories(self._context("acme", "ada@acme.test", acme))) == 1

    async def test_a_memory_does_not_cross_people(self, app_db):
        store = self._memory(app_db, "acme")
        await store.save_text_memory(
            "I work in EUR", self._context("acme", "ada@acme.test", store)
        )

        colleague = await store.get_recent_text_memories(
            self._context("acme", "grace@acme.test", store)
        )
        assert colleague == []

    async def test_it_survives_a_new_store(self, app_db):
        """The point of the whole exercise: it is in the database, not in a process."""
        first = self._memory(app_db, "acme")
        await first.save_text_memory(
            "Fiscal year starts in April", self._context("acme", "ada@acme.test", first)
        )

        second = self._memory(app_db, "acme")
        again = await second.get_recent_text_memories(self._context("acme", "ada@acme.test", second))
        assert [m.content for m in again] == ["Fiscal year starts in April"]


# ----------------------------------------------------------------------
# The routes
# ----------------------------------------------------------------------


@pytest.mark.integration
class TestRoutes:
    async def test_write_read_forget(self, world):
        client = world["clients"]["acme.analyst"]

        created = await client.post(MEMORIES, json={"content": "Prefer bar charts"})
        assert created.status_code == 201, created.text
        memory_id = created.json()["memory_id"]

        listed = await client.get(MEMORIES)
        assert listed.status_code == 200
        assert "Prefer bar charts" in [m["content"] for m in listed.json()["memories"]]

        gone = await client.delete(f"{MEMORIES}/{memory_id}")
        assert gone.status_code == 200

        after = await client.get(MEMORIES)
        assert memory_id not in [m["memory_id"] for m in after.json()["memories"]]

    async def test_an_analyst_needs_no_admin_rights(self, world):
        """These are the caller's own memories.

        A person is entitled to read what has been recorded about them without
        being an administrator of anything -- so this is deliberately *not*
        behind ``require_tenant_admin``, and the test says so out loud.
        """
        viewer = world["clients"]["acme.viewer"]
        assert (await viewer.get(MEMORIES)).status_code == 200
        assert (await viewer.post(MEMORIES, json={"content": "I read only"})).status_code == 201

    async def test_another_workspace_cannot_see_or_delete_it(self, world):
        acme = world["clients"]["acme.admin"]
        globex = world["clients"]["globex.admin"]

        created = await acme.post(MEMORIES, json={"content": "Acme fiscal year is April"})
        memory_id = created.json()["memory_id"]

        theirs = await globex.get(MEMORIES)
        assert "Acme fiscal year is April" not in [
            m["content"] for m in theirs.json()["memories"]
        ]

        # 404, not 403: a 403 here would confirm the id is real.
        assert (await globex.delete(f"{MEMORIES}/{memory_id}")).status_code == 404

        # And it is still there afterwards.
        assert memory_id in [m["memory_id"] for m in (await acme.get(MEMORIES)).json()["memories"]]

    async def test_an_unknown_id_is_404(self, world):
        client = world["clients"]["acme.admin"]
        response = await client.delete(f"{MEMORIES}/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    async def test_a_blank_memory_is_refused(self, world):
        """``min_length`` passes on whitespace, which would store an unnamable row."""
        client = world["clients"]["acme.admin"]
        assert (await client.post(MEMORIES, json={"content": "   "})).status_code == 422
        assert (await client.post(MEMORIES, json={"content": ""})).status_code == 422

    async def test_an_oversized_memory_is_refused(self, world):
        client = world["clients"]["acme.admin"]
        response = await client.post(MEMORIES, json={"content": "x" * 2001})
        assert response.status_code == 422

    async def test_signing_out_does_not_leave_it_readable(self, world):
        """No session, no memories -- not an empty list, a refusal."""
        import httpx

        anonymous = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=world["app"]), base_url="https://test"
        )
        try:
            assert (await anonymous.get(MEMORIES)).status_code == 401
        finally:
            await anonymous.aclose()
