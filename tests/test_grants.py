"""Grant resolution: the authorization surface writes are checked against.

These are the rules that decide what a caller can touch at all, so they are
tested as properties rather than as examples. Each test names the property in
its docstring, because a reader of a failing test needs to know what invariant
broke, not which fixture changed.
"""

from types import SimpleNamespace

import pytest

from vanna.core.grants import (
    ColumnGrant,
    GrantStore,
    TableGrant,
    catalog_write_facts,
    normalize_table,
    resolve_grants,
)
from vanna.integrations.local import MemoryGrantStore


def table_grant(role, table, **verbs):
    return TableGrant(data_source_id="wh", role=role, table=table, **verbs)


def column_grant(role, table, column, **flags):
    return ColumnGrant(
        data_source_id="wh", role=role, table=table, column=column, **flags
    )


@pytest.fixture
def context():
    return SimpleNamespace(tenant_id="acme")


class TestGrantInvariants:
    """A write grant cannot exist without the read grant it depends on."""

    @pytest.mark.parametrize("verb", ["can_insert", "can_update", "can_delete"])
    def test_table_write_requires_select(self, verb):
        with pytest.raises(ValueError, match="can_select"):
            table_grant("analyst", "orders", **{verb: True})

    @pytest.mark.parametrize("flag", ["can_filter", "can_aggregate", "can_write"])
    def test_column_flags_require_read(self, flag):
        with pytest.raises(ValueError, match="can_read"):
            column_grant("analyst", "orders", "status", **{flag: True})

    def test_write_with_select_is_accepted(self):
        grant = table_grant("admin", "orders", can_select=True, can_update=True)
        assert grant.can_update and grant.key == "orders"


class TestResolution:
    def test_roles_union_never_intersect(self):
        """Holding another role can only ever widen what a caller may do."""
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["analyst", "admin"],
            table_grants=[
                table_grant("analyst", "sales.orders", can_select=True),
                table_grant("admin", "sales.orders", can_select=True, can_update=True),
            ],
            column_grants=[
                column_grant("analyst", "sales.orders", "id", can_read=True),
                column_grant("admin", "sales.orders", "id", can_read=True),
                column_grant(
                    "admin", "sales.orders", "status", can_read=True, can_write=True
                ),
            ],
            key_columns={"sales.orders": ["id"]},
        )
        assert resolved.table("sales.orders").can_update

    def test_a_role_the_caller_lacks_is_ignored(self):
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["viewer"],
            table_grants=[table_grant("admin", "orders", can_select=True)],
            column_grants=[column_grant("admin", "orders", "id", can_read=True)],
        )
        assert resolved.is_empty

    def test_ungranted_column_is_absent_not_masked(self):
        """A withheld column must be unnameable, not merely unreadable.

        Dropping it makes referencing it an unknown-column error, which reveals
        nothing. Masking it would confirm the column exists.
        """
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["admin"],
            table_grants=[table_grant("admin", "orders", can_select=True)],
            column_grants=[column_grant("admin", "orders", "id", can_read=True)],
        )
        assert resolved.table("orders").column("salary") is None

    def test_table_with_no_readable_columns_is_dropped(self):
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["admin"],
            table_grants=[table_grant("admin", "orders", can_select=True)],
            column_grants=[],
        )
        assert resolved.is_empty

    def test_generated_column_is_never_assignable(self):
        """However it was granted -- the database refuses the assignment."""
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["admin"],
            table_grants=[table_grant("admin", "orders", can_select=True, can_update=True)],
            column_grants=[
                column_grant("admin", "orders", "id", can_read=True, can_write=True),
                column_grant("admin", "orders", "status", can_read=True, can_write=True),
            ],
            key_columns={"orders": ["id"]},
            unassignable_columns={"orders": ["id"]},
        )
        table = resolved.table("orders")
        assert table.column("id").can_write is False
        assert table.column("status").can_write is True

    def test_verbs_need_something_to_act_on(self):
        """Without a key column, UPDATE and DELETE cannot name their rows."""
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["admin"],
            table_grants=[
                table_grant(
                    "admin", "events", can_select=True,
                    can_insert=True, can_update=True, can_delete=True,
                )
            ],
            column_grants=[
                column_grant("admin", "events", "payload", can_read=True, can_write=True)
            ],
            key_columns={"events": ["event_id"]},
        )
        events = resolved.table("events")
        assert events.can_insert
        assert not events.can_update
        assert not events.can_delete


class TestNameResolution:
    @pytest.mark.parametrize(
        "written,expected",
        [("Sales.Orders", "sales.orders"), ('"orders"', "orders"), ("[dbo].[T]", "dbo.t")],
    )
    def test_normalization_is_spelling_insensitive(self, written, expected):
        assert normalize_table(written) == expected

    def test_bare_name_resolves_while_unambiguous(self):
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["admin"],
            table_grants=[table_grant("admin", "sales.orders", can_select=True)],
            column_grants=[column_grant("admin", "sales.orders", "id", can_read=True)],
        )
        assert resolved.table("orders") is not None

    def test_ambiguous_bare_name_refuses_rather_than_guesses(self):
        """Picking one of two tables called `orders` is not help anybody wants."""
        resolved = resolve_grants(
            data_source_id="wh",
            roles=["admin"],
            table_grants=[
                table_grant("admin", "sales.orders", can_select=True),
                table_grant("admin", "ops.orders", can_select=True),
            ],
            column_grants=[
                column_grant("admin", "sales.orders", "id", can_read=True),
                column_grant("admin", "ops.orders", "id", can_read=True),
            ],
        )
        assert resolved.table("orders") is None
        assert resolved.table("sales.orders") is not None


