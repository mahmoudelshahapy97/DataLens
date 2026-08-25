"""The configuration API: what an administrator may change, and what is refused.

Route-level tests over an in-memory store, so they run without PostgreSQL. Three
groups, and the first two are the ones worth having:

* **authorisation.** A cube names the models a workspace's grants are written
  against, so a tenant admin who could rewrite their own cubes could widen their
  own read access. Platform admin only, on every route including the reads.
* **validation.** Content that is stored and only fails later -- at the next cold
  runtime build, minutes later, in a different request -- is worse than content
  that is refused. Everything goes through the runtime's own types on the way in.
* **the manifest is recompiled.** The runtime reads ``target/mdl.json``, not the
  cubes. A save that left the manifest alone would report success and change
  nothing, which is the most confusing outcome a working save can have.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI

from vanna_app.config_store import ConfigRecord, classify

CUBE = "name: sales\nbase_object: orders\ndescription: Order revenue.\n"


def make_user(email: str, tenant: str = "acme") -> Any:
    from vanna.core.user import User

    return User(
        id=email,
        tenant_id=tenant,
        email=email,
        group_memberships=["admin"],
        metadata={"role": "admin"},
    )


class MemoryConfigStore:
    """The store's contract, in a dict, with the same classification rules."""

    def __init__(self) -> None:
        self.rows: Dict[str, ConfigRecord] = {}
        self.versions: Dict[str, List[Dict[str, Any]]] = {}
        self.projected: List[str] = []
        self.db = SimpleNamespace(fetch_one=self._fetch_one)

    async def _fetch_one(self, sql: str, params=()) -> Optional[Dict[str, Any]]:
        # Only the one query the revert route makes.
        record_id, version = params
        for path, history in self.versions.items():
            for entry in history:
                if entry["id"] == record_id and entry["version"] == version:
                    return {"raw_content": entry["raw_content"]}
        return None

    async def get(self, path, **kwargs) -> Optional[ConfigRecord]:
        return self.rows.get(path)

    async def list(self, *, kind=None, tenant_id=None, with_content=True, **kwargs):
        return [
            record
            for record in sorted(self.rows.values(), key=lambda r: r.relative_path)
            if (kind is None or record.kind == kind)
            and (tenant_id is None or record.tenant_id == tenant_id)
        ]

    async def history(self, record_id, *, limit=20):
        for history in self.versions.values():
            if history and history[0]["id"] == record_id:
                return list(reversed(history))[:limit]
        return []

    async def put(self, record, *, source="import", actor=None, note=None,
                  project_into=None):
        existing = self.rows.get(record.relative_path)
        if existing is not None and existing.checksum == record.checksum:
            record.id, record.version = existing.id, existing.version
            return "unchanged"
        record.id = (existing.id if existing else len(self.rows) + 1)
        record.version = (existing.version + 1) if existing else 1
        self.rows[record.relative_path] = record
        self.versions.setdefault(record.relative_path, []).append(
            {
                "id": record.id,
                "version": record.version,
                "checksum": record.checksum,
                "raw_content": record.raw_content,
                "source": source,
                "created_by": actor,
                "note": note,
                "created_at": None,
            }
        )
        if project_into is not None:
            project_into(FakeCursor(self), record)
        return "updated" if existing else "created"

    def seed(self, path: str, content: str, parsed: Any = None) -> ConfigRecord:
        import yaml

        if parsed is None and path.endswith((".yml", ".yaml")):
            parsed = yaml.safe_load(content)
        if parsed is None and path.endswith(".json"):
            parsed = json.loads(content)
        record = ConfigRecord(
            relative_path=path, raw_content=content, parsed=parsed
        )
        record.id = len(self.rows) + 1
        record.version = 1
        self.rows[path] = record
        self.versions.setdefault(path, []).append(
            {
                "id": record.id, "version": 1, "checksum": record.checksum,
                "raw_content": content, "source": "import",
                "created_by": "importer", "note": None, "created_at": None,
            }
        )
        return record


class FakeCursor:
    """Just enough cursor for the projection to run against the memory store."""

    def __init__(self, store: MemoryConfigStore) -> None:
        self.store = store
        self._rows: List[Any] = []
        self._one: Any = None

    def execute(self, sql: str, params=()) -> None:
        text = " ".join(sql.split())
        if "SELECT relative_path, kind, raw_content, parsed" in text:
            tenant = params[1]
            self._rows = [
                (r.relative_path, r.kind, r.raw_content, r.parsed)
                for r in self.store.rows.values()
                if r.tenant_id == tenant and r.scope == "project"
            ]
        elif "FROM vanna_app.config_files" in text and "FOR UPDATE" in text:
            path = params[3]
            existing = self.store.rows.get(path)
            self._one = (
                (existing.id, existing.checksum, existing.version)
                if existing
                else None
            )
        elif text.startswith(("UPDATE vanna_app.config_files",
                              "INSERT INTO vanna_app.config_files")):
            path = _path_from(text, params)
            self.store.projected.append(path)
            self._one = (999, 2)
        elif "FROM vanna_app.tenants" in text:
            self._one = None
        else:
            self._one = None

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


