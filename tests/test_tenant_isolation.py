"""The property the whole product rests on: workspace A cannot reach workspace B.

Before this file there was no automated proof of it at all. Every guard was correct
by inspection, which is the state a system is in right up until it is not.

The matrix below drives the *real* application -- real routes, real sessions, real
schema -- and asserts, for every route that takes a workspace in its path, that:

* a member of another workspace gets 404,
* a viewer cannot write in their own workspace,
* a tenant admin cannot do the things reserved for a platform admin,
* a platform admin can.

404 rather than 403 throughout, deliberately: a 403 confirms the resource exists to
somebody who has no business knowing that it does.
"""

from __future__ import annotations

from typing import Dict

import httpx
import pytest

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# The application under test
# ----------------------------------------------------------------------


@pytest.fixture
async def app_env(database_url: str, tmp_path_factory) -> Dict[str, str]:
    """A multi-tenant deployment pointed at a throwaway database."""
    import uuid
    from urllib.parse import urlsplit, urlunsplit

    import psycopg2

    name = f"vanna_iso_{uuid.uuid4().hex[:12]}"
    parts = urlsplit(database_url)
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    target = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

    connection = psycopg2.connect(admin_url, connect_timeout=10)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    connection.close()

    data_dir = tmp_path_factory.mktemp("vanna-data")
    yield {
        "VANNA_DEPLOYMENT_MODE": "multi-tenant",
        "VANNA_ADMIN_EMAILS": "root@example.com",
        "VANNA_APP_DATABASE_URL": target,
        "VANNA_SECRET_KEY": "t" * 48,
        "VANNA_SECURE_COOKIES": "true",
        "VANNA_TRUSTED_PROXIES": "127.0.0.0/8",
        "VANNA_CORS_ORIGINS": "http://localhost:3000",
        "VANNA_DEFAULT_TENANT": "acme",
        "VANNA_DATA_DIR": str(data_dir),
        "VANNA_KNOWLEDGE_DIR": str(data_dir / "knowledge"),
        "VANNA_SQLITE_PATH": str(data_dir / "demo.db"),
        "VANNA_DATABASE_URL": "",
        "VANNA_LLM_PROVIDER": "mock",
        "VANNA_SCAN_ON_START": "false",
        "VANNA_METRICS_ENABLED": "false",
        "LOG_LEVEL": "WARNING",
    }

    connection = psycopg2.connect(admin_url, connect_timeout=10)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
    connection.close()


