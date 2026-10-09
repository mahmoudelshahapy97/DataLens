"""`POST /api/vanna/v2/schema/rescan` files the scan under the workspace's database.

The scanner stamps every table and relationship it finds with the
``data_source_id`` it is given, defaulting to ``"default"``, and the catalog
store honours a record's own value over the caller's context. The route passed
none, so every rescan from the schema screen wrote the whole database under
``"default"`` -- rows no reader looks up -- and the catalog the agent actually
reads never changed. Found by rescanning a workspace's Northwind database and
finding its ten inferred joins under the wrong key.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vanna.capabilities.schema_catalog import ScanReport
from vanna_app.routes import workspace


@pytest.fixture
def scans(monkeypatch):
    calls = []

    class RecordingScanner:
        def __init__(self, runner, *, dialect):
            pass

        async def scan(self, context, catalog, **kwargs):
            calls.append(kwargs)
            return ScanReport(tables_scanned=13, relationships_found=10,
                              relationships_inferred=10)

    monkeypatch.setattr(
        "vanna.capabilities.schema_catalog.SchemaScanner", RecordingScanner
    )
    return calls


def _app(settings) -> TestClient:
    admin = SimpleNamespace(
        id="ada", email="ada@acme.example", tenant_id="acme",
        group_memberships=["admin"], metadata={"role": "admin"},
    )
    runtime = SimpleNamespace(
        runner=object(), dialect="postgres", catalog=object(),
        data_source="postgresql://wh/northwind",
    )

    async def caller(request):
        return admin

    async def runtime_for_request(user, request):
        return runtime

    async def tool_context(user, *, data_source=""):
        return SimpleNamespace(metadata={"data_source_id": data_source})

    async def record(*args, **kwargs):
        return None

    deps = SimpleNamespace(
        settings=settings, directory=None, caller=caller,
        runtime_for_request=runtime_for_request, tool_context=tool_context,
        admin_audit=SimpleNamespace(record=record), client_ip=lambda request: "127.0.0.1",
    )
    app = FastAPI()
    workspace.register(app, deps)
    return TestClient(app)


def test_rescan_names_the_workspace_database(scans, settings, monkeypatch):
    monkeypatch.setattr(workspace, "require_tenant_admin", lambda *a, **k: None)
    response = _app(settings).post("/api/vanna/v2/schema/rescan", json={})

    assert response.status_code == 200
    assert scans == [{"data_source_id": "postgresql://wh/northwind"}]
    assert response.json()["relationships_inferred"] == 10
