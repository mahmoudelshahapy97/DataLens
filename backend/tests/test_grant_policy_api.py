"""The preset and workspace-default endpoints.

Route-level tests over stubbed dependencies rather than a live control plane, so
they run everywhere rather than skipping without PostgreSQL -- the same reasoning
``test_grants_api.py`` gives, and for the same reason: these endpoints can fail
before they touch a database at all.

Three properties carry the weight here:

* **Only a workspace admin gets in**, and a refusal is 404 rather than 403, so
  the endpoint does not confirm that the workspace exists.
* **Saving a default is not applying it.** Applying writes grant rows and moves
  the grant version, and moving the version re-authorizes every write that was
  approved but has not run. That must not happen as a side effect of saving a
  dropdown.
* **Read enforcement cannot be switched on into an empty matrix.** Grants have
  governed writes only, so a workspace arriving at this screen has no read
  grants; enabling enforcement first would hide every table from the role.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import FastAPI

from vanna.capabilities.schema_catalog import ColumnMetadata, TableMetadata
from vanna.integrations.local import MemoryGrantStore
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

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
        table_name="customers",
        schema_name="erp",
        columns=[ColumnMetadata(name="customer_id", is_primary_key=True)],
    ),
]


class FakeCatalog:
    async def get_tables(self, context, **kwargs):
        return CATALOG


class FakePolicyStore:
    """The policy table, in a dict. Same surface the Postgres one exposes."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Any] = {}

    async def get(self, tenant_id, data_source_id):
        return {
            role: policy
            for (t, d, role), policy in self.rows.items()
            if (t, d) == (tenant_id, data_source_id)
        }

    async def set(self, tenant_id, data_source_id, policies, *, updated_by=""):
        for policy in policies:
            self.rows[(tenant_id, data_source_id, policy.role)] = policy

    async def mark_applied(self, tenant_id, data_source_id, role, *, version):
        policy = self.rows.get((tenant_id, data_source_id, role))
        if policy is not None:
            policy.last_applied_version = version

    async def pending_for_new_tables(self, tenant_id, data_source_id):
        return [
            p
            for (t, d, _), p in self.rows.items()
            if (t, d) == (tenant_id, data_source_id)
            and p.apply_to_new_tables
            and p.preset != "none"
        ]

    async def enforced_read_roles(self, tenant_id, data_source_id):
        return [
            p.role
            for (t, d, _), p in self.rows.items()
            if (t, d) == (tenant_id, data_source_id) and p.enforce_reads
        ]


def make_user(tenant: str, *, email: str, role: str = "admin") -> Any:
    from vanna.core.user import User

    groups = ["user", role]
    return User(
        id=email, tenant_id=tenant, email=email,
        group_memberships=groups, metadata={"role": role},
    )


class Harness:
    def __init__(self, user: Any, *, admin_emails=()) -> None:
        from vanna_app.routes import grants

        self.user = user
        self.store = MemoryGrantStore()
        self.store.load_catalog_facts(CATALOG)
        self.policies = FakePolicyStore()
        self.audit: List[tuple] = []

        async def runtime_for(tenant_id, *, data_source_id=None):
            assert isinstance(tenant_id, str)
            return SimpleNamespace(
                data_source=f"{tenant_id}-warehouse", catalog=FakeCatalog()
            )

        async def caller(request, **kwargs):
            return self.user

        async def record(action, **kwargs):
            self.audit.append((action, kwargs))

        self.deps = SimpleNamespace(
            settings=SimpleNamespace(is_demo=False, admin_emails=set(admin_emails)),
            platform=SimpleNamespace(grants=self.store, grant_policies=self.policies),
            caller=caller,
            runtime_for=runtime_for,
            agent_memory=DemoAgentMemory(),
            admin_audit=SimpleNamespace(record=record),
        )

        self.app = FastAPI()
        grants.register(self.app, self.deps)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://test"
        )


@pytest.fixture
def harness():
    return Harness(make_user("acme", email="admin@acme.test"))


BASE = "/api/vanna/v2/admin/tenants/acme/grants"