class TestCatalogFacts:
    def test_extracts_keys_and_generated_columns(self):
        from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata

        keys, unassignable = catalog_write_facts([
            TableMetadata(table_name="orders", schema_name="sales", columns=[
                ColumnMetadata(name="id", is_primary_key=True, is_generated=True),
                ColumnMetadata(name="status"),
            ])
        ])
        assert keys == {"sales.orders": ["id"]}
        assert unassignable == {"sales.orders": ["id"]}


class TestMemoryGrantStore:
    async def test_every_mutation_bumps_the_version(self, context):
        """The version is what an approved write is re-checked against.

        A grant change that does not move it is a change no in-flight write
        will notice.
        """
        store = MemoryGrantStore()
        assert await store.version(context, data_source_id="wh") == 0

        await store.set_table_grant(
            context, table_grant("admin", "orders", can_select=True, can_update=True)
        )
        first = await store.version(context, data_source_id="wh")
        assert first > 0

        await store.set_column_grant(
            context, column_grant("admin", "orders", "status", can_read=True)
        )
        assert await store.version(context, data_source_id="wh") > first

    async def test_resolve_reports_the_version_it_read_under(self, context):
        store = MemoryGrantStore()
        await store.set_table_grant(context, table_grant("admin", "orders", can_select=True))
        await store.auto_grant_columns(
            context, data_source_id="wh", role="admin", table="orders", columns=["id"]
        )
        resolved = await store.resolve(context, data_source_id="wh", roles=["admin"])
        assert resolved.version == await store.version(context, data_source_id="wh")

    async def test_auto_grant_leaves_existing_choices_alone(self, context):
        """An administrator who withheld a column must not silently get it back."""
        store = MemoryGrantStore()
        await store.set_table_grant(context, table_grant("admin", "orders", can_select=True))
        await store.set_column_grant(
            context, column_grant("admin", "orders", "salary", can_read=False)
        )
        await store.auto_grant_columns(
            context, data_source_id="wh", role="admin", table="orders",
            columns=["id", "salary"],
        )
        resolved = await store.resolve(context, data_source_id="wh", roles=["admin"])
        assert resolved.table("orders").column("salary") is None
        assert resolved.table("orders").column("id") is not None

    async def test_tenants_are_isolated(self, context):
        store = MemoryGrantStore()
        await store.set_table_grant(context, table_grant("admin", "orders", can_select=True))
        await store.auto_grant_columns(
            context, data_source_id="wh", role="admin", table="orders", columns=["id"]
        )
        other = SimpleNamespace(tenant_id="globex")
        assert (await store.resolve(other, data_source_id="wh", roles=["admin"])).is_empty


class TestAutofillContract:
    """Every store must honour autofill's ownership rule, the base class included.

    ``MemoryGrantStore`` and ``PostgresGrantStore`` both override
    ``auto_grant_columns`` with one statement each. The base class keeps a
    generic implementation for any store outside this repository -- and it is
    the documented contract, so it has to behave the same way. Parametrising
    over both is what stops the two drifting.
    """

    class BareStore(MemoryGrantStore):
        """MemoryGrantStore with the inherited autofill instead of its own.

        Exercises `GrantStore.auto_grant_columns` over the primitive methods,
        which is the path a third-party store would take.
        """

        auto_grant_columns = GrantStore.auto_grant_columns

    @pytest.fixture(params=["overridden", "inherited"])
    def store(self, request):
        if request.param == "inherited":
            return self.BareStore(
                key_columns={"orders": ["id"]}, unassignable_columns={"orders": ["id"]}
            )
        return MemoryGrantStore(
            key_columns={"orders": ["id"]}, unassignable_columns={"orders": ["id"]}
        )

    async def _autofill(self, store, context, *, can_write):
        await store.auto_grant_columns(
            context, data_source_id="wh", role="admin", table="orders",
            columns=["id", "status", "note"], can_write=can_write,
            unassignable=["id"],
        )

    async def _writable(self, store, context):
        grants = await store.list_column_grants(
            context, data_source_id="wh", role="admin", table="orders"
        )
        return {g.column: g.can_write for g in grants}

    async def test_it_stamps_its_own_rows(self, store, context):
        await self._autofill(store, context, can_write=False)
        grants = await store.list_column_grants(
            context, data_source_id="wh", role="admin", table="orders"
        )
        assert grants and all(g.is_autofilled for g in grants)

    async def test_a_generated_column_is_never_writable(self, store, context):
        await self._autofill(store, context, can_write=True)
        assert (await self._writable(store, context))["id"] is False

    async def test_write_arrives_after_a_read_only_pass(self, store, context):
        """The regression: gaps-only autofill left every column unwritable, so
        a table granted Read & write had write verbs and nothing to apply them
        to -- which build_write_policy resolves by dropping the verbs."""
        await self._autofill(store, context, can_write=False)
        assert (await self._writable(store, context))["status"] is False

        await self._autofill(store, context, can_write=True)
        assert (await self._writable(store, context))["status"] is True

    async def test_write_is_withdrawn_when_the_table_stops_being_writable(
        self, store, context
    ):
        await self._autofill(store, context, can_write=True)
        await self._autofill(store, context, can_write=False)
        assert (await self._writable(store, context))["status"] is False

    async def test_a_persons_choice_outlives_every_later_pass(self, store, context):
        """Autofill does not argue with an administrator."""
        await self._autofill(store, context, can_write=True)
        await store.set_column_grant(context, ColumnGrant(
            data_source_id="wh", role="admin", table="orders", column="status",
            can_read=True, can_write=False, granted_by="alice@example.com",
        ))

        await self._autofill(store, context, can_write=True)
        writable = await self._writable(store, context)
        assert writable["status"] is False, "a withheld column must stay withheld"
        assert writable["note"] is True, "autofill's own rows still follow"

    async def test_read_is_never_taken_away(self, store, context):
        await self._autofill(store, context, can_write=True)
        await self._autofill(store, context, can_write=False)
        grants = await store.list_column_grants(
            context, data_source_id="wh", role="admin", table="orders"
        )
        assert all(g.can_read for g in grants)


