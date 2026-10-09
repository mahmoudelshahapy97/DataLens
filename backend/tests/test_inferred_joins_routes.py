"""Review routes for inferred joins: they follow the database on screen.

Inferred joins exist only where foreign keys are not declared, which is rarely a
workspace's default database -- in the demo workspace it is Northwind, beside a
Chinook default. Resolving the default (as the annotation routes do) showed an
empty panel for exactly the database that needed reviewing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vanna_app.datasources import UnknownDataSource
from vanna_app.routes import catalog as catalog_routes

BASE = "/api/vanna/v2/admin/tenants/demo/catalog/relationships"
REGISTERED = {None: "postgresql://wh/chinook", "postgresql://wh/northwind": "postgresql://wh/northwind"}


class Store:
    def __init__(self):
        self.reviews = []

    def annotate_table(self):  # presence marks the control-plane store
        raise NotImplementedError

    async def list_inferred_relationships(self, tenant_id, data_source_id):
        if data_source_id != "postgresql://wh/northwind":
            return []
        return [{
            "from_table_key": "northwind.salesorder", "from_column_key": "custid",
            "to_table_key": "northwind.customer", "to_column_key": "custid",
            "join_type": "many_to_one", "confidence": 0.95,
            "review_status": "proposed", "reviewed_by": None,
            "reviewed_at": None, "lifecycle_status": "present",
        }]

    async def review_relationship(self, tenant_id, data_source_id, **kwargs):
        self.reviews.append((tenant_id, data_source_id, kwargs))
        return kwargs["from_column"] == "custid"


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(catalog_routes, "require_tenant_admin", lambda *a, **k: None)
    store = Store()

    async def caller(request):
        return SimpleNamespace(id="ada", email="ada@acme.example", tenant_id="demo")

    async def runtime_for(tenant_id, *, data_source_id=None):
        if data_source_id not in REGISTERED:
            raise UnknownDataSource(f"{data_source_id} is not registered")
        return SimpleNamespace(data_source=REGISTERED[data_source_id])

    async def record(*a, **k):
        return None

    deps = SimpleNamespace(
        settings=settings, caller=caller, runtime_for=runtime_for,
        platform=SimpleNamespace(catalog=store),
        admin_audit=SimpleNamespace(record=record), client_ip=lambda r: "127.0.0.1",
    )
    app = FastAPI()
    catalog_routes.register(app, deps)
    return TestClient(app), store


def test_the_listing_follows_the_requested_database(client):
    http, _ = client
    default = http.get(f"{BASE}/inferred").json()
    assert default["relationships"] == []

    northwind = http.get(
        f"{BASE}/inferred", headers={"X-Data-Source-Id": "postgresql://wh/northwind"}
    ).json()
    (row,) = northwind["relationships"]
    assert row["in_use"] is True
    assert northwind["min_confidence"] == pytest.approx(0.8)


def test_an_unregistered_database_is_refused_not_defaulted(client):
    http, _ = client
    response = http.get(
        f"{BASE}/inferred", headers={"X-Data-Source-Id": "postgresql://elsewhere/x"}
    )
    assert response.status_code == 404


def test_a_review_is_filed_against_the_requested_database(client):
    http, store = client
    response = http.put(
        f"{BASE}/review",
        headers={"X-Data-Source-Id": "postgresql://wh/northwind"},
        json={"from_table": "Northwind.SalesOrder", "from_column": "CustID",
              "to_table": "northwind.customer", "to_column": "custid",
              "decision": "rejected"},
    )
    assert response.status_code == 200
    tenant, source, kwargs = store.reviews[0]
    assert (tenant, source) == ("demo", "postgresql://wh/northwind")
    assert kwargs["from_table"] == "northwind.salesorder"  # normalized
    assert kwargs["decision"] == "rejected"


def test_an_unknown_join_is_a_404(client):
    http, _ = client
    response = http.put(
        f"{BASE}/review",
        json={"from_table": "a", "from_column": "nope", "to_table": "b",
              "to_column": "id", "decision": "accepted"},
    )
    assert response.status_code == 404


def test_an_unknown_decision_is_rejected_by_validation(client):
    http, _ = client
    response = http.put(
        f"{BASE}/review",
        json={"from_table": "a", "from_column": "custid", "to_table": "b",
              "to_column": "id", "decision": "maybe"},
    )
    assert response.status_code == 422