class TestPresets:
    async def test_the_catalogue_is_offered(self, harness):
        async with harness.client() as client:
            body = (await client.get(f"{BASE}/presets")).json()

        names = [p["name"] for p in body["presets"]]
        assert names == ["admin", "analyst", "none", "viewer"]

    async def test_a_viewer_preset_reads_and_filters_but_never_writes(self, harness):
        """Filtering is granted; writing is what separates a viewer from an admin.

        `can_filter` was False here until the AST column check went live and made
        the flag mean something. A preset grants columns rather than withholding
        them, so denying filter denied it on columns the viewer could already read
        in full -- and left a viewer unable to write a WHERE clause.
        """
        async with harness.client() as client:
            body = (await client.get(f"{BASE}/presets")).json()

        viewer = next(p for p in body["presets"] if p["name"] == "viewer")
        assert viewer["column"]["can_read"] is True
        assert viewer["column"]["can_filter"] is True
        assert viewer["column"]["can_write"] is False

    async def test_applying_one_grants_the_catalog(self, harness):
        async with harness.client() as client:
            response = await client.post(
                f"{BASE}/apply-preset",
                json={"role": "analyst", "preset": "analyst"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["tables"] == 2

            listed = (await client.get(f"{BASE}?role=analyst")).json()
        assert {g["table"] for g in listed["tables"]} == {"erp.orders", "erp.customers"}

    async def test_applying_twice_grants_nothing_the_second_time(self, harness):
        async with harness.client() as client:
            await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "analyst"}
            )
            again = await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "analyst"}
            )
        assert again.json()["tables"] == 0

    async def test_only_named_tables_are_granted(self, harness):
        async with harness.client() as client:
            await client.post(
                f"{BASE}/apply-preset",
                json={"role": "analyst", "preset": "analyst",
                      "tables": ["erp.orders"]},
            )
            listed = (await client.get(f"{BASE}?role=analyst")).json()
        assert {g["table"] for g in listed["tables"]} == {"erp.orders"}

    async def test_an_unknown_preset_is_refused(self, harness):
        async with harness.client() as client:
            response = await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "wizard"}
            )
        assert response.status_code == 400

    async def test_an_unknown_role_is_refused(self, harness):
        """A grant for a role nobody holds resolves for nobody and looks fine."""
        async with harness.client() as client:
            response = await client.post(
                f"{BASE}/apply-preset", json={"role": "analsyt", "preset": "analyst"}
            )
        assert response.status_code == 400

    async def test_it_is_audited(self, harness):
        async with harness.client() as client:
            await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "analyst"}
            )
        assert harness.audit[-1][0] == "grants.preset_applied"


class TestTheWorkspaceDefault:
    async def test_saving_records_intent_without_granting(self, harness):
        async with harness.client() as client:
            before = (await client.get(f"{BASE}?role=analyst")).json()["version"]
            response = await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "analyst"}}, "apply": False},
            )
            after = (await client.get(f"{BASE}?role=analyst")).json()

        assert response.status_code == 200, response.text
        assert after["version"] == before, (
            "saving moved the grant version, which re-authorizes pending writes"
        )
        assert after["tables"] == []

    async def test_applying_grants_and_moves_the_version(self, harness):
        async with harness.client() as client:
            before = (await client.get(f"{BASE}?role=analyst")).json()["version"]
            response = await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "analyst"}}, "apply": True},
            )
            after = (await client.get(f"{BASE}?role=analyst")).json()

        assert response.json()["applied"]["analyst"]["tables"] == 2
        assert after["version"] > before
        assert len(after["tables"]) == 2

    async def test_the_saved_default_is_read_back(self, harness):
        async with harness.client() as client:
            await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "viewer",
                                            "apply_to_new_tables": True}}},
            )
            body = (await client.get(f"{BASE}/policy")).json()

        assert body["roles"]["analyst"]["preset"] == "viewer"
        assert body["roles"]["analyst"]["apply_to_new_tables"] is True

    async def test_read_enforcement_needs_something_to_enforce(self, harness):
        """Switching it on against an empty matrix hides every table at once."""
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "none",
                                            "enforce_reads": True}}},
            )
        assert response.status_code == 400
        assert "no readable table" in response.text

    async def test_read_enforcement_is_allowed_alongside_a_preset(self, harness):
        """The preset being applied is what it will enforce against."""
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "analyst",
                                            "enforce_reads": True}},
                      "apply": True},
            )
        assert response.status_code == 200, response.text

    async def test_naming_a_preset_without_applying_it_is_still_a_lockout(
        self, harness
    ):
        """The hole the first version of this guard left open.

        A preset named with `apply: false` writes no grants at all, so the role
        is enforced against an empty matrix and sees nothing -- the exact
        outcome the guard exists to prevent, reached by naming the thing that
        was supposed to prevent it.
        """
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "analyst",
                                            "enforce_reads": True}},
                      "apply": False},
            )
        assert response.status_code == 400, (
            "enforcement was enabled against a preset that was never applied"
        )

    async def test_read_enforcement_is_allowed_once_grants_exist(self, harness):
        async with harness.client() as client:
            await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "analyst"}
            )
            response = await client.put(
                f"{BASE}/policy",
                json={"roles": {"analyst": {"preset": "none",
                                            "enforce_reads": True}}},
            )
        assert response.status_code == 200, response.text

    async def test_it_is_audited(self, harness):
        async with harness.client() as client:
            await client.put(
                f"{BASE}/policy", json={"roles": {"analyst": {"preset": "analyst"}}}
            )
        assert harness.audit[-1][0] == "grants.policy_changed"


