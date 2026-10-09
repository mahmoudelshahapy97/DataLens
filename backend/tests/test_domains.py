"""Business domains: the store.

A domain is curation -- somebody writing down that ``invoice.total`` is revenue,
that revenue excludes cancelled invoices, and that "churn" means no order in
ninety days. None of that is in the schema, and all of it decides whether a
syntactically perfect query answers the question that was asked.

Two things are pinned here, and they are the two places this kind of feature
usually goes wrong.

**It cannot widen access.** Membership is intersected with the caller's readable
tables, so a domain can narrow what is in play and never add to it. A grouping
that could hand out access would be a second, weaker permission system.

**Curation outlives the things it describes.** Deleting a domain must not delete
the sentences somebody wrote about the tables that were in it.

The other half -- that any of this reaches the model at all -- is in
``test_domain_prompt.py``, which needs no database and asserts on the rendered
prompt rather than on the store.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


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
async def store(app_db, tenants):
    from vanna_app.domain_store import DomainStore

    await tenants()
    return DomainStore(app_db)


async def make_domain(store, tenant="acme", name="Revenue", **kwargs):
    kwargs.setdefault("data_source_id", "db1")
    kwargs.setdefault("description", "Invoices and what they earned.")
    return await store.create(tenant, name=name, **kwargs)


class TestTheStore:
    async def test_create_and_read_back(self, store):
        created = await make_domain(
            store, terminology={"churn": "no invoice in 90 days"}
        )
        assert created["name"] == "Revenue"
        assert created["terminology"] == {"churn": "no invoice in 90 days"}
        assert created["is_enabled"] is True
        assert created["tables"] == []

        listed = await store.list_domains("acme", data_source_id="db1")
        assert [d["name"] for d in listed] == ["Revenue"]

    async def test_membership_is_replaced_not_merged(self, store):
        """The caller is a screen showing the whole list.

        Merging would silently keep a table the administrator had just unticked.
        """
        domain = await make_domain(store)
        await store.replace_tables("acme", domain["id"], ["sales.invoice", "sales.line"])
        await store.replace_tables("acme", domain["id"], ["sales.invoice"])

        again = await store.get_domain("acme", domain["id"])
        assert again["tables"] == ["sales.invoice"]

    async def test_membership_is_stored_normalized(self, store):
        """Same key as the grants and the catalog, so everything joins."""
        domain = await make_domain(store)
        await store.replace_tables("acme", domain["id"], ['"Sales"."Invoice"'])

        again = await store.get_domain("acme", domain["id"])
        assert again["tables"] == ["sales.invoice"]

    async def test_patch_touches_only_what_it_is_given(self, store):
        domain = await make_domain(store, terminology={"churn": "90 days"})
        await store.update("acme", domain["id"], name="Revenue and billing")

        again = await store.get_domain("acme", domain["id"])
        assert again["name"] == "Revenue and billing"
        assert again["description"] == "Invoices and what they earned."
        assert again["terminology"] == {"churn": "90 days"}

    async def test_deleting_a_domain_keeps_the_table_descriptions(self, store, app_db):
        """``table_annotations.domain_id`` is ON DELETE SET NULL.

        Deleting a grouping must not delete the sentences somebody wrote about
        the tables that were in it.
        """
        domain = await make_domain(store)
        await app_db.execute(
            "INSERT INTO vanna_app.table_annotations "
            "(tenant_id, data_source_id, table_key, description, domain_id) "
            "VALUES ('acme','db1','sales.invoice','One row per invoice',%s)",
            (domain["id"],),
        )

        assert await store.delete("acme", domain["id"]) is True

        rows = await app_db.fetch_all(
            "SELECT description, domain_id FROM vanna_app.table_annotations "
            "WHERE tenant_id='acme' AND table_key='sales.invoice'"
        )
        assert rows[0]["description"] == "One row per invoice"
        assert rows[0]["domain_id"] is None

    async def test_a_name_is_unique_case_insensitively(self, store):
        """"Sales" and "sales" are the same domain to everyone except a database."""
        await make_domain(store, name="Sales")
        with pytest.raises(Exception) as caught:
            await make_domain(store, name="sales")
        assert "business_domains_name_idx" in str(caught.value)

    async def test_the_same_name_is_fine_on_another_data_source(self, store):
        await make_domain(store, name="Sales", data_source_id="db1")
        other = await make_domain(store, name="Sales", data_source_id="db2")
        assert other["data_source_id"] == "db2"

    async def test_one_workspace_cannot_see_another(self, store):
        await make_domain(store, tenant="acme")
        assert await store.list_domains("globex") == []

    async def test_deleting_across_workspaces_does_nothing(self, store):
        domain = await make_domain(store, tenant="acme")
        assert await store.delete("globex", domain["id"]) is False
        assert await store.get_domain("acme", domain["id"]) is not None


class TestItCannotWidenAccess:
    async def test_membership_is_intersected_with_what_may_be_read(self, store):
        from vanna_app.domain_store import readable_tables_for

        domain = await make_domain(store)
        await store.replace_tables(
            "acme", domain["id"], ["sales.invoice", "sales.payroll"]
        )

        # The caller may read only one of them.
        allowed = await readable_tables_for(
            store, "acme", domain["id"], readable={"sales.invoice"}
        )
        assert allowed == {"sales.invoice"}

    async def test_without_read_enforcement_membership_stands_alone(self, store):
        """``readable=None`` means this caller is not under enforcement."""
        from vanna_app.domain_store import readable_tables_for

        domain = await make_domain(store)
        await store.replace_tables("acme", domain["id"], ["sales.invoice"])

        assert await readable_tables_for(
            store, "acme", domain["id"], readable=None
        ) == {"sales.invoice"}

    async def test_a_disabled_domain_contributes_nothing(self, store):
        from vanna_app.domain_store import readable_tables_for

        domain = await make_domain(store)
        await store.replace_tables("acme", domain["id"], ["sales.invoice"])
        await store.update("acme", domain["id"], is_enabled=False)

        assert await readable_tables_for(
            store, "acme", domain["id"], readable=None
        ) is None