@pytest.fixture
async def world(app_env: Dict[str, str]):
    """The application, two workspaces, and a signed-in client per person."""
    from vanna_app.accounts import Accounts
    from vanna_app.config import load_and_validate
    from vanna_app.db import AppDatabase
    from vanna_app.migrate import upgrade
    from vanna_app.secrets import Cipher
    from vanna_app.tenancy import Directory
    from vanna_app.wiring import create_app

    settings = load_and_validate(app_env)

    db = AppDatabase(settings.app_database_url, minconn=1, maxconn=8, create_if_missing=False)
    upgrade(db)
    directory = Directory(db, Cipher(settings.secret_key))
    accounts = Accounts(db)

    password = "correct-horse-battery-staple"
    people = {}
    for tenant in ("acme", "globex"):
        await directory.create_tenant(tenant, name=tenant.title())
        for role in ("admin", "analyst", "viewer"):
            email = f"{role}@{tenant}.test"
            await accounts.create(email, password, full_name=role.title())
            await directory.add_user(tenant, email, role=role)
            people[f"{tenant}.{role}"] = email
    await accounts.create("root@example.com", password, full_name="Platform")
    await directory.add_user("acme", "root@example.com", role="admin")
    people["platform"] = "root@example.com"
    db.close()

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async def sign_in(email: str, tenant: str) -> httpx.AsyncClient:
        # https, because VANNA_SECURE_COOKIES is true here as it must be in any
        # real deployment -- over http the client would correctly refuse to send
        # the session cookie back and every request would look unauthenticated.
        client = httpx.AsyncClient(
            transport=transport,
            base_url="https://test",
            headers={"X-Tenant-Id": tenant},
        )
        response = await client.post(
            "/api/vanna/v2/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        # The CSRF cookie is issued on the way past; echo it back the way the
        # browser does, or every write in this suite would 403 for the right
        # reason and tell us nothing about authorisation.
        token = client.cookies.get("vanna_csrf")
        if token:
            client.headers["X-CSRF-Token"] = token
        return client

    clients = {}
    for key, email in people.items():
        tenant = "acme" if key in ("platform",) else key.split(".")[0]
        clients[key] = await sign_in(email, tenant)

    yield {"app": app, "clients": clients, "people": people, "settings": settings}

    for client in clients.values():
        await client.aclose()


# ----------------------------------------------------------------------
# The matrix
# ----------------------------------------------------------------------

#: (method, path template) for every route that names a workspace. ``{other}`` is
#: substituted with a workspace the caller does not belong to.
CROSS_TENANT_ROUTES = [
    ("GET", "/api/vanna/v2/admin/tenants/{other}/users"),
    ("POST", "/api/vanna/v2/admin/tenants/{other}/users"),
    ("GET", "/api/vanna/v2/admin/tenants/{other}/starters"),
    ("POST", "/api/vanna/v2/admin/tenants/{other}/starters"),
    ("GET", "/api/vanna/v2/admin/tenants/{other}/usage"),
    ("GET", "/api/vanna/v2/admin/tenants/{other}/billing"),
    ("PATCH", "/api/vanna/v2/admin/tenants/{other}"),
    ("DELETE", "/api/vanna/v2/admin/tenants/{other}"),
    ("GET", "/api/vanna/v2/admin/tenants/{other}/datasources"),
    ("POST", "/api/vanna/v2/admin/tenants/{other}/datasources"),
    ("GET", "/api/vanna/v2/admin/tenants/{other}/domains"),
    ("POST", "/api/vanna/v2/admin/tenants/{other}/domains"),
    ("PATCH", "/api/vanna/v2/admin/tenants/{other}/domains/00000000-0000-0000-0000-000000000000"),
    ("PUT", "/api/vanna/v2/admin/tenants/{other}/domains/00000000-0000-0000-0000-000000000000/tables"),
    ("DELETE", "/api/vanna/v2/admin/tenants/{other}/domains/00000000-0000-0000-0000-000000000000"),
    ("GET", "/api/vanna/v2/admin/tenants/{other}/catalog/tables/public.orders"),
    ("PATCH", "/api/vanna/v2/admin/tenants/{other}/catalog/tables/public.orders"),
    ("PATCH", "/api/vanna/v2/admin/tenants/{other}/catalog/columns/public.orders/total"),
]

BODIES = {
    "POST /api/vanna/v2/admin/tenants/{other}/users": {"email": "x@y.z", "role": "admin"},
    "POST /api/vanna/v2/admin/tenants/{other}/starters": {"question": "hello"},
    "PATCH /api/vanna/v2/admin/tenants/{other}": {"name": "Renamed"},
    "POST /api/vanna/v2/admin/tenants/{other}/datasources": {
        "database_url": "postgresql://u:p@nowhere:5432/db"
    },
    "POST /api/vanna/v2/admin/tenants/{other}/domains": {"name": "Sales"},
    "PATCH /api/vanna/v2/admin/tenants/{other}/domains/"
    "00000000-0000-0000-0000-000000000000": {"name": "Sales"},
    "PUT /api/vanna/v2/admin/tenants/{other}/domains/"
    "00000000-0000-0000-0000-000000000000/tables": {"tables": []},
    "PATCH /api/vanna/v2/admin/tenants/{other}/catalog/tables/public.orders": {
        "description": "Orders placed by customers."
    },
    "PATCH /api/vanna/v2/admin/tenants/{other}/catalog/columns/public.orders/total": {
        "description": "Order total in cents."
    },
}


class TestCrossWorkspaceAccess:
    """The core claim, over every route that takes a workspace in its path."""

    @pytest.mark.parametrize("method,template", CROSS_TENANT_ROUTES)
    @pytest.mark.parametrize("role", ["admin", "analyst", "viewer"])
    async def test_another_workspace_is_not_found(self, world, method, template, role):
        client = world["clients"][f"acme.{role}"]
        path = template.format(other="globex")
        body = BODIES.get(f"{method} {template}")

        response = await client.request(method, path, json=body)
        assert response.status_code == 404, (
            f"{role} of acme reached {method} {path} "
            f"-> {response.status_code} {response.text[:200]}"
        )

    async def test_a_platform_admin_may_reach_another_workspace(self, world):
        response = await world["clients"]["platform"].get(
            "/api/vanna/v2/admin/tenants/globex/users"
        )
        assert response.status_code == 200
        assert {u["email"] for u in response.json()["users"]} == {
            "admin@globex.test", "analyst@globex.test", "viewer@globex.test"
        }

    async def test_a_forged_workspace_header_grants_nothing(self, world):
        """The header selects a workspace; membership is still checked."""
        client = world["clients"]["acme.admin"]
        response = await client.get(
            "/api/vanna/v2/me", headers={"X-Tenant-Id": "globex"}
        )
        assert response.status_code == 403
        assert "not a member" in response.text

    async def test_an_unknown_workspace_is_refused(self, world):
        response = await world["clients"]["acme.admin"].get(
            "/api/vanna/v2/me", headers={"X-Tenant-Id": "does-not-exist"}
        )
        assert response.status_code == 403


class TestPlatformOnlyOperations:
    """Things a workspace admin must not be able to do to their own workspace."""

    async def test_a_tenant_admin_cannot_grant_themselves_a_plan(self, world):
        # This was the live escalation: the route needed only tenant admin, so a
        # workspace admin could grant themselves the enterprise quota for nothing.
        response = await world["clients"]["acme.admin"].post(
            "/api/vanna/v2/admin/tenants/acme/billing/plan",
            json={"plan": "enterprise", "months": 12},
        )
        assert response.status_code == 404

    async def test_a_tenant_admin_cannot_record_a_payment(self, world):
        response = await world["clients"]["acme.admin"].post(
            "/api/vanna/v2/admin/tenants/acme/billing/payments",
            json={"reference": "free-money", "plan": "enterprise", "months": 99},
        )
        assert response.status_code == 404

    async def test_a_tenant_admin_cannot_grant_their_workspace_writes(self, world):
        response = await world["clients"]["acme.admin"].patch(
            "/api/vanna/v2/admin/tenants/acme", json={"allow_writes": True}
        )
        assert response.status_code == 404

    async def test_a_tenant_admin_cannot_repoint_the_datasource(self, world):
        response = await world["clients"]["acme.admin"].patch(
            "/api/vanna/v2/admin/tenants/acme",
            json={"database_url": "postgresql://attacker@evil/db"},
        )
        assert response.status_code == 404

    async def test_a_tenant_admin_may_still_rename_their_workspace(self, world):
        response = await world["clients"]["acme.admin"].patch(
            "/api/vanna/v2/admin/tenants/acme", json={"description": "Our workspace"}
        )
        assert response.status_code == 200

    async def test_a_platform_admin_may_set_a_plan(self, world):
        response = await world["clients"]["platform"].post(
            "/api/vanna/v2/admin/tenants/acme/billing/plan",
            json={"plan": "pro", "months": 1},
        )
        assert response.status_code == 200
        assert response.json()["subscription"]["plan"] == "pro"

    async def test_a_tenant_admin_may_read_their_billing(self, world):
        response = await world["clients"]["acme.admin"].get(
            "/api/vanna/v2/admin/tenants/acme/billing"
        )
        assert response.status_code == 200
        assert response.json()["can_change"] is False

    @pytest.mark.parametrize(
        "path",
        [
            "/api/vanna/v2/admin/tenants",
            "/api/vanna/v2/admin/engines",
            "/api/vanna/v2/admin/datasources",
            "/api/vanna/v2/admin/accounts",
        ],
    )
    async def test_platform_routes_refuse_a_tenant_admin(self, world, path):
        assert (await world["clients"]["acme.admin"].get(path)).status_code == 404
        assert (await world["clients"]["platform"].get(path)).status_code == 200


class TestViewerCannotWrite:
    async def test_a_viewer_cannot_save_a_query(self, world):
        response = await world["clients"]["acme.viewer"].post(
            "/api/vanna/v2/saved-queries", json={"title": "t", "sql": "SELECT 1"}
        )
        assert response.status_code == 403
        assert "viewer" in response.text.lower()

    async def test_an_analyst_can_save_a_query(self, world):
        response = await world["clients"]["acme.analyst"].post(
            "/api/vanna/v2/saved-queries", json={"title": "t", "sql": "SELECT 1"}
        )
        assert response.status_code == 200

    async def test_a_viewer_rating_does_not_write_a_shared_example(self, world):
        """Rating is allowed; contributing to shared knowledge is not.

        A positive rating captures the turn as a candidate example, which is a
        write to the workspace's retrieval set -- so the rating is recorded and the
        capture is skipped, rather than refusing a signal that costs nobody
        anything.
        """
        body = {
            "request_id": "req-viewer",
            "conversation_id": "c1",
            "rating": "positive",
            "question": "Which albums sold the most in 2024?",
            "sql": "SELECT a.title, sum(i.quantity) FROM albums a "
                   "JOIN invoice_items i ON i.album_id = a.id GROUP BY a.title",
        }
        response = await world["clients"]["acme.viewer"].post(
            "/api/vanna/v2/feedback", json=body
        )
        assert response.status_code == 200
        assert response.json()["captured_example"] is False

    async def test_an_analyst_rating_does_capture_one(self, world):
        body = {
            "request_id": "req-analyst",
            "conversation_id": "c1",
            "rating": "positive",
            "question": "Which albums sold the most in 2024?",
            "sql": "SELECT a.title, sum(i.quantity) FROM albums a "
                   "JOIN invoice_items i ON i.album_id = a.id GROUP BY a.title",
        }
        response = await world["clients"]["acme.analyst"].post(
            "/api/vanna/v2/feedback", json=body
        )
        assert response.status_code == 200
        assert response.json()["captured_example"] is True

    async def test_a_viewer_can_still_read(self, world):
        assert (await world["clients"]["acme.viewer"].get(
            "/api/vanna/v2/saved-queries"
        )).status_code == 200


class TestWorkspaceScopedData:
    """Data written in one workspace must not be visible from another."""

    async def test_saved_queries_do_not_cross(self, world):
        await world["clients"]["acme.analyst"].post(
            "/api/vanna/v2/saved-queries",
            json={"title": "acme-secret", "sql": "SELECT 1"},
        )
        mine = (await world["clients"]["acme.analyst"].get(
            "/api/vanna/v2/saved-queries"
        )).json()["saved"]
        theirs = (await world["clients"]["globex.analyst"].get(
            "/api/vanna/v2/saved-queries"
        )).json()["saved"]

        assert any(s["title"] == "acme-secret" for s in mine)
        assert not any(s["title"] == "acme-secret" for s in theirs)

    async def test_a_saved_query_id_from_another_workspace_is_a_404(self, world):
        created = (await world["clients"]["acme.analyst"].post(
            "/api/vanna/v2/saved-queries",
            json={"title": "acme-secret", "sql": "SELECT 1"},
        )).json()["saved"]

        response = await world["clients"]["globex.analyst"].delete(
            f"/api/vanna/v2/saved-queries/{created['id']}"
        )
        assert response.status_code == 404

        # And it is still there afterwards, which is the half a 404 alone does not
        # prove -- the delete could have succeeded and still answered 404.
        mine = (await world["clients"]["acme.analyst"].get(
            "/api/vanna/v2/saved-queries"
        )).json()["saved"]
        assert any(s["id"] == created["id"] for s in mine)

    async def test_the_roster_is_not_public_by_default(self, world):
        anonymous = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=world["app"]), base_url="https://test"
        )
        try:
            response = await anonymous.get("/api/vanna/v2/tenants/acme/users")
            assert response.status_code == 404
        finally:
            await anonymous.aclose()

    async def test_an_unauthenticated_caller_gets_401_not_a_default_identity(self, world):
        """There is no fallback identity. There used to be: `demo@example.com`."""
        anonymous = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=world["app"]),
            base_url="https://test",
            headers={"X-User-Email": "root@example.com", "X-Tenant-Id": "acme"},
        )
        try:
            response = await anonymous.get("/api/vanna/v2/me")
            assert response.status_code == 401
            assert "Sign in" in response.text
        finally:
            await anonymous.aclose()


