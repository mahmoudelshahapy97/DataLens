"""The schema catalog in the control plane, against a real database.

The catalog used to be one JSON file, and moving it to Postgres is mostly about
things a file cannot do -- row-level tenant isolation, two replicas, an
administrator who wants to query it. But one bug motivated the *shape* of the
schema rather than the move itself, and it is the first thing pinned here.

``upsert_tables`` replaces a whole ``TableMetadata``. With a description stored on
the same row, every routine re-scan silently destroys whatever a human wrote about
the table -- and nobody notices, because the scan reports success and the prompt
merely gets a little worse. ``capabilities/schema_catalog/dbt.py`` already
documents the same hazard on its merge path ("dbt owns meaning, the scan owns
structure"), which is where the split used here comes from: structure and meaning
are different tables, and a scan only ever writes to one of them.

:class:`TestARescanCannotDestroyCuration` is that guarantee. The rest covers what
the ABC promises -- tenant scoping, SQL identifier semantics, and the lifecycle
rules that let a table disappear without taking its annotations with it.
"""

from __future__ import annotations

import pytest

from vanna.capabilities.schema_catalog import (
    CatalogStatus,
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    TableMetadata,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def tenants(app_db):
    """Two workspaces, because the isolation tests need somewhere to leak to."""

    async def make() -> None:
        for tenant in ("acme", "globex"):
            await app_db.execute(
                "INSERT INTO vanna_app.tenants (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (tenant, tenant.title()),
            )

    return make


@pytest.fixture
async def catalog(app_db, tenants):
    from vanna_app.catalog_store import PostgresSchemaCatalog

    await tenants()
    return PostgresSchemaCatalog(app_db)


@pytest.fixture
def acme(tool_context):
    return tool_context("acme", "ada@acme.example")


@pytest.fixture
def globex(tool_context):
    return tool_context("globex", "bob@globex.example")


def column(name: str, **kwargs) -> ColumnMetadata:
    return ColumnMetadata(name=name, **kwargs)


def table(name: str, *columns: ColumnMetadata, **kwargs) -> TableMetadata:
    kwargs.setdefault("schema_name", "sales")
    kwargs.setdefault("data_source_id", "db1")
    return TableMetadata(table_name=name, columns=list(columns), **kwargs)


async def annotate_table(app_db, tenant, table_key, description):
    await app_db.execute(
        "INSERT INTO vanna_app.table_annotations "
        "(tenant_id, data_source_id, table_key, description) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (tenant_id, data_source_id, table_key) "
        "DO UPDATE SET description = EXCLUDED.description",
        (tenant, "db1", table_key, description),
    )


class TestRoundTrip:
    async def test_tables_and_columns_survive_a_write_and_a_read(self, catalog, acme):
        await catalog.upsert_tables(
            acme,
            [
                table(
                    "orders",
                    column("id", is_primary_key=True, data_type="int"),
                    column("status", low_cardinality=True, categories=["A", "C"]),
                    column(
                        "customer_id",
                        foreign_key=ForeignKey(
                            column="customer_id",
                            references_table="customers",
                            references_column="id",
                        ),
                    ),
                    row_count_estimate=42,
                )
            ],
        )

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders is not None
        assert orders.row_count_estimate == 42
        # Column order is the scanner's order, not the database's whim: it is what
        # the prompt renders, and a shuffled column list reads as a different table.
        assert [c.name for c in orders.columns] == ["id", "status", "customer_id"]
        assert orders.columns[0].is_primary_key
        assert orders.columns[1].categories == ["A", "C"]
        assert orders.columns[2].foreign_key.references_table == "customers"

    async def test_relationships_round_trip(self, catalog, acme):
        await catalog.upsert_tables(acme, [table("orders", column("customer_id"))])
        await catalog.upsert_relationships(
            acme,
            [
                RelationshipMetadata(
                    name="orders_customer",
                    from_table="sales.orders",
                    from_column="customer_id",
                    to_table="sales.customers",
                    to_column="id",
                    data_source_id="db1",
                )
            ],
        )

        found = await catalog.get_relationships(acme, data_source_id="db1")
        assert len(found) == 1
        assert found[0].join_type == "many_to_one"

    async def test_a_failed_scan_is_recorded_as_failed(self, catalog, acme):
        """A table the scanner could not profile arrives with no columns.

        Flattening that to ``scanned`` would present a successfully scanned table
        that happens to have no columns -- and grant resolution drops zero-column
        tables, so the failure would become a silent disappearance instead of
        something an operator can see.
        """
        await catalog.upsert_tables(
            acme,
            [
                table(
                    "locked",
                    status=CatalogStatus.FAILED,
                    error_message="permission denied",
                )
            ],
        )

        locked = await catalog.get_table(acme, "sales.locked", data_source_id="db1")
        assert locked.status == CatalogStatus.FAILED
        assert locked.error_message == "permission denied"


class TestARescanCannotDestroyCuration:
    """The reason structure and meaning are separate tables."""

    async def test_a_description_survives_a_rescan(self, catalog, acme, app_db):
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await annotate_table(app_db, "acme", "sales.orders", "One row per order")

        # Exactly what a nightly scan does: the same tables, written again.
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders.description == "One row per order"

    async def test_a_column_description_survives_a_rescan(self, catalog, acme, app_db):
        await catalog.upsert_tables(acme, [table("orders", column("status"))])
        await app_db.execute(
            "INSERT INTO vanna_app.column_annotations "
            "(tenant_id, data_source_id, table_key, column_key, description) "
            "VALUES ('acme','db1','sales.orders','status','Order state')"
        )

        await catalog.upsert_tables(acme, [table("orders", column("status"))])

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders.get_column("status").description == "Order state"

    async def test_coded_values_reach_the_prompt(self, catalog, acme, app_db):
        """``ColumnMetadata`` has no field for a code book, so it joins the description.

        Without it the model guesses the literal, and a wrong literal returns zero
        rows rather than an error anybody can act on.
        """
        await catalog.upsert_tables(acme, [table("orders", column("status"))])
        await app_db.execute(
            "INSERT INTO vanna_app.column_annotations "
            "(tenant_id, data_source_id, table_key, column_key, description, value_labels) "
            "VALUES ('acme','db1','sales.orders','status','Order state',"
            "'{\"A\": \"Active\"}'::jsonb)"
        )

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        described = orders.get_column("status").description
        assert "Order state" in described
        assert "A = Active" in described

    async def test_an_annotation_outlives_the_table_it_describes(
        self, catalog, acme, app_db
    ):
        """A table can vanish for reasons that are not permanent.

        A failed deploy, a rename in progress, a scan that ran against a replica
        mid-migration. Deleting the annotation would make the outage permanent for
        whoever wrote it.
        """
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await annotate_table(app_db, "acme", "sales.orders", "One row per order")

        # A scan that no longer sees it.
        await catalog.upsert_tables(acme, [table("customers", column("id"))])
        assert await catalog.get_table(acme, "sales.orders", data_source_id="db1") is None

        # ...and it comes back with its description intact.
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders.description == "One row per order"


class TestLifecycle:
    async def test_a_vanished_table_stops_being_returned(self, catalog, acme):
        await catalog.upsert_tables(
            acme, [table("orders", column("id")), table("customers", column("id"))]
        )
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        names = {t.table_name for t in await catalog.get_tables(acme)}
        assert names == {"orders"}

    async def test_a_dropped_column_stops_being_returned(self, catalog, acme):
        await catalog.upsert_tables(
            acme, [table("orders", column("id"), column("secret_note"))]
        )
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert [c.name for c in orders.columns] == ["id"]

    async def test_a_failed_table_does_not_retire_the_columns_we_knew(
        self, catalog, acme
    ):
        """A FAILED table arrives with no columns because nothing could be read.

        Treating that as "the table now has no columns" would drop it from every
        prompt and every grant -- turning a transient permission error on one table
        into its disappearance.
        """
        await catalog.upsert_tables(acme, [table("orders", column("id"), column("total"))])
        await catalog.upsert_tables(
            acme, [table("orders", status=CatalogStatus.FAILED, error_message="denied")]
        )

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert [c.name for c in orders.columns] == ["id", "total"]

    async def test_an_empty_upsert_is_a_no_op(self, catalog, acme):
        """A scan that failed outright must not blank a working catalog."""
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await catalog.upsert_tables(acme, [])

        assert len(await catalog.get_tables(acme)) == 1

    async def test_clear_removes_structure_but_not_curation(self, catalog, acme, app_db):
        """``clear`` runs when a workspace is repointed at a different database.

        The structure describes the old one and is wrong, not stale. The
        annotations belong to the people who wrote them and are keyed by name, so
        they still apply if the workspace is pointed back.
        """
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await annotate_table(app_db, "acme", "sales.orders", "One row per order")

        assert await catalog.clear(acme, data_source_id="db1") == 1
        assert await catalog.get_tables(acme) == []

        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders.description == "One row per order"


class TestTenantScoping:
    """The ABC calls this a hard contract: a catalog that ignores it leaks one
    tenant's table names and column descriptions into another tenant's prompt."""

    async def test_one_workspace_cannot_see_another(self, catalog, acme, globex):
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        assert await catalog.get_tables(globex) == []
        assert await catalog.get_table(globex, "sales.orders") is None
        assert await catalog.search_tables(globex, "orders") == []

    async def test_the_tenant_is_taken_from_the_context_not_the_record(
        self, catalog, acme
    ):
        """A scanner or an import could otherwise write into another tenant."""
        smuggled = table("orders", column("id"))
        smuggled.tenant_id = "globex"

        await catalog.upsert_tables(acme, [smuggled])

        rows = await catalog.db.fetch_all(
            "SELECT tenant_id FROM vanna_app.catalog_tables"
        )
        assert {r["tenant_id"] for r in rows} == {"acme"}

    async def test_clear_only_clears_the_calling_tenant(self, catalog, acme, globex):
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await catalog.upsert_tables(globex, [table("orders", column("id"))])

        await catalog.clear(acme)

        assert await catalog.get_tables(acme) == []
        assert len(await catalog.get_tables(globex)) == 1


class TestLookupSemantics:
    async def test_a_table_is_found_qualified_or_not(self, catalog, acme):
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        assert await catalog.get_table(acme, "sales.orders") is not None
        assert await catalog.get_table(acme, "orders") is not None

    async def test_lookup_is_case_insensitive(self, catalog, acme):
        """The ABC asks for SQL identifier semantics on unquoted names."""
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        assert await catalog.get_table(acme, "SALES.ORDERS") is not None
        assert await catalog.get_table(acme, "Orders") is not None

    async def test_a_missing_table_is_none_not_an_error(self, catalog, acme):
        assert await catalog.get_table(acme, "sales.nope") is None

    async def test_data_sources_do_not_bleed_into_each_other(self, catalog, acme):
        """A workspace with two databases must not merge their schemas."""
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await catalog.upsert_tables(
            acme, [table("invoices", column("id"), data_source_id="db2")]
        )

        db1 = {t.table_name for t in await catalog.get_tables(acme, data_source_id="db1")}
        db2 = {t.table_name for t in await catalog.get_tables(acme, data_source_id="db2")}
        assert db1 == {"orders"}
        assert db2 == {"invoices"}

    async def test_search_never_returns_nothing_for_a_non_empty_catalog(
        self, catalog, acme
    ):
        """An empty schema section guarantees a hallucinated table name."""
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        assert await catalog.search_tables(acme, "zzzz-no-such-thing") != []


class TestWritingAnAnnotation:
    """The write side of curation, which had no caller until now.

    Both descriptions already reached the model's prompt and ``value_labels`` is
    what stops it inventing a literal -- but there was no endpoint and no editor,
    so the only way to set any of it was raw SQL.
    """

    async def test_a_description_round_trips(self, catalog, acme):
        await catalog.upsert_tables(acme, [table("orders", column("id"))])

        await catalog.annotate_table(
            "acme", "db1", "sales.orders",
            description="One row per order", updated_by="ada@acme.example",
        )

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders.description == "One row per order"

    async def test_editing_the_description_leaves_the_display_name_alone(self, catalog, acme):
        """``coalesce`` rather than SET: a screen that edits one field must not
        blank a field it never showed."""
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await catalog.annotate_table(
            "acme", "db1", "sales.orders", display_name="Orders", description="First"
        )

        await catalog.annotate_table("acme", "db1", "sales.orders", description="Second")

        stored = await catalog.get_table_annotation("acme", "db1", "sales.orders")
        assert stored["description"] == "Second"
        assert stored["display_name"] == "Orders"

    async def test_a_description_can_be_cleared(self, catalog, acme):
        """An empty string means "clear it"; ``None`` means "leave it"."""
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await catalog.annotate_table("acme", "db1", "sales.orders", description="Wrong")

        await catalog.annotate_table("acme", "db1", "sales.orders", description="")

        stored = await catalog.get_table_annotation("acme", "db1", "sales.orders")
        assert stored["description"] == ""

    async def test_a_code_book_reaches_the_prompt(self, catalog, acme):
        await catalog.upsert_tables(acme, [table("orders", column("status"))])

        await catalog.annotate_column(
            "acme", "db1", "sales.orders", "status",
            description="Order state", value_labels={"A": "Active", "C": "Cancelled"},
        )

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        rendered = orders.get_column("status").description
        assert "A = Active" in rendered and "C = Cancelled" in rendered

    async def test_a_code_book_is_replaced_not_merged(self, catalog, acme):
        """It is edited as a set; merging would make removing a code impossible."""
        await catalog.upsert_tables(acme, [table("orders", column("status"))])
        await catalog.annotate_column(
            "acme", "db1", "sales.orders", "status", value_labels={"A": "Active", "X": "Typo"}
        )

        await catalog.annotate_column(
            "acme", "db1", "sales.orders", "status", value_labels={"A": "Active"}
        )

        stored = await catalog.get_column_annotation(
            "acme", "db1", "sales.orders", "status"
        )
        assert stored["value_labels"] == {"A": "Active"}

    async def test_an_annotation_survives_the_next_rescan(self, catalog, acme):
        await catalog.upsert_tables(acme, [table("orders", column("status"))])
        await catalog.annotate_column(
            "acme", "db1", "sales.orders", "status", description="Order state"
        )

        await catalog.upsert_tables(acme, [table("orders", column("status"))])

        orders = await catalog.get_table(acme, "sales.orders", data_source_id="db1")
        assert orders.get_column("status").description == "Order state"

    async def test_it_does_not_reach_another_workspace(self, catalog, acme, globex):
        await catalog.upsert_tables(acme, [table("orders", column("id"))])
        await catalog.upsert_tables(globex, [table("orders", column("id"))])

        await catalog.annotate_table("acme", "db1", "sales.orders", description="Ours")

        theirs = await catalog.get_table(globex, "sales.orders", data_source_id="db1")
        assert theirs.description is None

    async def test_existence_checks_are_scoped_to_the_workspace(self, catalog, acme):
        """What the routes use to refuse a typo, since there is no foreign key --
        an annotation deliberately outlives the structural row it describes."""
        await catalog.upsert_tables(acme, [table("orders", column("status"))])

        assert await catalog.table_exists("acme", "db1", "sales.orders") is True
        assert await catalog.table_exists("globex", "db1", "sales.orders") is False
        assert await catalog.table_exists("acme", "db2", "sales.orders") is False
        assert await catalog.column_exists("acme", "db1", "sales.orders", "status") is True
        assert await catalog.column_exists("acme", "db1", "sales.orders", "nope") is False
