"""The admin grants API: the endpoints the permissions matrix is built on.

Route-level tests over a stubbed ``Deps`` rather than a live control plane, so
they run everywhere rather than skipping without PostgreSQL. That matters here
more than usual: these endpoints shipped with a crash on their first line, and a
test that only runs when someone has a database set up would not have caught it
either.

Two regressions are pinned deliberately:

* ``_data_source`` passed the ``User`` to ``Platform.runtime_for(tenant_id: str)``,
  which keys a dict by it -- and a Pydantic model is unhashable, so every one of
  these endpoints raised ``TypeError`` before doing any work.
* ``_context`` scoped to ``user.tenant_id`` while the route authorized against the
  path's ``{tenant_id}``, so a platform admin administering workspace B would read
  and write grants against their own workspace A.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import FastAPI

from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata
from vanna.integrations.local import MemoryGrantStore
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

CATALOG = [
    TableMetadata(table_name="customers", schema_name="erp", columns=[
        ColumnMetadata(name="customer_id", nullable=False, is_primary_key=True,
                       is_generated=True),
        ColumnMetadata(name="email", data_type="text", nullable=False),
        ColumnMetadata(name="region", data_type="text"),
    ]),
    TableMetadata(table_name="orders", schema_name="erp", columns=[
        ColumnMetadata(name="order_id", nullable=False, is_primary_key=True,
                       is_generated=True),
        ColumnMetadata(name="status", data_type="text", nullable=False),
    ]),
]


class FakeCatalog:
    async def get_tables(self, context, *, data_source_id=None, schema=None):
        return CATALOG


def make_user(tenant: str, *, email: str, role: str = "admin") -> Any:
    from vanna.core.user import User

    return User(
        id=email, tenant_id=tenant, email=email,
        group_memberships=[role], metadata={"role": role},
    )


class Harness:
    """A grants API wired to in-memory stores, plus a record of what it was asked."""

    def __init__(self, user: Any, *, admin_emails=()) -> None:
        from vanna_app.routes import grants

        self.user = user
        self.store = MemoryGrantStore()
        self.store.load_catalog_facts(CATALOG)
        #: Every tenant id `runtime_for` was called with. The type assertion in
        #: `runtime_for` below is what pins the unhashable-User regression.
        self.runtime_calls: List[str] = []
        #: The `data_source_id` each endpoint asked for; None means the
        #: workspace default.
        self.requested_sources: List[Any] = []

        async def runtime_for(tenant_id, *, data_source_id=None):
            # `data_source_id` because a workspace can register several databases
            # and every endpoint here administers one of them. The stub records it
            # so a test can assert which was addressed.
            assert isinstance(tenant_id, str), (
                f"runtime_for takes a tenant id, got {type(tenant_id).__name__}"
            )
            self.requested_sources.append(data_source_id)
            self.runtime_calls.append(tenant_id)
            return SimpleNamespace(
                data_source=data_source_id or f"{tenant_id}-warehouse", catalog=FakeCatalog()
            )

        async def caller(request, **kwargs):
            return self.user

        self.deps = SimpleNamespace(
            settings=SimpleNamespace(is_demo=False, admin_emails=set(admin_emails)),
            platform=SimpleNamespace(grants=self.store),
            caller=caller,
            runtime_for=runtime_for,
            agent_memory=DemoAgentMemory(),
            admin_audit=None,
        )

        self.app = FastAPI()
        grants.register(self.app, self.deps)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://test"
        )

    @staticmethod
    def read_only(table: str, role: str = "analyst") -> Dict[str, Any]:
        return {"role": role, "table": table, "can_read": True}

    @staticmethod
    def read_write(table: str, role: str = "analyst") -> Dict[str, Any]:
        return {
            "role": role, "table": table, "can_read": True,
            "can_insert": True, "can_update": True, "can_delete": True,
        }


@pytest.fixture
def harness():
    return Harness(make_user("acme", email="admin@acme.test"))


class TestTheEndpointsWork:
    """The regression that motivated this file: they used to raise on line one."""

    async def test_listing_grants_succeeds(self, harness):
        async with harness.client() as client:
            response = await client.get("/api/vanna/v2/admin/tenants/acme/grants")
        assert response.status_code == 200, response.text
        assert set(harness.runtime_calls) == {"acme"}

    async def test_setting_a_grant_succeeds(self, harness):
        async with harness.client() as client:
            response = await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/table",
                json=harness.read_only("erp.customers"),
            )
        assert response.status_code == 200, response.text
        assert response.json()["version"] > 0

    async def test_runtime_is_resolved_by_tenant_id_not_by_user(self, harness):
        """`runtime_for` asserts its argument is a str; a User is unhashable."""
        async with harness.client() as client:
            await client.get("/api/vanna/v2/admin/tenants/acme/grants")
        assert harness.runtime_calls and all(
            isinstance(call, str) for call in harness.runtime_calls
        )


class TestTheMatrixPayload:
    async def test_it_lists_every_catalog_table_not_only_granted_ones(self, harness):
        """A matrix has to show what is *not* granted -- that is most of it."""
        async with harness.client() as client:
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        assert [r["table"] for r in body["resources"]] == ["erp.customers", "erp.orders"]
        assert body["tables"] == [], "nothing granted yet"

    async def test_it_marks_keys_and_generated_columns(self, harness):
        """These drive which columns the UI may offer as writable."""
        async with harness.client() as client:
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        columns = {
            c["name"]: c
            for r in body["resources"] if r["table"] == "erp.customers"
            for c in r["columns"]
        }
        assert columns["customer_id"]["is_primary_key"] is True
        assert columns["customer_id"]["is_generated"] is True
        assert columns["email"]["is_generated"] is False

    async def test_it_offers_the_roles_a_grant_may_name(self, harness):
        async with harness.client() as client:
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()
        assert body["roles"] == ["admin", "analyst", "viewer"]


class TestThreeStateRoundTrip:
    """No access -> Read only -> Read & write, as the UI will drive it."""

    async def test_read_only_grants_select_and_no_verbs(self, harness):
        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_only("erp.customers"))
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        grant = next(g for g in body["tables"] if g["table"] == "erp.customers")
        assert grant["can_read"] is True
        assert not any(grant[v] for v in ("can_insert", "can_update", "can_delete"))

    async def test_read_write_grants_all_three_verbs(self, harness):
        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        grant = next(g for g in body["tables"] if g["table"] == "erp.customers")
        assert all(grant[v] for v in
                   ("can_read", "can_insert", "can_update", "can_delete"))

    async def test_write_autofills_columns_but_never_a_generated_one(self, harness):
        """A generated column can never be assigned, so granting it would only
        ever produce a statement the validator refuses."""
        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        columns = {c["column"]: c for c in body["columns"]}
        assert columns["email"]["can_write"] is True
        assert columns["customer_id"]["can_write"] is False

    async def test_a_column_can_be_withheld_from_write_individually(self, harness):
        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            response = await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/column",
                json={"role": "analyst", "table": "erp.customers",
                      "column": "email", "can_read": True, "can_write": False})
            assert response.status_code == 200, response.text
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        columns = {c["column"]: c for c in body["columns"]}
        assert columns["email"]["can_write"] is False
        assert columns["email"]["can_read"] is True
        assert columns["region"]["can_write"] is True, "others are untouched"

    async def test_revoking_returns_a_table_to_no_access(self, harness):
        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json={"role": "analyst", "table": "erp.customers",
                                   "can_read": False})
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        grant = next(g for g in body["tables"] if g["table"] == "erp.customers")
        assert not any(grant[v] for v in
                       ("can_read", "can_insert", "can_update", "can_delete"))

    async def test_every_mutation_moves_the_version(self, harness):
        """The version is what refuses an already-approved write after a revoke."""
        async with harness.client() as client:
            first = (await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/table",
                json=harness.read_only("erp.customers"))).json()["version"]
            second = (await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/table",
                json=harness.read_write("erp.customers"))).json()["version"]
        assert second > first


class TestRefusals:
    async def test_write_without_read_is_a_400_not_a_500(self, harness):
        async with harness.client() as client:
            response = await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/table",
                json={"role": "analyst", "table": "erp.customers",
                      "can_read": False, "can_update": True})
        assert response.status_code == 400
        assert "can_select" in response.text

    async def test_an_unknown_role_is_refused(self, harness):
        """Otherwise the grant is stored, resolves for nobody, and looks fine."""
        async with harness.client() as client:
            response = await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/table",
                json=harness.read_only("erp.customers", role="analsyt"))
        assert response.status_code == 400
        assert "analsyt" in response.text

    async def test_a_non_admin_gets_not_found(self):
        """404 rather than 403: the two must be indistinguishable."""
        harness = Harness(make_user("acme", email="vic@acme.test", role="viewer"))
        async with harness.client() as client:
            response = await client.get("/api/vanna/v2/admin/tenants/acme/grants")
        assert response.status_code == 404

    async def test_an_admin_of_another_workspace_gets_not_found(self):
        harness = Harness(make_user("globex", email="admin@globex.test"))
        async with harness.client() as client:
            response = await client.get("/api/vanna/v2/admin/tenants/acme/grants")
        assert response.status_code == 404

    async def test_without_a_control_plane_it_is_a_503(self, harness):
        harness.deps.platform.grants = None
        async with harness.client() as client:
            response = await client.get("/api/vanna/v2/admin/tenants/acme/grants")
        assert response.status_code == 503


class TestCrossTenantScoping:
    """A platform admin administers the workspace in the path, not their own."""

    @pytest.fixture
    def platform_admin(self):
        return Harness(
            make_user("acme", email="root@example.com"),
            admin_emails={"root@example.com"},
        )

    async def test_grants_land_on_the_targeted_workspace(self, platform_admin):
        async with platform_admin.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/globex/grants/table",
                             json=platform_admin.read_write("erp.orders"))

            theirs = (await client.get(
                "/api/vanna/v2/admin/tenants/globex/grants")).json()
            mine = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        assert [g["table"] for g in theirs["tables"]] == ["erp.orders"]
        assert mine["tables"] == [], "the caller's own workspace must be untouched"

    async def test_the_data_source_follows_the_targeted_workspace(self, platform_admin):
        async with platform_admin.client() as client:
            await client.get("/api/vanna/v2/admin/tenants/globex/grants")
        assert set(platform_admin.runtime_calls) == {"globex"}, (
            "the caller's own workspace must never be resolved here"
        )


class TestAdministeringASecondDatabase:
    """A workspace can register several databases, and each has its own matrix.

    The grant tables have always been keyed
    ``(tenant_id, data_source_id, role, table_key)``. What was missing was any way
    to *name* a data source from these endpoints, so every one of them silently
    administered the workspace default and the second database's matrix could not
    be reached at all.
    """

    async def test_the_default_database_is_used_when_none_is_named(self, harness):
        async with harness.client() as client:
            await client.get("/api/vanna/v2/admin/tenants/acme/grants?role=analyst")

        # Two resolutions per request: the grants, then the catalog behind
        # `resources`. The first asks for None (no preference) and the second
        # for the concrete id the first resolved to -- which is the point, so
        # the two cannot end up describing different databases.
        assert set(harness.requested_sources) - {None} == {"acme-warehouse"}

    async def test_a_named_database_is_the_one_administered(self, harness):
        async with harness.client() as client:
            await client.get("/api/vanna/v2/admin/tenants/acme/grants"
                "?role=analyst&data_source_id=warehouse-two")

        assert set(harness.requested_sources) - {None} == {"warehouse-two"}

    async def test_a_write_is_recorded_against_the_named_database(self, harness):
        async with harness.client() as client:
            response = await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/table"
                "?data_source_id=warehouse-two",
                json={"role": "analyst", "table": "erp.orders", "can_read": True},
            )
            assert response.status_code == 200, response.text

        assert set(harness.requested_sources) - {None} == {"warehouse-two"}


class TestGrantsReachTheSqlGenerator:
    """The assertion that makes the UI meaningful rather than decorative."""

    async def test_a_read_write_grant_produces_a_writable_policy(self, harness):
        from vanna.core.write import build_write_policy

        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))

        context = SimpleNamespace(tenant_id="acme")
        resolved = await harness.store.resolve(
            context, data_source_id="acme-warehouse", roles=["analyst"])
        policy = build_write_policy(
            resolved, CATALOG, dialect="postgres", max_rows=50)

        table = policy.resolve_table("erp.customers")
        assert table is not None
        assert table.can_insert and table.can_update and table.can_delete
        assert table.column("email").can_write is True
        assert table.column("customer_id").can_write is False, "generated"

    async def test_a_read_only_grant_produces_no_writable_table(self, harness):
        from vanna.core.write import build_write_policy

        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_only("erp.customers"))

        context = SimpleNamespace(tenant_id="acme")
        resolved = await harness.store.resolve(
            context, data_source_id="acme-warehouse", roles=["analyst"])
        policy = build_write_policy(
            resolved, CATALOG, dialect="postgres", max_rows=50)
        assert policy.is_empty

    async def test_withholding_a_column_removes_it_from_assignable(self, harness):
        from vanna.core.write import build_write_policy

        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/column",
                json={"role": "analyst", "table": "erp.customers",
                      "column": "email", "can_read": True, "can_write": False})

        context = SimpleNamespace(tenant_id="acme")
        resolved = await harness.store.resolve(
            context, data_source_id="acme-warehouse", roles=["analyst"])
        policy = build_write_policy(
            resolved, CATALOG, dialect="postgres", max_rows=50)

        table = policy.resolve_table("erp.customers")
        # Dropped outright rather than kept with can_write=False: the write
        # policy carries only what can be assigned or can address a row, so a
        # withheld column becomes unnameable rather than merely refused.
        assert table.column("email") is None
        assert table.column("region").can_write is True


class TestCyclingThroughReadToWrite:
    """The path the UI actually takes: No access -> Read only -> Read & write.

    This is where the feature was broken. The read step autofills column rows
    with ``can_write=False``; a fill-gaps-only autofill then left them that way,
    so the table ended up carrying write verbs with nothing assignable -- and
    ``build_write_policy`` resolves that by dropping the verbs. "Read & write"
    granted nothing at all, silently.
    """

    async def test_write_survives_the_pass_through_read_only(self, harness):
        from vanna.core.write import build_write_policy

        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_only("erp.customers"))
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        columns = {c["column"]: c for c in body["columns"]}
        assert columns["email"]["can_write"] is True, (
            "autofill must bring its own rows along when the table becomes writable"
        )

        resolved = await harness.store.resolve(
            SimpleNamespace(tenant_id="acme"),
            data_source_id="acme-warehouse", roles=["analyst"])
        policy = build_write_policy(
            resolved, CATALOG, dialect="postgres", max_rows=50)
        table = policy.resolve_table("erp.customers")
        assert table is not None and table.can_update, (
            "a table with no assignable column loses its write verbs"
        )

    async def test_a_deliberate_column_choice_outlives_a_table_change(self, harness):
        """Autofill owns the rows it created and nothing else."""
        async with harness.client() as client:
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            await client.put(
                "/api/vanna/v2/admin/tenants/acme/grants/column",
                json={"role": "analyst", "table": "erp.customers",
                      "column": "email", "can_read": True, "can_write": False})
            # Cycle the table away and back.
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_only("erp.customers"))
            await client.put("/api/vanna/v2/admin/tenants/acme/grants/table",
                             json=harness.read_write("erp.customers"))
            body = (await client.get(
                "/api/vanna/v2/admin/tenants/acme/grants")).json()

        columns = {c["column"]: c for c in body["columns"]}
        assert columns["email"]["can_write"] is False, (
            "a withheld column must not be re-granted by a later table change"
        )
        assert columns["region"]["can_write"] is True, "autofill's own rows follow"