class TestHistoryIsSharedForReadingOnly:
    """Deleting and rating are the caller's own rows only.

    These two routes take no workspace in their path, so they cannot go in
    ``CROSS_TENANT_ROUTES`` -- the thing that has to be checked is not "another
    workspace's path is a 404" but "another *member's* row is out of reach", and
    every member is handed everybody's row ids by ``GET /history`` on purpose.
    """

    @staticmethod
    async def _seed(world, rows):
        """Insert generations directly. There is no route that creates one."""
        from vanna_app.db import AppDatabase

        db = AppDatabase(
            world["settings"].app_database_url, minconn=1, maxconn=2,
            create_if_missing=False,
        )
        try:
            for identifier, tenant, owner in rows:
                await db.execute(
                    """INSERT INTO vanna_app.generations
                           (id, tenant_id, user_id, question, sql, status, request_id)
                       VALUES (%s, %s, %s, %s, %s, 'valid', %s)""",
                    (identifier, tenant, owner, "how many?", "SELECT 1",
                     f"req-{identifier}"),
                )
        finally:
            db.close()

    @staticmethod
    async def _me(client) -> str:
        return (await client.get("/api/vanna/v2/me")).json()["user"]["id"]

    async def test_a_member_cannot_delete_another_members_row(self, world):
        analyst = world["clients"]["acme.analyst"]
        viewer = world["clients"]["acme.viewer"]
        await self._seed(world, [("row-analyst", "acme", await self._me(analyst))])

        response = await viewer.delete("/api/vanna/v2/history/row-analyst")
        assert response.status_code == 404

        # And it is still there -- a 404 alone does not prove the row survived.
        listed = (await analyst.get("/api/vanna/v2/history")).json()["history"]
        assert any(row["id"] == "row-analyst" for row in listed)

    async def test_clearing_takes_only_the_callers_own_rows(self, world):
        analyst = world["clients"]["acme.analyst"]
        viewer = world["clients"]["acme.viewer"]
        await self._seed(
            world,
            [
                ("row-analyst", "acme", await self._me(analyst)),
                ("row-viewer", "acme", await self._me(viewer)),
            ],
        )

        response = await viewer.delete("/api/vanna/v2/history")
        assert response.status_code == 200
        assert response.json()["deleted"] == 1

        left = {row["id"] for row in (await analyst.get(
            "/api/vanna/v2/history"
        )).json()["history"]}
        assert left == {"row-analyst"}

    async def test_a_viewer_may_forget_their_own_question(self, world):
        """Not a write to the workspace: it is their own question, and forgetting
        it takes nothing from anybody else."""
        viewer = world["clients"]["acme.viewer"]
        await self._seed(world, [("row-viewer", "acme", await self._me(viewer))])

        assert (await viewer.delete(
            "/api/vanna/v2/history/row-viewer"
        )).status_code == 200

    async def test_another_workspaces_row_is_out_of_reach(self, world):
        theirs = world["clients"]["globex.analyst"]
        await self._seed(world, [("row-globex", "globex", await self._me(theirs))])

        response = await world["clients"]["acme.analyst"].delete(
            "/api/vanna/v2/history/row-globex"
        )
        assert response.status_code == 404
        assert len((await theirs.get(
            "/api/vanna/v2/history"
        )).json()["history"]) == 1

    async def test_a_member_cannot_rate_another_members_turn(self, world):
        """A positive rating promotes the turn to a candidate example, so it
        steers what the model is shown for the whole workspace."""
        analyst = world["clients"]["acme.analyst"]
        await self._seed(world, [("row-analyst", "acme", await self._me(analyst))])

        response = await world["clients"]["acme.viewer"].post(
            "/api/vanna/v2/feedback",
            json={"request_id": "req-row-analyst", "rating": "negative",
                  "conversation_id": "c1", "question": "how many?", "sql": "SELECT 1"},
        )
        # The endpoint reports the rating as accepted either way -- it deliberately
        # never fails a turn over feedback -- so the row is what settles it.
        assert response.status_code in (200, 403)
        listed = (await analyst.get("/api/vanna/v2/history")).json()["history"]
        assert next(r for r in listed if r["id"] == "row-analyst")["feedback"] is None