class TestOwnership:
    """Who owns a column grant, and therefore whether autofill may adjust it."""

    @pytest.mark.parametrize("granted_by,managed", [
        (None, True),                    # predates provenance
        ("", True),
        ("   ", True),
        ("autofill", True),
        ("system:preset", True),
        ("system:anything", True),
        ("alice@example.com", False),    # a person claimed it
        ("u-1234", False),
    ])
    def test_only_a_person_claims_a_row(self, granted_by, managed):
        grant = column_grant("admin", "orders", "status",
                             can_read=True, granted_by=granted_by)
        assert grant.is_machine_managed is managed

    def test_unclaimed_counts_as_machine_managed(self):
        """The alternative is worse in a specific way.

        Autofill has to be able to raise can_write when a table becomes
        writable. Treating an unclaimed row as a person's leaves the table
        holding write verbs with nothing assignable, which build_write_policy
        answers by dropping the verbs -- so "Read & write" grants nothing and
        reports success.
        """
        assert column_grant("admin", "t", "c", can_read=True).is_machine_managed


class TestPresetsAndTableAccess:
    """A preset must not leave a table permanently unwritable.

    Presets build their grants without provenance, so `apply_preset` stamps the
    caller's marker. Before it did, preset rows were owned by nobody -- and a
    row nobody owns is one autofill will not touch, so an administrator could
    click Read & write as often as they liked and every column stayed read-only.
    """

    @pytest.fixture
    def tables(self):
        from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata

        return [TableMetadata(table_name="orders", schema_name="erp", columns=[
            ColumnMetadata(name="order_id", nullable=False,
                           is_primary_key=True, is_generated=True),
            ColumnMetadata(name="status", data_type="text", nullable=False),
        ])]

    @pytest.fixture
    async def seeded(self, context, tables):
        """A store with a read-only preset already applied."""
        from vanna.core.grants import get_preset, preset_grants

        store = MemoryGrantStore()
        store.load_catalog_facts(tables)
        table_grants, column_grants = preset_grants(
            get_preset("viewer"), tenant_id="acme", data_source_id="wh",
            role="analyst", tables=tables,
        )
        await store.apply_preset(
            context, data_source_id="wh", role="analyst",
            table_grants=table_grants, column_grants=column_grants,
            mode="fill", granted_by="system:preset",
        )
        return store

    async def test_preset_rows_are_marked_machine_generated(self, seeded, context):
        rows = await seeded.list_column_grants(
            context, data_source_id="wh", role="analyst")
        assert rows and all(r.granted_by == "system:preset" for r in rows)
        assert all(r.is_machine_managed for r in rows)

    async def test_read_and_write_afterwards_actually_grants_write(
        self, seeded, context, tables
    ):
        from vanna.core.write import build_write_policy

        await seeded.set_table_grant(context, table_grant(
            "analyst", "erp.orders", can_select=True,
            can_insert=True, can_update=True, can_delete=True))
        await seeded.auto_grant_columns(
            context, data_source_id="wh", role="analyst", table="erp.orders",
            columns=["order_id", "status"], can_write=True,
            unassignable=["order_id"],
        )

        resolved = await seeded.resolve(
            context, data_source_id="wh", roles=["analyst"])
        policy = build_write_policy(
            resolved, tables, dialect="postgres", max_rows=50)

        table = policy.resolve_table("erp.orders")
        assert table is not None, "write verbs with nothing assignable get dropped"
        assert table.can_update is True
        assert table.column("status").can_write is True
        assert table.column("order_id").can_write is False, "generated"
