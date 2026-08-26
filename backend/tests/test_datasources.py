"""More than one database per workspace.

``tenants.database_url`` was the whole story until now: one workspace, one
connection, and switching database meant switching workspace. The permission
model was already shaped for more -- ``table_grants``, ``column_grants``,
``grant_policies`` and the schema catalog are all keyed
``(tenant_id, data_source_id, ...)`` -- so the registry is the piece that was
missing rather than a new dimension.

Three properties are pinned here.

**An id from a browser is a preference, not a credential.** It is checked against
the registry before anything is built from it, and an unregistered one is an error
rather than a quiet fallback to the default: a caller who named a database and
silently got a different one would read the answer as being about the one they
asked for.

**Exactly one default, or none.** Enforced by a partial unique index. "Two
defaults" is a state where the answer to "which database?" depends on row order.

**A thread cannot change database.** The binding is claimed by the first message
and is immutable, so a conversation's history is always a record of questions
asked against one schema.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

ADA = "ada@acme.example"
CHINOOK = "postgresql://user:pw@warehouse:5432/chinook"
WORLD = "postgresql://user:pw@warehouse:5432/world"


def a_conversation(conversation_id: str, email: str, *, messages):
    """A real `Conversation`, not a stand-in.

    `get_conversation` re-validates the stored document through the pydantic model,
    so a hand-rolled dict passes `_write` and then fails to load -- which tests the
    fake rather than the store.
    """
    from vanna.core.storage import Conversation
    from vanna.core.storage.models import Message
    from vanna.core.user import User

    return Conversation(
        id=conversation_id,
        user=User(id=email, email=email, tenant_id="acme"),
        messages=[Message(role=role, content=content) for role, content in messages],
    )


@pytest.fixture
def tenants(app_db):
    async def make() -> None:
        for tenant in ("acme", "globex"):
            await app_db.execute(
                "INSERT INTO vanna_app.tenants (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (tenant, tenant.title()),
            )

    return make


@pytest.fixture
async def registry(app_db, tenants):
    from vanna_app.datasources import DataSourceRegistry
    from vanna_app.secrets import Cipher

    await tenants()
    return DataSourceRegistry(app_db, Cipher("k" * 48))


class TestTheRegistry:
    async def test_register_derives_a_credential_free_id(self, registry):
        """The id is the label, and the label never carries a password."""
        registered = await registry.register("acme", CHINOOK, is_default=True)

        assert "pw" not in registered["data_source_id"]
        assert registered["data_source_id"].endswith("/chinook")

    async def test_listing_never_returns_the_connection_string(self, registry):
        await registry.register("acme", CHINOOK, is_default=True)

        listed = await registry.list_sources("acme")
        assert "database_url" not in listed[0]
        assert "pw" not in str(listed[0])

    async def test_the_url_round_trips_when_asked_for(self, registry):
        await registry.register("acme", CHINOOK, is_default=True)

        resolved = await registry.resolve("acme", None)
        assert resolved["database_url"].reveal() == CHINOOK

    async def test_resolving_none_gives_the_default(self, registry):
        await registry.register("acme", CHINOOK, is_default=True)
        await registry.register("acme", WORLD)

        assert (await registry.resolve("acme", None))["data_source_id"].endswith(
            "/chinook"
        )

    async def test_an_unregistered_id_is_an_error_not_a_fallback(self, registry):
        from vanna_app.datasources import UnknownDataSource

        await registry.register("acme", CHINOOK, is_default=True)

        with pytest.raises(UnknownDataSource):
            await registry.resolve("acme", "postgresql://warehouse/payroll")

    async def test_another_workspaces_database_is_unregistered_here(self, registry):
        from vanna_app.datasources import UnknownDataSource

        await registry.register("globex", WORLD, is_default=True)
        world_id = (await registry.resolve("globex", None))["data_source_id"]
        await registry.register("acme", CHINOOK, is_default=True)

        with pytest.raises(UnknownDataSource):
            await registry.resolve("acme", world_id)

    async def test_promoting_a_new_default_demotes_the_old_one(self, registry):
        """A partial unique index would otherwise refuse the write."""
        await registry.register("acme", CHINOOK, is_default=True)
        await registry.register("acme", WORLD, is_default=True)

        listed = await registry.list_sources("acme")
        assert [s["is_default"] for s in listed].count(True) == 1
        assert (await registry.resolve("acme", None))["data_source_id"].endswith("/world")

    async def test_registering_the_same_database_twice_updates_it(self, registry):
        await registry.register("acme", CHINOOK, label="Old", is_default=True)
        await registry.register("acme", CHINOOK, label="New", is_default=True)

        listed = await registry.list_sources("acme")
        assert len(listed) == 1
        assert listed[0]["label"] == "New"

    async def test_an_empty_registry_resolves_to_nothing(self, registry):
        """Not an error: it is the pre-registry world, and the caller falls back."""
        assert await registry.resolve("acme", None) is None


class TestBackfill:
    async def test_it_registers_the_workspaces_existing_database(self, registry):
        assert await registry.backfill("acme", CHINOOK) is not None

        listed = await registry.list_sources("acme")
        assert len(listed) == 1
        assert listed[0]["is_default"] is True

    async def test_it_is_idempotent(self, registry):
        await registry.backfill("acme", CHINOOK)
        assert await registry.backfill("acme", CHINOOK) is None

    async def test_it_never_overwrites_a_deliberate_configuration(self, registry):
        """A workspace that has been configured must not be reset by a backfill."""
        await registry.register("acme", WORLD, label="World", is_default=True)

        assert await registry.backfill("acme", CHINOOK) is None
        assert [s["label"] for s in await registry.list_sources("acme")] == ["World"]

    async def test_no_url_means_nothing_to_register(self, registry):
        """A NULL ``database_url`` means "use the server default"."""
        assert await registry.backfill("acme", "") is None
        assert await registry.list_sources("acme") == []


class TestTheThreadBinding:
    @pytest.fixture
    async def conversations(self, app_db, tenants):
        from vanna_app.stores import PostgresConversationStore

        await tenants()
        return PostgresConversationStore(app_db)

    async def test_the_first_message_claims_the_database(self, conversations):
        bound = await conversations.bind_data_source("acme", "thread-1", "db-a", ADA)

        assert bound == "db-a"
        assert await conversations.data_source_of("acme", "thread-1") == "db-a"

    async def test_a_later_message_cannot_move_the_thread(self, conversations):
        """The property that makes this a binding rather than a parameter.

        Otherwise a thread's history would describe tables that are no longer in
        scope, and a client could switch database by editing one field.
        """
        await conversations.bind_data_source("acme", "thread-1", "db-a", ADA)

        assert await conversations.bind_data_source("acme", "thread-1", "db-b", ADA) == "db-a"
        assert await conversations.data_source_of("acme", "thread-1") == "db-a"

    async def test_an_unbound_thread_reports_nothing(self, conversations):
        """Every conversation that predates the column. Means "the default"."""
        assert await conversations.data_source_of("acme", "never-seen") is None

    async def test_binding_a_thread_does_not_lose_its_transcript(self, conversations):
        """The bug this class did not catch.

        `bind_data_source` inserts the row before the agent writes anything, and it
        used to insert `user_id = ''`. `_write`'s conflict predicate requires the
        owner to match, so every transcript update afterwards was silently
        discarded -- and the row was invisible to `summaries`, `get_conversation`
        and `delete_conversation`, which all filter on the owner. A conversation
        nobody could read, list or delete.

        The client sends a data source on the first message of every new thread
        once a workspace has two databases, so this was every conversation.

        The old tests all passed: they asserted the *binding*, and never that the
        conversation survived it.
        """
        from types import SimpleNamespace

        await conversations.bind_data_source("acme", "thread-9", "db-a", ADA)

        conversation = a_conversation(
            "thread-9", ADA,
            messages=[("user", "how many orders?"), ("assistant", "1,204.")],
        )
        await conversations.update_conversation(conversation)
        user = conversation.user

        listed = await conversations.summaries("acme", ADA)
        assert [row["id"] for row in listed] == ["thread-9"]

        stored = await conversations.get_conversation("thread-9", user)
        assert stored is not None
        assert [m.content for m in stored.messages][0] == "how many orders?"

        # And it can be deleted, which an unowned row could not be.
        assert await conversations.delete_conversation("thread-9", user) is True

    async def test_an_orphaned_thread_is_adopted_on_its_next_turn(
        self, conversations, app_db
    ):
        """A row already orphaned in a running deployment has to recover.

        Ours had some, so the fix cannot only help threads started after it.
        """
        from types import SimpleNamespace

        await app_db.execute(
            "INSERT INTO vanna_app.conversations "
            "(id, tenant_id, user_id, title, document) "
            "VALUES ('thread-old', 'acme', '', '', '{}'::jsonb)"
        )

        await conversations.update_conversation(
            a_conversation("thread-old", ADA, messages=[("user", "still here?")])
        )

        assert [r["id"] for r in await conversations.summaries("acme", ADA)] == [
            "thread-old"
        ]

    async def test_a_bound_but_unwritten_thread_does_not_break_the_next_turn(
        self, conversations
    ):
        """The crash the first version of this fix introduced.

        Giving the placeholder row an owner made it visible to
        `get_conversation` -- which the agent calls at the start of every turn and
        validates. A `{}` document is not a `Conversation`, so the answer failed
        with a ValidationError inside the chat request. Silent data loss traded for
        a crash is not a fix.

        A pinned thread with no transcript is *absent*, and it is not listed.
        """
        from types import SimpleNamespace

        await conversations.bind_data_source("acme", "thread-new", "db-a", ADA)
        user = SimpleNamespace(id=ADA, tenant_id="acme")

        assert await conversations.get_conversation("thread-new", user) is None
        assert await conversations.summaries("acme", ADA) == []

        # ...and the first real turn still lands on that row.
        await conversations.update_conversation(
            a_conversation("thread-new", ADA, messages=[("user", "first question")])
        )
        assert [r["id"] for r in await conversations.summaries("acme", ADA)] == [
            "thread-new"
        ]
        assert await conversations.data_source_of("acme", "thread-new") == "db-a"

    async def test_a_thread_id_does_not_cross_workspaces(self, conversations):
        await conversations.bind_data_source("acme", "shared-id", "db-a", ADA)

        assert await conversations.data_source_of("globex", "shared-id") is None


class TestTheRuntimeCacheIsKeyedOnThePair:
    """One workspace, several databases, one runtime each.

    A runtime owns a SQL runner bound to one connection, so a workspace with two
    databases needs two of them. The cache was keyed on ``tenant_id`` alone, and
    re-keying it is the riskiest part of this change: it touches connection
    pooling, eviction and the readiness probe.
    """

    class FakeRuntime:
        def __init__(self, tenant_id, data_source):
            import time

            self.tenant_id = tenant_id
            self.data_source = data_source
            # As the real TenantRuntime does. Left at 0.0 this reads as older than
            # any TTL, so every lookup would rebuild and the cache test would pass
            # for the wrong reason.
            self.last_used = time.monotonic()
            self.closed = False
            self.retired = False

        def touch(self):
            import time

            self.last_used = time.monotonic()

        def retire(self):
            self.retired = True

        def close(self):
            self.closed = True

    @pytest.fixture
    def platform(self):
        """A Platform with construction and preparation stubbed out.

        Only the cache is under test; building a real runtime would open a
        warehouse connection and scan it.
        """
        from vanna_app.platform import Platform

        from types import SimpleNamespace

        instance = Platform.__new__(Platform)
        # A stub rather than the real Settings, which is a frozen dataclass: the
        # eviction test has to move the bound.
        instance.settings = SimpleNamespace(
            default_tenant="acme",
            tenant_runtime_ttl_seconds=1800,
            max_tenant_runtimes=32,
            database_url="",
        )
        instance.directory = None
        instance.datasources = None
        instance._runtimes = __import__("collections").OrderedDict()
        instance._locks = {}
        instance._locks_guard = __import__("asyncio").Lock()

        async def resolve_source(tenant_id, data_source_id=None):
            return (data_source_id or "default-db"), f"postgresql://x/{data_source_id or 'default-db'}"

        async def build(tenant_id, data_source, database_url):
            return TestTheRuntimeCacheIsKeyedOnThePair.FakeRuntime(tenant_id, data_source)

        async def prepare(runtime):
            return None

        async def clear(*a, **k):
            return 0

        instance.resolve_source = resolve_source
        instance._build_runtime = build
        instance._prepare_tenant = prepare
        instance.catalog = type("C", (), {"clear": staticmethod(clear)})()
        instance.system_context = lambda tenant_id: None
        return instance

    async def test_two_databases_get_two_runtimes(self, platform):
        first = await platform.runtime_for("acme", data_source_id="db-a")
        second = await platform.runtime_for("acme", data_source_id="db-b")

        assert first is not second
        assert len(platform._runtimes) == 2

    async def test_the_same_database_is_cached(self, platform):
        first = await platform.runtime_for("acme", data_source_id="db-a")
        again = await platform.runtime_for("acme", data_source_id="db-a")

        assert first is again
        assert len(platform._runtimes) == 1

    async def test_workspaces_do_not_share_a_slot(self, platform):
        await platform.runtime_for("acme", data_source_id="db-a")
        await platform.runtime_for("globex", data_source_id="db-a")

        assert len(platform._runtimes) == 2

    async def test_invalidating_drops_every_database_of_the_workspace(self, platform):
        """The caller invalidates because the *workspace* changed.

        It does not know which of that workspace's databases were affected, so
        dropping one and leaving the other serving a stale configuration would be
        a bug that only appears on the second database.
        """
        a = await platform.runtime_for("acme", data_source_id="db-a")
        b = await platform.runtime_for("acme", data_source_id="db-b")
        await platform.runtime_for("globex", data_source_id="db-a")

        await platform.runtime_for("acme", data_source_id="db-a", invalidate=True)

        # Retired, not closed. This used to assert `closed`, which pinned a bug:
        # invalidation happens while the workspace is being used, and closing a
        # runtime's connection pool underneath a request that is still running
        # fails that request -- observed under load as a 400 on `/run-sql`, sharing
        # its request id with "connection pool is closed". Dropping the reference
        # releases the connections when the last user lets go instead.
        assert a.retired and b.retired
        assert not a.closed and not b.closed
        # globex is untouched, and acme/db-a was rebuilt.
        assert {key[0] for key in platform._runtimes} == {"acme", "globex"}

    async def test_the_bound_now_counts_pairs(self, platform):
        """``VANNA_MAX_TENANT_RUNTIMES`` bounds warehouse connection pools.

        It counts (workspace, database) pairs now, which is what the operations
        note has to say: the product that must fit the warehouse's connection
        limit grew by the number of databases per workspace.
        """
        platform.settings.max_tenant_runtimes = 2

        await platform.runtime_for("acme", data_source_id="db-a")
        await platform.runtime_for("acme", data_source_id="db-b")
        await platform.runtime_for("acme", data_source_id="db-c")

        assert len(platform._runtimes) == 2