class TestLastAdminGuard:
    async def test_the_last_admin_cannot_be_demoted(self, world):
        users = (await world["clients"]["platform"].get(
            "/api/vanna/v2/admin/tenants/globex/users"
        )).json()["users"]
        admin = next(u for u in users if u["role"] == "admin")

        response = await world["clients"]["platform"].patch(
            f"/api/vanna/v2/admin/tenants/globex/users/{admin['id']}",
            json={"role": "viewer"},
        )
        assert response.status_code == 400
        assert "last admin" in response.text.lower()

    async def test_demotion_works_once_another_admin_exists(self, world):
        client = world["clients"]["platform"]
        await client.post(
            "/api/vanna/v2/admin/tenants/globex/users",
            json={"email": "second@globex.test", "role": "admin"},
        )
        users = (await client.get("/api/vanna/v2/admin/tenants/globex/users")).json()["users"]
        admin = next(u for u in users if u["email"] == "admin@globex.test")

        response = await client.patch(
            f"/api/vanna/v2/admin/tenants/globex/users/{admin['id']}",
            json={"role": "viewer"},
        )
        assert response.status_code == 200


class TestCsrf:
    async def test_a_write_without_the_token_is_refused(self, world):
        client = world["clients"]["acme.analyst"]
        response = await client.post(
            "/api/vanna/v2/saved-queries",
            json={"title": "t", "sql": "SELECT 1"},
            headers={"X-CSRF-Token": ""},
        )
        assert response.status_code == 403
        assert "csrf" in response.text.lower()

    async def test_a_write_with_a_forged_token_is_refused(self, world):
        client = world["clients"]["acme.analyst"]
        response = await client.post(
            "/api/vanna/v2/saved-queries",
            json={"title": "t", "sql": "SELECT 1"},
            headers={"X-CSRF-Token": "forged.token"},
        )
        assert response.status_code == 403

    async def test_reads_need_no_token(self, world):
        client = world["clients"]["acme.analyst"]
        response = await client.get(
            "/api/vanna/v2/saved-queries", headers={"X-CSRF-Token": ""}
        )
        assert response.status_code == 200