def _path_from(text: str, params) -> str:
    for value in params:
        if isinstance(value, str) and value.endswith(".json"):
            return value
    return "unknown"


class Harness:
    def __init__(self, user: Any, *, admin_emails=("root@example.com",)) -> None:
        from vanna_app.routes import config

        self.user = user
        self.store = MemoryConfigStore()
        self.forgotten = 0

        async def caller(request, **kwargs):
            return self.user

        def forget():
            self.forgotten += 1

        self.deps = SimpleNamespace(
            settings=SimpleNamespace(
                is_demo=False,
                admin_emails=set(admin_emails),
                config_source="database",
                config_refresh_seconds=5,
            ),
            platform=SimpleNamespace(
                config_store=self.store, forget_configuration=forget
            ),
            caller=caller,
            admin_audit=None,
        )
        self.app = FastAPI()
        config.register(self.app, self.deps)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://test"
        )


@pytest.fixture
def harness():
    return Harness(make_user("root@example.com"))


BASE = "/api/vanna/v2/admin/config"


class TestAuthorisation:
    """Every route, because a read of a cube is a read of what a workspace's
    grants are written against."""

    # 404 rather than 403, which is what `authz.require_platform_admin`
    # answers everywhere: an unprivileged caller should not be able to map the
    # admin surface by reading status codes.
    @pytest.mark.parametrize(
        "method,url,body",
        [
            ("get", f"{BASE}/files", None),
            ("get", f"{BASE}/file?path=projects/acme/cubes/sales.yml", None),
            ("get", f"{BASE}/versions?path=projects/acme/cubes/sales.yml", None),
            (
                "put",
                f"{BASE}/file",
                {"path": "projects/acme/cubes/sales.yml", "content": CUBE},
            ),
            (
                "post",
                f"{BASE}/revert",
                {"path": "projects/acme/cubes/sales.yml", "version": 1},
            ),
        ],
    )
    async def test_a_tenant_admin_is_refused(self, method, url, body):
        harness = Harness(make_user("admin@acme.test"))
        async with harness.client() as client:
            response = await getattr(client, method)(
                url, **({"json": body} if body else {})
            )
        assert response.status_code == 404, response.text

    async def test_a_platform_admin_is_allowed(self, harness):
        async with harness.client() as client:
            response = await client.get(f"{BASE}/files")
        assert response.status_code == 200, response.text


class TestReads:
    async def test_listing_says_where_configuration_comes_from(self, harness):
        """So the screen can tell an administrator whether their edit will do
        anything at all -- in `disk` mode it will not."""
        harness.store.seed("projects/acme/cubes/sales.yml", CUBE)
        async with harness.client() as client:
            body = (await client.get(f"{BASE}/files")).json()

        assert body["source"] == "database"
        assert body["items"][0]["path"] == "projects/acme/cubes/sales.yml"
        assert body["items"][0]["kind"] == "cube"
        assert body["items"][0]["tenant_id"] == "acme"
        assert "content" not in body["items"][0], "the listing carried every file's body"

    async def test_reading_one_file_returns_its_content(self, harness):
        harness.store.seed("projects/acme/cubes/sales.yml", CUBE)
        async with harness.client() as client:
            body = (
                await client.get(
                    f"{BASE}/file", params={"path": "projects/acme/cubes/sales.yml"}
                )
            ).json()
        assert body["content"] == CUBE

    async def test_a_missing_file_is_404(self, harness):
        async with harness.client() as client:
            response = await client.get(f"{BASE}/file", params={"path": "nope.yml"})
        assert response.status_code == 404