class TestRevocation:
    async def test_a_table_grant_can_be_removed(self, harness):
        async with harness.client() as client:
            await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "analyst"}
            )
            response = await client.delete(
                f"{BASE}/table", params={"role": "analyst", "table": "erp.orders"}
            )
            listed = (await client.get(f"{BASE}?role=analyst")).json()

        assert response.json()["deleted"] == 1
        assert {g["table"] for g in listed["tables"]} == {"erp.customers"}

    async def test_removing_something_absent_reports_zero(self, harness):
        async with harness.client() as client:
            response = await client.delete(
                f"{BASE}/table", params={"role": "analyst", "table": "erp.nothing"}
            )
        assert response.json()["deleted"] == 0

    async def test_a_column_grant_can_be_removed(self, harness):
        async with harness.client() as client:
            await client.post(
                f"{BASE}/apply-preset", json={"role": "analyst", "preset": "analyst"}
            )
            response = await client.delete(
                f"{BASE}/column",
                params={"role": "analyst", "table": "erp.orders", "column": "total"},
            )
            listed = (await client.get(f"{BASE}?role=analyst")).json()

        assert response.json()["deleted"] == 1
        assert not any(
            g["table"] == "erp.orders" and g["column"] == "total"
            for g in listed["columns"]
        )


class TestAuthorisation:
    """Only a workspace admin, and a refusal says nothing."""

    @pytest.mark.parametrize("role", ["analyst", "viewer"])
    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("put", "/policy", {"roles": {}}),
            ("post", "/apply-preset", {"role": "analyst", "preset": "analyst"}),
            ("get", "/presets", None),
            ("get", "/policy", None),
        ],
    )
    async def test_a_non_admin_is_refused(self, role, method, path, body):
        harness = Harness(make_user("acme", email="a@acme.test", role=role))
        async with harness.client() as client:
            call = getattr(client, method)
            response = await (
                call(f"{BASE}{path}", json=body) if body is not None
                else call(f"{BASE}{path}")
            )
        assert response.status_code == 404, (
            f"{method} {path} as {role} returned {response.status_code}; a refusal "
            "must not confirm the workspace exists"
        )

    async def test_a_member_of_another_workspace_is_refused(self):
        harness = Harness(make_user("globex", email="b@globex.test", role="admin"))
        async with harness.client() as client:
            response = await client.get(f"{BASE}/policy")
        assert response.status_code == 404

    async def test_a_platform_admin_may_administer_any_workspace(self):
        """The reason these routes do not use the library's group check, which
        would refuse a platform admin working on somebody else's workspace."""
        harness = Harness(
            make_user("globex", email="root@example.com", role="admin"),
            admin_emails={"root@example.com"},
        )
        async with harness.client() as client:
            response = await client.get(f"{BASE}/policy")
        assert response.status_code == 200


class TestWithoutAControlPlane:
    async def test_the_default_endpoints_say_so(self):
        harness = Harness(make_user("acme", email="admin@acme.test"))
        harness.deps.platform.grant_policies = None

        async with harness.client() as client:
            response = await client.get(f"{BASE}/policy")
        assert response.status_code == 503
