"""Grants deciding what may be read, not only what may be written.

The grant model always described a read surface -- ``can_select`` on a table,
``can_read`` on a column -- and nothing consulted it for reads. The only caller of
``GrantStore.resolve`` was ``WriteService``, so the permission matrix an
administrator filled in governed what could be *changed* and said nothing about
what could be *seen*: a viewer with no grants could still ask about every table
the workspace connection reached.

:class:`GrantFilteredCatalog` closes that at the point the prompt is built. The
tests below are about the three ways that could go wrong:

* **Silently doing nothing** -- the wrapper is inert unless a role is enrolled,
  which is exactly what an existing deployment needs and also exactly what a
  broken implementation looks like. Every test that asserts filtering is paired
  with one asserting the unenrolled case still sees everything.
* **Failing open** -- if grants cannot be resolved, or the policy cannot be read,
  the caller must see nothing rather than everything.
* **Locking the system out of itself** -- the scanner and the seeder run on a
  system context. Filtering those would leave the catalog permanently empty, so
  nothing could ever be granted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata
from vanna.core.grants import ColumnGrant, TableGrant
from vanna.integrations.local import MemoryGrantStore

from vanna_app.read_guard import GrantFilteredCatalog, unfiltered

CATALOG = [
    TableMetadata(
        table_name="orders",
        schema_name="erp",
        columns=[
            ColumnMetadata(name="order_id", is_primary_key=True),
            ColumnMetadata(name="total", data_type="numeric"),
        ],
    ),
    TableMetadata(
        table_name="salaries",
        schema_name="erp",
        columns=[
            ColumnMetadata(name="employee_id", is_primary_key=True),
            ColumnMetadata(name="amount", data_type="numeric"),
        ],
    ),
]

RELATIONSHIPS = [
    SimpleNamespace(from_table="erp.orders", to_table="erp.salaries"),
    SimpleNamespace(from_table="erp.orders", to_table="erp.orders"),
]


class FakeCatalog:
    def __init__(self, tables=None, relationships=None) -> None:
        self.tables = CATALOG if tables is None else tables
        self.relationships = RELATIONSHIPS if relationships is None else relationships

    async def get_tables(self, context, **kwargs):
        return list(self.tables)

    async def get_table(self, context, name, **kwargs):
        for table in self.tables:
            if table.table_name == name or f"{table.schema_name}.{table.table_name}" == name:
                return table
        return None

    async def search_tables(self, context, query, **kwargs):
        return list(self.tables)

    async def get_relationships(self, context, **kwargs):
        return list(self.relationships)

    async def upsert_tables(self, context, tables, **kwargs):
        self.tables = list(tables)


@pytest.fixture
def store():
    return MemoryGrantStore()


@pytest.fixture
def analyst(tool_context):
    return tool_context("acme", "ada@acme.example", role="analyst",
                        groups=["user", "analyst"])


@pytest.fixture
def system(tool_context):
    ctx = tool_context("acme", "system", role="admin", groups=["admin"])
    # The scanner and the seeder run as this; `id` is what identifies it.
    ctx.user.id = "system"
    return ctx


def guarded(store, *, roles=("analyst",), inner=None):
    async def enforced():
        return list(roles)

    return GrantFilteredCatalog(
        inner or FakeCatalog(),
        grants=store,
        data_source_id="wh",
        enforced_roles=enforced,
    )


async def grant_orders(store, context, *, columns=("order_id", "total")):
    await store.set_table_grant(
        context,
        TableGrant(
            data_source_id="wh", role="analyst", table="erp.orders", can_select=True
        ),
    )
    for name in columns:
        await store.set_column_grant(
            context,
            ColumnGrant(
                data_source_id="wh",
                role="analyst",
                table="erp.orders",
                column=name,
                can_read=True,
            ),
        )


class TestEnforcementIsOptIn:
    async def test_an_unenrolled_role_sees_everything(self, store, analyst):
        """Grants governed writes only until now. A deployment that has not opted
        in must behave exactly as it did."""
        catalog = guarded(store, roles=())
        assert len(await catalog.get_tables(analyst)) == 2

    async def test_an_enrolled_role_with_no_grants_sees_nothing(self, store, analyst):
        catalog = guarded(store)
        assert await catalog.get_tables(analyst) == []

    async def test_a_role_not_named_in_the_policy_is_unaffected(
        self, store, tool_context
    ):
        viewer = tool_context("acme", "v@acme.example", role="viewer",
                              groups=["user", "viewer"])
        catalog = guarded(store, roles=("analyst",))
        assert len(await catalog.get_tables(viewer)) == 2


class TestFiltering:
    async def test_only_granted_tables_are_visible(self, store, analyst):
        await grant_orders(store, analyst)
        catalog = guarded(store)

        names = [t.table_name for t in await catalog.get_tables(analyst)]
        assert names == ["orders"], "an ungranted table reached the prompt"

    async def test_ungranted_columns_are_dropped_not_masked(self, store, analyst):
        await grant_orders(store, analyst, columns=("order_id",))
        catalog = guarded(store)

        orders = (await catalog.get_tables(analyst))[0]
        assert [c.name for c in orders.columns] == ["order_id"], (
            "a column the caller cannot read was still described to the model"
        )

    async def test_a_table_with_no_readable_column_disappears(self, store, analyst):
        """Not a narrower table -- an unreadable one.

        Describing it would invite a SELECT the policy then refuses for a reason
        the model cannot see from the schema it was given.
        """
        await store.set_table_grant(
            analyst,
            TableGrant(
                data_source_id="wh", role="analyst", table="erp.orders",
                can_select=True,
            ),
        )
        catalog = guarded(store)
        assert await catalog.get_tables(analyst) == []

    async def test_get_table_is_filtered_too(self, store, analyst):
        await grant_orders(store, analyst)
        catalog = guarded(store)

        assert await catalog.get_table(analyst, "erp.orders") is not None
        assert await catalog.get_table(analyst, "erp.salaries") is None, (
            "naming a table directly must not get past the guard"
        )

    async def test_search_is_filtered_too(self, store, analyst):
        """`search_tables` is how the agent explores the catalog."""
        await grant_orders(store, analyst)
        catalog = guarded(store)

        found = await catalog.search_tables(analyst, "salary information")
        assert [t.table_name for t in found] == ["orders"]

    async def test_a_relationship_naming_an_invisible_table_is_hidden(
        self, store, analyst
    ):
        """A join the caller cannot make, which names the table while describing it."""
        await grant_orders(store, analyst)
        catalog = guarded(store)

        edges = await catalog.get_relationships(analyst)
        assert all(
            "salaries" not in (e.from_table + e.to_table) for e in edges
        ), "an ungranted table was named in a relationship"

    async def test_the_prompt_schema_section_is_filtered_too(self, store, analyst):
        """`get_context` builds the schema section of every prompt. It used to
        reach the inner catalog through `__getattr__` and so read it unfiltered:
        the tools hid `salaries` while the system prompt described it."""
        await grant_orders(store, analyst, columns=("order_id",))
        catalog = guarded(store, inner=FakeCatalog(relationships=[]))

        for threshold in (30_000, 0):  # whole-schema path and search path
            ctx = await catalog.get_context(analyst, "salaries", threshold=threshold)
            assert "salaries" not in ctx.text, "an ungranted table reached the prompt"
            assert "total" not in ctx.text, "an ungranted column reached the prompt"
            assert ctx.table_names == ["erp.orders"]

    async def test_the_catalog_hash_sees_only_visible_tables(self, store, analyst):
        await grant_orders(store, analyst)
        filtered = guarded(store, inner=FakeCatalog(relationships=[]))
        everything = guarded(store, roles=(), inner=FakeCatalog(relationships=[]))
        assert await filtered.catalog_hash(analyst) != await everything.catalog_hash(
            analyst
        )

    async def test_the_original_catalog_is_not_mutated(self, store, analyst):
        """Filtering returns copies. Mutating the shared catalog would narrow it
        for every other caller in the workspace."""
        await grant_orders(store, analyst, columns=("order_id",))
        inner = FakeCatalog()
        catalog = guarded(store, inner=inner)

        await catalog.get_tables(analyst)
        assert [c.name for c in inner.tables[0].columns] == ["order_id", "total"]


class TestFailureDirections:
    async def test_a_failing_grant_store_denies_rather_than_allows(
        self, analyst
    ):
        class Broken:
            async def resolve(self, *a, **k):
                raise RuntimeError("control plane is down")

        catalog = guarded(Broken())
        with pytest.raises(RuntimeError):
            await catalog.get_tables(analyst)

    async def test_an_unreadable_policy_leaves_the_catalog_alone(
        self, store, analyst
    ):
        """The policy says *whether* to enforce. Unable to tell, do not enforce.

        The opposite choice turns a blip in the control plane into "every user
        sees nothing", which is an outage rather than a safeguard -- and the
        grants themselves are still checked before any statement runs.
        """
        async def broken():
            raise RuntimeError("policy unavailable")

        catalog = GrantFilteredCatalog(
            FakeCatalog(), grants=store, data_source_id="wh", enforced_roles=broken
        )
        assert len(await catalog.get_tables(analyst)) == 2

    async def test_a_caller_with_no_groups_is_not_enforced(self, store, tool_context):
        anonymous = tool_context("acme", "nobody@acme.example", groups=[])
        catalog = guarded(store)
        assert len(await catalog.get_tables(anonymous)) == 2


class TestTheSystemContext:
    async def test_the_scanner_is_never_filtered(self, store, system):
        """Filtering the system context would leave the catalog permanently empty.

        The scan writes the tables and the seeder reads them back; if the guard
        applied there, nothing would ever be catalogued and so nothing could ever
        be granted -- a lockout with no way out.
        """
        catalog = guarded(store)
        assert len(await catalog.get_tables(system)) == 2


class TestUnfiltered:
    async def test_it_reaches_past_the_guard(self, store, analyst):
        """The admin screens must see every table, including ungranted ones."""
        inner = FakeCatalog()
        catalog = guarded(store, inner=inner)

        assert await catalog.get_tables(analyst) == []
        assert len(await unfiltered(catalog).get_tables(analyst)) == 2

    def test_it_is_a_no_op_on_a_plain_catalog(self):
        plain = FakeCatalog()
        assert unfiltered(plain) is plain


class TestTheGuardGoesOutside:
    """Which catalog the guard wraps, found by running it rather than reasoning.

    The first version wrapped the *physical* catalog and handed that to
    ``SemanticSchemaCatalog`` as its ``physical=`` argument. Every unit test
    passed. Against a real semantic workspace enforcement did nothing at all:
    the semantic catalog answers ``get_tables`` from its manifest and never
    consults the physical one, so the filter was never reached and eleven of
    eleven models stayed visible with a table explicitly revoked.

    These pin the shape that fixed it -- the guard is outermost, so it narrows
    whatever the agent actually reads.
    """

    def test_the_runtime_catalog_is_the_guard_itself(self):
        """`_read_guarded` wraps and returns; it must not be handed inwards."""
        import inspect

        from vanna_app import platform

        source = inspect.getsource(platform.Platform._build_registry)
        assert "SemanticSchemaCatalog(manifest, physical=self.catalog)" in source, (
            "the semantic catalog should sit over the *plain* physical catalog"
        )
        assert "self._read_guarded(\n" in source or "_read_guarded(" in source
        # The guarded call must contain the semantic catalog, not the reverse.
        guarded_first = source.index("_read_guarded")
        semantic_at = source.index("SemanticSchemaCatalog(manifest")
        assert guarded_first < semantic_at, (
            "the guard is inside the semantic catalog again, where it is inert"
        )

    async def test_it_filters_whatever_it_is_given(self, store, analyst):
        """The guard knows nothing about semantics -- it wraps a catalog.

        Modelled on the semantic case: the inner catalog serves objects that
        exist only in a manifest, and the guard narrows them the same way.
        """
        models = [
            TableMetadata(table_name="invoice_lines", schema_name=None,
                          columns=[ColumnMetadata(name="line_revenue")]),
            TableMetadata(table_name="salaries", schema_name=None,
                          columns=[ColumnMetadata(name="amount")]),
        ]
        await store.set_table_grant(
            analyst,
            TableGrant(data_source_id="wh", role="analyst", table="invoice_lines",
                       can_select=True),
        )
        await store.set_column_grant(
            analyst,
            ColumnGrant(data_source_id="wh", role="analyst", table="invoice_lines",
                        column="line_revenue", can_read=True),
        )

        catalog = guarded(store, inner=FakeCatalog(tables=models, relationships=[]))
        assert [t.table_name for t in await catalog.get_tables(analyst)] == [
            "invoice_lines"
        ]

    async def test_a_model_that_exposes_nothing_disappears(self, store, analyst):
        """A pure bridge, whose only columns are hidden join keys.

        Chinook's `playlist_tracks` is one: it vanished under enforcement even
        though nobody revoked it, which read as a bug until the reason was
        clear. It has no column anybody could select, so there is nothing to
        show -- and joins through it still compile, because the semantic
        compiler reads the manifest rather than this catalog.
        """
        bridge = [
            TableMetadata(table_name="playlist_tracks", schema_name=None, columns=[])
        ]
        await store.set_table_grant(
            analyst,
            TableGrant(data_source_id="wh", role="analyst", table="playlist_tracks",
                       can_select=True),
        )

        catalog = guarded(store, inner=FakeCatalog(tables=bridge, relationships=[]))
        assert await catalog.get_tables(analyst) == []
