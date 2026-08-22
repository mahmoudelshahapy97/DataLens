"""The application boots, refuses what it should, and answers its probes.

Thin on purpose: the behaviour lives in the other files. What this covers is the
*wiring* -- that everything constructed in ``wiring.create_app`` fits together, and
that the endpoints an orchestrator and a load balancer depend on actually work.

The ``/ready`` test earns its place. It returned 422 for a while: this module has
``from __future__ import annotations``, so FastAPI resolved a ``response: Response``
parameter against the module namespace, did not find the function-local import, and
treated it as a required query parameter. The probe therefore reported a client
error instead of running its checks -- and nothing else would have noticed, because
the only consumer is a load balancer that would have marked every replica unhealthy.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
async def app(database_url: str, tmp_path):
    from urllib.parse import urlsplit, urlunsplit

    import psycopg2

    from vanna_app.config import load_and_validate
    from vanna_app.wiring import create_app

    name = f"vanna_smoke_{uuid.uuid4().hex[:10]}"
    parts = urlsplit(database_url)
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    target = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

    connection = psycopg2.connect(admin_url, connect_timeout=10)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    connection.close()

    settings = load_and_validate({
        "VANNA_DEPLOYMENT_MODE": "multi-tenant",
        "VANNA_ADMIN_EMAILS": "root@example.com",
        "VANNA_APP_DATABASE_URL": target,
        "VANNA_SECRET_KEY": "s" * 48,
        "VANNA_SECURE_COOKIES": "true",
        "VANNA_TRUSTED_PROXIES": "127.0.0.0/8",
        "VANNA_CORS_ORIGINS": "https://app.example.com",
        "VANNA_DATA_DIR": str(tmp_path),
        "VANNA_KNOWLEDGE_DIR": str(tmp_path / "knowledge"),
        "VANNA_SQLITE_PATH": str(tmp_path / "demo.db"),
        "VANNA_DATABASE_URL": "",
        "VANNA_LLM_PROVIDER": "mock",
        "VANNA_SCAN_ON_START": "false",
        "VANNA_METRICS_ENABLED": "false",
        "LOG_LEVEL": "ERROR",
    })
    yield create_app(settings)

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
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as session:
        yield session


class TestProbes:
    async def test_health_is_liveness_and_touches_nothing(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_ready_checks_the_control_plane(self, client):
        response = await client.get("/ready")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"] == {"control_plane": True, "migrations": True}

    async def test_ready_reports_not_ready_when_the_control_plane_is_gone(
        self, app, client
    ):
        """A replica that cannot sign anybody in must not receive traffic.

        The distinction between the two probes is the whole point: with only one,
        an orchestrator either restarts a pod over a slow database (if the probe
        checks it) or routes users to a pod that cannot authenticate them (if it
        does not).
        """
        # Closing the pool is what a database outage looks like from inside the
        # process. Reached through the closure the probe itself uses, so this
        # cannot pass against a different database than the one under test.
        services = app.state.services
        services["db"].close()

        response = await client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not-ready"
        assert body["checks"]["control_plane"] is False

        # Liveness is unaffected, which is the property that stops the restart loop.
        assert (await client.get("/health")).status_code == 200


class TestUnauthenticated:
    async def test_there_is_no_default_identity(self, client):
        """The original returned `demo@example.com` when nothing was presented."""
        response = await client.get("/api/vanna/v2/me")
        assert response.status_code == 401

    async def test_a_claimed_header_grants_nothing(self, client):
        response = await client.get(
            "/api/vanna/v2/me", headers={"X-User-Email": "root@example.com"}
        )
        assert response.status_code == 401

    async def test_a_failed_login_says_nothing_useful(self, client):
        unknown = await client.post(
            "/api/vanna/v2/auth/login",
            json={"email": "nobody@example.com", "password": "x"},
        )
        assert unknown.status_code == 401
        assert unknown.json()["detail"] == "Those credentials are not valid."

    async def test_the_public_workspace_list_carries_names_only(self, client):
        response = await client.get("/api/vanna/v2/tenants")
        assert response.status_code == 200
        for tenant in response.json()["tenants"]:
            assert set(tenant) == {"id", "name"}

    async def test_auth_methods_describes_the_deployment(self, client):
        body = (await client.get("/api/vanna/v2/auth/methods")).json()
        assert body["password"] is True
        assert body["oidc"] is False
        # No SMTP configured, so the reset link would lead nowhere and is not
        # offered -- a dead link is worse than no link.
        assert body["can_reset"] is False


class TestCsrf:
    async def test_a_write_is_refused_without_a_token(self, client):
        response = await client.post(
            "/api/vanna/v2/saved-queries", json={"title": "t", "sql": "SELECT 1"}
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "csrf_failed"

    async def test_the_token_cookie_is_issued_on_the_first_request(self, client):
        await client.get("/health")
        assert client.cookies.get("vanna_csrf")


class TestObservability:
    async def test_every_response_carries_a_request_id(self, client):
        response = await client.get("/health")
        assert response.headers.get("x-request-id")

    async def test_an_upstream_request_id_is_adopted(self, client):
        response = await client.get(
            "/health", headers={"X-Request-Id": "abcdef0123456789"}
        )
        assert response.headers["x-request-id"] == "abcdef0123456789"

    async def test_an_absurd_upstream_id_is_replaced(self, client):
        # An unbounded attacker-controlled string would otherwise end up in every
        # log line for the request.
        response = await client.get("/health", headers={"X-Request-Id": "x" * 500})
        assert len(response.headers["x-request-id"]) <= 64
        assert response.headers["x-request-id"] != "x" * 500