class TestValidation:
    """Refused on the way in, with the runtime's own types."""

    async def test_broken_yaml_is_refused(self, harness):
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml",
                      "content": "name: [unclosed"},
            )
        assert response.status_code == 400
        assert harness.store.rows == {}

    async def test_a_cube_that_does_not_validate_is_refused(self, harness):
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml",
                      "content": "models: 7\n"},
            )
        assert response.status_code == 400, response.text
        assert harness.store.rows == {}

    async def test_a_project_config_without_a_name_is_refused(self, harness):
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/vanna_project.yml",
                      "content": "dialect: postgres\n"},
            )
        assert response.status_code == 400
        assert "name" in response.json()["detail"]

    async def test_a_baseline_rule_with_a_bad_id_is_refused(self, harness):
        """The id is permanent -- a workspace's opt-out is stored against it -- so
        the shape is checked here exactly as the file loader checks it."""
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={
                    "path": "instructions/baseline.yml",
                    "content": "instructions:\n  - id: oops\n    text: Be careful.\n",
                },
            )
        assert response.status_code == 400
        assert "platform." in response.json()["detail"]

    async def test_a_credential_is_refused(self, harness):
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={
                    "path": "projects/acme/vanna_project.yml",
                    "content": "name: Acme\npassword: hunter2\n",
                },
            )
        assert response.status_code == 400
        assert "credential" in response.json()["detail"]
        assert harness.store.rows == {}

    async def test_a_domain_document_with_a_missing_field_is_refused(self, harness):
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={
                    "path": "domains/domains.yml",
                    "content": "domains:\n  - id: acme\n    name: Acme\n",
                },
            )
        assert response.status_code == 400
        assert "database" in response.json()["detail"]

    async def test_a_valid_cube_is_stored(self, harness):
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml", "content": CUBE,
                      "note": "first cut"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["result"] == "created"
        assert body["kind"] == "cube"
        assert body["refresh_seconds"] == 5
        assert harness.store.rows["projects/acme/cubes/sales.yml"].raw_content == CUBE


class TestTheWriteTakesEffect:
    async def test_the_worker_forgets_what_it_had_cached(self, harness):
        """Its own edit, immediately. The other three workers notice on their next
        fingerprint check, which is what `refresh_seconds` in the reply is about."""
        async with harness.client() as client:
            await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml", "content": CUBE},
            )
        assert harness.forgotten == 1

    async def test_an_unchanged_write_does_not_disturb_anything(self, harness):
        harness.store.seed("projects/acme/cubes/sales.yml", CUBE)
        async with harness.client() as client:
            body = (
                await client.put(
                    f"{BASE}/file",
                    json={"path": "projects/acme/cubes/sales.yml", "content": CUBE},
                )
            ).json()
        assert body["result"] == "unchanged"
        assert harness.forgotten == 0, "it retired every runtime for a no-op"

    async def test_saving_a_cube_recompiles_the_manifest(self, harness):
        """The runtime reads `target/mdl.json`. A save that left it alone would
        report success and change nothing at all."""
        harness.store.seed(
            "projects/acme/vanna_project.yml",
            "name: Acme\ndialect: postgres\n",
        )
        async with harness.client() as client:
            response = await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml", "content": CUBE},
            )
        assert response.status_code == 200, response.text
        assert harness.store.projected == ["projects/acme/target/mdl.json"]


class TestHistory:
    async def test_the_versions_of_a_file_are_listed_newest_first(self, harness):
        harness.store.seed("projects/acme/cubes/sales.yml", CUBE)
        async with harness.client() as client:
            await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml",
                      "content": CUBE + "description: revenue\n",
                      "note": "widened"},
            )
            body = (
                await client.get(
                    f"{BASE}/versions",
                    params={"path": "projects/acme/cubes/sales.yml"},
                )
            ).json()

        assert [item["version"] for item in body["items"]] == [2, 1]
        assert body["items"][0]["note"] == "widened"
        assert body["items"][0]["source"] == "api"

    async def test_reverting_moves_forward_rather_than_rewriting(self, harness):
        """A history that can be rewritten answers "what was running last
        Tuesday" with a guess."""
        harness.store.seed("projects/acme/cubes/sales.yml", CUBE)
        edited = CUBE + "description: revenue\n"
        async with harness.client() as client:
            await client.put(
                f"{BASE}/file",
                json={"path": "projects/acme/cubes/sales.yml", "content": edited},
            )
            body = (
                await client.post(
                    f"{BASE}/revert",
                    json={"path": "projects/acme/cubes/sales.yml", "version": 1},
                )
            ).json()

        assert body["version"] == 3, body
        assert harness.store.rows["projects/acme/cubes/sales.yml"].raw_content == CUBE

    async def test_reverting_to_a_version_that_does_not_exist_is_404(self, harness):
        harness.store.seed("projects/acme/cubes/sales.yml", CUBE)
        async with harness.client() as client:
            response = await client.post(
                f"{BASE}/revert",
                json={"path": "projects/acme/cubes/sales.yml", "version": 9},
            )
        assert response.status_code == 404


class TestPaths:
    def test_the_route_and_the_store_classify_alike(self):
        """The route writes what `classify` decides, and the runtime looks it up
        the same way. If these disagreed, a saved cube would be stored somewhere
        the runtime never looks."""
        where = classify("projects/acme/cubes/sales.yml")
        record = ConfigRecord(
            relative_path="projects/acme/cubes/sales.yml", raw_content=CUBE
        )
        assert (record.scope, record.tenant_id, record.kind) == (
            where.scope, where.tenant_id, where.kind,
        )
