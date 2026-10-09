"""Configuration in PostgreSQL: classification, secrets, caching, round trip.

Every semantic project, instruction pack and domain definition used to be read
from a file on the path that answers a question. The catalog moves that into the
control plane so an administrator can change a cube without rebuilding an image.

The tests that matter most here are not the storage ones. They are:

* **the refusal to fall back to disk.** With ``VANNA_CONFIG_SOURCE=database``, a
  missing manifest has to be an error. A silent reversion to whatever YAML is
  baked into the image is a deployment that looks healthy while running last
  week's configuration -- and, because grants for a semantic workspace name
  models, one that has quietly lost the enforcement built on them.
* **the refusal to store a credential.** ``config_files`` must not become a
  second, unencrypted place warehouse passwords live next to
  ``tenant_datasources``.
* **cross-process invalidation.** Four uvicorn workers each hold their own cache,
  so an in-process ``invalidate()`` would leave three of them serving the old
  cube forever. The cache re-checks a database fingerprint instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from vanna_app import config_import
from vanna_app.config_store import (
    KIND_BASELINE,
    KIND_CUBE,
    KIND_DOMAIN,
    KIND_MANIFEST,
    KIND_MODEL,
    KIND_PACK,
    KIND_PROJECT_CONFIG,
    ConfigCache,
    ConfigParseError,
    ConfigRecord,
    ConfigurationUnavailable,
    PostgresConfigStore,
    StoredProject,
    checksum_of,
    classify,
    find_secrets,
    normalise_path,
    parse_content,
    redact,
)


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------


class TestClassification:
    """A new configuration file must not need a migration, or a code change."""

    @pytest.mark.parametrize(
        "path,kind",
        [
            ("domains/domains.yml", KIND_DOMAIN),
            ("instructions/baseline.yml", KIND_BASELINE),
            ("instructions/packs/data-hygiene.yml", KIND_PACK),
            ("projects/chinook/vanna_project.yml", KIND_PROJECT_CONFIG),
            ("projects/chinook/cubes/sales.yml", KIND_CUBE),
            ("projects/chinook/models/albums/metadata.yml", KIND_MODEL),
            ("projects/chinook/models/albums/ref_sql.sql", "model_sql"),
            ("projects/chinook/relationships.yml", "relationships"),
            ("projects/chinook/target/mdl.json", KIND_MANIFEST),
            ("projects/chinook/knowledge/rules/general.md", "knowledge_rule"),
            ("projects/chinook/knowledge/sql/examples.md", "knowledge_sql"),
            ("evals/datasets/sql_generation/basic.yaml", "eval_dataset"),
        ],
    )
    def test_every_shipped_shape(self, path, kind):
        assert classify(path).kind == kind

    def test_a_project_file_carries_its_workspace(self):
        """The directory name is the tenant id -- the rule `_project_dir_for` uses.

        If this drifts, a workspace silently gets another workspace's models, and
        every query fails against a schema that does not contain them.
        """
        where = classify("projects/chinook/cubes/sales.yml")
        assert (where.scope, where.tenant_id, where.project) == (
            "project", "chinook", "chinook",
        )

    def test_global_content_is_not_owned_by_a_workspace(self):
        where = classify("instructions/packs/retail-operations.yml")
        assert (where.scope, where.tenant_id) == ("global", "")

    def test_an_unknown_path_is_kept_not_refused(self):
        """The alternative is a migration per new file, which is the thing this
        design exists to avoid."""
        assert classify("something/nobody/planned.yml").kind == "other"

    def test_windows_separators_do_not_create_a_second_identity(self):
        """The path is the row's identity. Importing from a Windows checkout and
        looking it up from the Linux container has to find the same row."""
        assert (
            normalise_path("projects\\chinook\\cubes\\sales.yml")
            == "projects/chinook/cubes/sales.yml"
        )
        assert classify("projects\\chinook\\cubes\\sales.yml").kind == KIND_CUBE


class TestParsing:
    def test_yaml_and_json_are_parsed(self):
        assert parse_content("a.yml", "name: sales") == {"name": "sales"}
        assert parse_content("a.json", '{"n": 1}') == {"n": 1}

    def test_markdown_and_sql_have_no_parsed_form(self):
        """None rather than {}: "no structure" and "an empty mapping" are
        different, and the runtime reads `parsed` to decide whether it can use a
        row at all."""
        assert parse_content("rules/general.md", "# a rule") is None
        assert parse_content("models/x/ref_sql.sql", "SELECT 1") is None

    def test_broken_yaml_raises_rather_than_returning_none(self):
        with pytest.raises(ConfigParseError) as caught:
            parse_content("a.yml", "name: [unclosed")
        assert "a.yml" in str(caught.value)

    def test_the_checksum_is_of_the_bytes(self):
        assert checksum_of("a") == checksum_of("a") != checksum_of("a ")


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------


class TestSecretRefusal:
    """`tenant_datasources` already holds warehouse credentials, encrypted. The
    catalog must not become a second store for them, in plain text."""

    def test_a_password_key_is_found(self):
        assert find_secrets({"database": {"password": "hunter2"}}) == [
            "database.password"
        ]

    def test_a_credentialed_url_is_found_anywhere(self):
        found = find_secrets({"domains": [{"url": "postgresql://u:p@host/db"}]})
        assert found == ["domains[0].url"]

    def test_it_is_found_in_a_file_with_no_structure(self):
        """A `.md` or `.sql` file has no keys to walk, so the URL scan is the only
        thing standing between it and a stored credential."""
        assert find_secrets(None, "psql postgresql://u:p@host/db") == ["<raw content>"]

    @pytest.mark.parametrize(
        "document",
        [
            {"token_budget": 4000},
            {"api_key": ""},
            {"secret": None},
            {"text": "never expose the password column to a user"},
            {"description": "see https://example.com/docs"},
        ],
    )
    def test_what_is_not_a_secret(self, document):
        """A check people route around protects nothing, so it has to be quiet
        about numbers, blanks, and instructions that merely mention the word."""
        assert find_secrets(document, json.dumps(document)) == []

    def test_the_shipped_configuration_is_clean(self):
        """The real files, because a check that only passes on fixtures would not
        have caught the domain file if it had ever carried a URL."""
        root = config_import.content_root()
        dirty = []
        for path in config_import.discover(root):
            record, outcome = config_import.read_record(path, root)
            if outcome is not None and outcome.result == "refused":
                dirty.append(outcome.relative_path)
        assert dirty == []

    def test_redaction_keeps_the_shape(self):
        masked = redact({"host": "db", "password": "hunter2", "port": 5432})
        assert masked["host"] == "db" and masked["port"] == 5432
        assert "hunter2" not in json.dumps(masked)

    def test_a_file_with_a_secret_is_refused_not_stored(self, tmp_path):
        (tmp_path / "projects" / "acme").mkdir(parents=True)
        target = tmp_path / "projects" / "acme" / "vanna_project.yml"
        target.write_text("name: acme\npassword: hunter2\n", encoding="utf-8")

        record, outcome = config_import.read_record(target, tmp_path)

        assert record is None, "a credential-bearing file was turned into a row"
        assert outcome.result == "refused"
        assert "password" in outcome.detail

    def test_redact_lets_it_through_masked(self, tmp_path):
        (tmp_path / "projects" / "acme").mkdir(parents=True)
        target = tmp_path / "projects" / "acme" / "vanna_project.yml"
        target.write_text("name: acme\npassword: hunter2\n", encoding="utf-8")

        record, outcome = config_import.read_record(
            target, tmp_path, redact_secrets=True
        )

        assert record is not None and outcome is None
        assert "hunter2" not in json.dumps(record.parsed)
        assert record.metadata["redacted"] == ["password"]


class TestDiscovery:
    def test_the_library_is_not_configuration(self, tmp_path):
        """`backend/vanna/` is code. Its YAML is packaging metadata and test
        fixtures, and walking into it would put hundreds of irrelevant rows in
        the catalog."""
        (tmp_path / "vanna" / "integrations").mkdir(parents=True)
        (tmp_path / "vanna" / "integrations" / "thing.yml").write_text("a: 1")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.json").write_text("{}")
        (tmp_path / "domains").mkdir()
        (tmp_path / "domains" / "domains.yml").write_text("domains: []")

        found = config_import.discover(tmp_path)

        assert [p.name for p in found] == ["domains.yml"]

    def test_the_real_tree_is_found_and_is_all_configuration(self):
        root = config_import.content_root()
        found = config_import.discover(root)
        assert found, f"nothing catalogable under {root}"
        assert all(
            p.suffix in {".yml", ".yaml", ".json", ".md", ".sql"} for p in found
        )
        # Relative to the root, because this checkout's own top directory is
        # called "vanna" and an absolute-path check matches it every time.
        relative = [p.relative_to(root) for p in found]
        assert not any("vanna" in path.parts for path in relative)
        assert any(path.parts[0] == "projects" for path in relative)


# ----------------------------------------------------------------------
# Records and projects
# ----------------------------------------------------------------------


class TestStoredProject:
    def test_it_validates_exactly_as_the_file_loader_does(self):
        record = ConfigRecord(
            relative_path="projects/demo/vanna_project.yml",
            raw_content="name: Demo",
            parsed={"name": "Demo", "dialect": "postgres", "fanout_guard": "reject"},
        )
        project = StoredProject.from_record(record)
        assert project.config.name == "Demo"
        assert project.config.fanout_guard == "reject"

    def test_a_stored_project_with_no_name_is_refused(self):
        """Through `ProjectConfig.from_dict`, so the message is the one a reader
        would get from the file -- not a second, differently-worded check."""
        from vanna.core.errors import VannaError

        record = ConfigRecord(
            relative_path="projects/demo/vanna_project.yml",
            raw_content="{}",
            parsed={"dialect": "postgres"},
        )
        with pytest.raises(VannaError):
            StoredProject.from_record(record)


# ----------------------------------------------------------------------
# The cache
# ----------------------------------------------------------------------


class FakeStore:
    """A store whose fingerprint the test controls."""

    def __init__(self) -> None:
        self.value = (1, "a", 1)
        self.calls = 0
        self.fail = False

    async def fingerprint(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("the control plane is unhappy")
        return self.value


class TestConfigCache:
    def test_a_built_object_is_reused(self):
        store = FakeStore()
        cache = ConfigCache(store, refresh_seconds=60)
        built = []

        async def build():
            built.append(1)
            return "manifest"

        async def go():
            assert await cache.get("k", build) == "manifest"
            assert await cache.get("k", build) == "manifest"

        asyncio.run(go())
        assert len(built) == 1

    def test_a_miss_is_cached_too(self):
        """"This workspace has no semantic project" is the common answer, and
        asking the database for it on every runtime build is the cost this cache
        exists to remove."""
        store = FakeStore()
        cache = ConfigCache(store, refresh_seconds=60)
        built = []

        async def build():
            built.append(1)
            return (None, None)

        async def go():
            await cache.get("k", build)
            await cache.get("k", build)

        asyncio.run(go())
        assert len(built) == 1

    def test_a_changed_fingerprint_rebuilds_and_notifies(self):
        """The cross-process property: nothing called `invalidate()` here. The
        write happened in another worker, and this one noticed by asking."""
        store = FakeStore()
        changed = []
        cache = ConfigCache(
            store, refresh_seconds=0, on_change=lambda: changed.append(1)
        )
        built = []

        async def build():
            built.append(1)
            return len(built)

        async def go():
            assert await cache.get("k", build) == 1
            store.value = (2, "b", 3)
            assert await cache.get("k", build) == 2

        asyncio.run(go())
        assert changed == [1], "the runtimes built on the old manifest were kept"

    def test_the_refresh_window_bounds_the_number_of_probes(self):
        store = FakeStore()
        cache = ConfigCache(store, refresh_seconds=60)

        async def go():
            for _ in range(10):
                await cache.get("k", _one)

        asyncio.run(go())
        assert store.calls == 1

    def test_a_failed_probe_keeps_the_cache(self):
        """Dropping it would mean rebuilding every manifest on every request
        against a database that is already struggling."""
        store = FakeStore()
        cache = ConfigCache(store, refresh_seconds=0)
        built = []

        async def build():
            built.append(1)
            return len(built)

        async def go():
            await cache.get("k", build)
            store.fail = True
            assert await cache.get("k", build) == 1

        asyncio.run(go())
        assert len(built) == 1

    def test_no_store_means_no_probes(self):
        """`disk` mode still goes through the cache; it just has nothing to ask."""
        cache = ConfigCache(None, refresh_seconds=0)
        assert asyncio.run(cache.get("k", _one)) == 1


async def _one():
    return 1


# ----------------------------------------------------------------------
# The instruction library
# ----------------------------------------------------------------------


class TestInstructionLibraryFromRecords:
    def test_it_builds_from_stored_rows(self):
        from vanna_app.instruction_library import InstructionLibrary

        library = InstructionLibrary.from_records(
            [
                ConfigRecord(
                    relative_path="instructions/baseline.yml",
                    raw_content="",
                    kind=KIND_BASELINE,
                    parsed={
                        "instructions": [
                            {"id": "platform.no-select-star", "text": "Be specific."}
                        ]
                    },
                ),
                ConfigRecord(
                    relative_path="instructions/packs/tidy.yml",
                    raw_content="",
                    kind=KIND_PACK,
                    parsed={
                        "id": "tidy",
                        "name": "Tidy",
                        "instructions": [{"text": "Round money to cents."}],
                    },
                ),
            ]
        )
        assert [e.instruction.id for e in library.baseline] == [
            "platform.no-select-star"
        ]
        assert library.pack("tidy").name == "Tidy"

    def test_an_empty_baseline_refuses_to_boot(self):
        """The same posture as the file loader. A process that starts and quietly
        applies no baseline rules is the failure the baseline exists to prevent --
        and after a failed import, that is exactly the state the catalog is in."""
        from vanna_app.instruction_library import (
            InstructionContentError,
            InstructionLibrary,
        )

        with pytest.raises(InstructionContentError) as caught:
            InstructionLibrary.from_records([])
        assert "import_config_files" in str(caught.value)

    def test_the_shipped_files_and_the_stored_rows_agree(self):
        """The two loaders share their validation, so this is really asserting
        that `parsed` is a faithful stand-in for the file."""
        from vanna_app.instruction_library import InstructionLibrary

        root = config_import.content_root()
        records = []
        for path in config_import.discover(root):
            record, _ = config_import.read_record(path, root)
            if record is not None and record.kind in (KIND_BASELINE, KIND_PACK):
                records.append(record)

        from_disk = InstructionLibrary.load()
        from_rows = InstructionLibrary.from_records(records)

        assert [e.instruction.text for e in from_rows.baseline] == [
            e.instruction.text for e in from_disk.baseline
        ]
        assert set(from_rows.packs) == set(from_disk.packs)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


class TestSettings:
    def test_an_unknown_source_is_refused_at_boot(self):
        from vanna_app.config import load_settings, validate

        settings = load_settings({"VANNA_CONFIG_SOURCE": "postgres"})
        assert any("VANNA_CONFIG_SOURCE" in p for p in validate(settings))

    def test_database_mode_without_a_control_plane_is_refused(self):
        from vanna_app.config import load_settings, validate

        settings = load_settings(
            {
                "VANNA_CONFIG_SOURCE": "database",
                "VANNA_DEPLOYMENT_MODE": "demo",
                "VANNA_APP_DATABASE_URL": "",
            }
        )
        problems = validate(settings)
        assert any("VANNA_APP_DATABASE_URL" in p for p in problems)

    def test_disk_is_the_default(self):
        """A deployment opts in. Defaulting to the catalog would break every
        existing checkout on the first boot after an upgrade, before anybody had
        a chance to import."""
        from vanna_app.config import load_settings

        assert load_settings({}).config_source == "disk"


# ----------------------------------------------------------------------
# Against a real database
# ----------------------------------------------------------------------


@pytest.fixture
def store(app_db):
    return PostgresConfigStore(app_db)


def count_rows(store, table):
    row = store.db.run_sync(f"SELECT count(*) AS n FROM vanna_app.{table}", fetch="one")
    return int(row["n"])


@pytest.mark.integration
class TestRoundTrip:
    """The files go in and come back unchanged, or the catalog is not a home for
    them -- it is a lossy copy, and the export would write out something nobody
    ever wrote."""

    def test_every_shipped_file_survives_the_trip(self, store):
        root = config_import.content_root()
        report = config_import.import_files(store, root=root)

        assert report.ok, report.summary()
        assert not report.failed and not report.refused

        on_disk = config_import.discover(root)
        stored = asyncio.run(store.list())
        assert len(stored) == len(on_disk), report.summary()

        by_path = {record.relative_path: record for record in stored}
        for path in on_disk:
            relative = normalise_path(str(path.relative_to(root)))
            record = by_path[relative]
            assert record.raw_content == path.read_text(encoding="utf-8"), relative
            if relative.endswith((".yml", ".yaml")):
                assert record.parsed == yaml.safe_load(record.raw_content), relative
            elif relative.endswith(".json"):
                assert record.parsed == json.loads(record.raw_content), relative
            else:
                assert record.parsed is None, relative

    def test_re_importing_changes_nothing(self, store):
        """The property that makes running this on every boot reasonable."""
        root = config_import.content_root()
        first = config_import.import_files(store, root=root)
        before = asyncio.run(store.list(with_content=False))

        second = config_import.import_files(store, root=root)

        assert len(second.created) == 0 and len(second.updated) == 0
        assert len(second.unchanged) == len(first.created)

        after = asyncio.run(store.list(with_content=False))
        assert {r.relative_path: r.updated_at for r in after} == {
            r.relative_path: r.updated_at for r in before
        }
        assert count_rows(store, "config_versions") == len(before), (
            "an import that changed nothing still wrote history"
        )

    def test_a_dry_run_writes_nothing(self, store):
        root = config_import.content_root()
        report = config_import.import_files(store, root=root, dry_run=True)

        assert len(report.created) > 0
        assert asyncio.run(store.list(with_content=False)) == []

    def test_an_unparseable_file_is_stored_and_reported(self, store, tmp_path):
        """Reported, because a file dropped in silence is a configuration
        difference that surfaces weeks later as a missing cube."""
        (tmp_path / "projects" / "acme" / "cubes").mkdir(parents=True)
        broken = tmp_path / "projects" / "acme" / "cubes" / "sales.yml"
        broken.write_text("name: [unclosed\n", encoding="utf-8")

        report = config_import.import_files(store, root=tmp_path)

        assert [o.relative_path for o in report.unparseable] == [
            "projects/acme/cubes/sales.yml"
        ]
        assert report.ok, "an unparseable file is not a failed import"
        stored = asyncio.run(store.list())
        assert len(stored) == 1
        assert stored[0].parsed is None
        assert stored[0].raw_content == broken.read_text(encoding="utf-8")

    def test_a_refused_file_leaves_no_row(self, store, tmp_path):
        (tmp_path / "projects" / "acme").mkdir(parents=True)
        (tmp_path / "projects" / "acme" / "vanna_project.yml").write_text(
            "name: acme\npassword: hunter2\n", encoding="utf-8"
        )

        report = config_import.import_files(store, root=tmp_path)

        assert not report.ok
        assert asyncio.run(store.list(with_content=False)) == []


@pytest.mark.integration
class TestVersioning:
    """Once a cube can be edited from a browser, "what did this look like before"
    stops being a git question and becomes a database one."""

    def a_cube(self, description=""):
        parsed = {"name": "sales"}
        raw = "name: sales\n"
        if description:
            parsed["description"] = description
            raw += f"description: {description}\n"
        return ConfigRecord(
            relative_path="projects/demo/cubes/sales.yml",
            raw_content=raw,
            parsed=parsed,
        )

    def test_a_change_is_recorded_with_its_predecessor(self, store):
        first = self.a_cube()
        assert asyncio.run(store.put(first, actor="importer")) == "created"

        edited = self.a_cube("revenue")
        assert (
            asyncio.run(
                store.put(
                    edited, source="api", actor="admin@example.com", note="widened"
                )
            )
            == "updated"
        )

        history = asyncio.run(store.history(edited.id))
        assert [h["version"] for h in history] == [2, 1]
        assert history[0]["source"] == "api"
        assert history[0]["created_by"] == "admin@example.com"
        assert history[0]["note"] == "widened"

        previous = store.db.run_sync(
            "SELECT raw_content FROM vanna_app.config_versions "
            "WHERE config_file_id = %s AND version = 1",
            (edited.id,),
            fetch="one",
        )
        assert previous["raw_content"] == "name: sales\n"

    def test_an_unchanged_write_does_not_bump_the_version(self, store):
        asyncio.run(store.put(self.a_cube()))
        again = self.a_cube()
        assert asyncio.run(store.put(again)) == "unchanged"
        assert again.version == 1
        assert count_rows(store, "config_versions") == 1

    def test_the_projection_runs_in_the_same_transaction(self, store):
        seen = []

        def project(cursor, record):
            cursor.execute("SELECT 1")
            seen.append(record.relative_path)

        asyncio.run(store.put(self.a_cube(), project_into=project))
        assert seen == ["projects/demo/cubes/sales.yml"]

    def test_a_failing_projection_rolls_the_write_back(self, store):
        """Which is why the projection is a callback inside `put` rather than a
        second write beside it: a failure between the two would leave the catalog
        and the derived tables disagreeing, with nothing to say which is right."""

        def explode(cursor, record):
            raise RuntimeError("the projection is unhappy")

        with pytest.raises(RuntimeError):
            asyncio.run(store.put(self.a_cube(), project_into=explode))

        assert asyncio.run(store.list(with_content=False)) == []
        assert count_rows(store, "config_versions") == 0


@pytest.mark.integration
class TestFingerprint:
    def a_cube(self, name="sales"):
        return ConfigRecord(
            relative_path=f"projects/demo/cubes/{name}.yml",
            raw_content=f"name: {name}\n",
            parsed={"name": name},
        )

    def test_it_moves_when_configuration_does(self, store):
        before = asyncio.run(store.fingerprint())
        asyncio.run(store.put(self.a_cube()))
        assert asyncio.run(store.fingerprint()) != before

    def test_it_holds_still_when_nothing_does(self, store):
        asyncio.run(store.put(self.a_cube()))
        assert asyncio.run(store.fingerprint()) == asyncio.run(store.fingerprint())

    def test_a_cache_over_the_real_store_notices_a_write(self, store):
        """End to end, with no in-process invalidation anywhere: the same thing
        that happens when another worker takes the write."""
        cache = ConfigCache(store, refresh_seconds=0)
        builds = []

        async def build():
            builds.append(1)
            return len(builds)

        async def go():
            assert await cache.get("k", build) == 1
            await store.put(self.a_cube("track_sales"))
            return await cache.get("k", build)

        assert asyncio.run(go()) == 2


@pytest.mark.integration
class TestQueryingTheContent:
    """The reason `parsed` is JSONB with a GIN index rather than a text column."""

    def test_a_model_can_be_found_across_every_project(self, store):
        config_import.import_files(store, root=config_import.content_root())

        # Which projects declare a `customers` model is read from the same tree the
        # import walked. It used to be the literal pair {"chinook", "demo"}, and
        # `projects/demo` was a byte-for-byte copy of `projects/chinook` -- so the
        # assertion passed on a duplicate and broke the day the duplicate went away.
        projects = config_import.content_root() / "projects"
        expected = {
            directory.name
            for directory in projects.iterdir()
            if (directory / "models" / "customers" / "metadata.yml").is_file()
        }
        assert expected, "no project declares a customers model; this test needs one"

        rows = store.db.run_sync(
            "SELECT tenant_id FROM vanna_app.config_files "
            "WHERE kind = 'model' AND parsed @> %s::jsonb ORDER BY tenant_id",
            (json.dumps({"name": "customers"}),),
            fetch="all",
        )
        assert {r["tenant_id"] for r in rows} == expected

        # The scoping itself, on a model only one project has: a lookup must not
        # spill across tenants even when the name is unique.
        rows = store.db.run_sync(
            "SELECT tenant_id FROM vanna_app.config_files "
            "WHERE kind = 'model' AND parsed @> %s::jsonb ORDER BY tenant_id",
            (json.dumps({"name": "orders"}),),
            fetch="all",
        )
        assert {r["tenant_id"] for r in rows} == {"acme"}


@pytest.mark.integration
class TestTheRuntimeReadsTheCatalog:
    """`Platform._load_project_from_catalog`, on the real store.

    Bound onto a stub rather than exercised through a whole `Platform`: the method
    needs two settings and a store, and building the rest -- an LLM client, a
    retrieval index, a catalog -- would test everything except the thing in
    question.
    """

    def loader(self, store, **settings):
        from types import SimpleNamespace

        from vanna_app.platform import Platform

        stub = SimpleNamespace(
            settings=SimpleNamespace(config_source="database", **settings),
            config_store=store,
        )
        stub._load_project_from_catalog = (
            Platform._load_project_from_catalog.__get__(stub)
        )
        stub._log_semantic_layer = Platform._log_semantic_layer
        return stub

    def test_it_builds_the_project_and_manifest_from_rows(self, store):
        config_import.import_files(store, root=config_import.content_root())
        stub = self.loader(store)

        project, manifest = asyncio.run(stub._load_project_from_catalog("chinook"))

        assert project is not None and manifest is not None
        assert project.config.name
        assert len(manifest.models) > 5
        assert {m.name for m in manifest.models} >= {"customers", "invoices"}

    def test_it_matches_what_the_files_produce(self, store):
        """The two paths have to agree, or switching a deployment over changes
        what the agent is allowed to see."""
        from vanna.project import Project
        from vanna.semantic import load_built_manifest

        root = config_import.content_root()
        config_import.import_files(store, root=root)

        from_disk = load_built_manifest(Project.load(root / "projects" / "chinook").paths)
        _, from_rows = asyncio.run(
            self.loader(store)._load_project_from_catalog("chinook")
        )

        assert from_rows.to_json_dict() == from_disk.to_json_dict()

    def test_a_workspace_with_no_project_has_no_semantic_layer(self, store):
        """Not an error: a workspace that never had a project queries its physical
        catalog, exactly as one with no project directory does."""
        config_import.import_files(store, root=config_import.content_root())

        assert asyncio.run(
            self.loader(store)._load_project_from_catalog("nobody")
        ) == (None, None)

    def test_a_project_without_its_manifest_is_an_error(self, store):
        """And specifically not a fall-through to the physical catalog. Grants for
        a semantic workspace name models, so a workspace that loses its manifest
        loses the enforcement built on it -- silently widening what it can read."""
        asyncio.run(
            store.put(
                ConfigRecord(
                    relative_path="projects/lonely/vanna_project.yml",
                    raw_content="name: Lonely\n",
                    parsed={"name": "Lonely", "dialect": "postgres"},
                )
            )
        )

        with pytest.raises(ConfigurationUnavailable) as caught:
            asyncio.run(self.loader(store)._load_project_from_catalog("lonely"))
        assert "mdl.json" in str(caught.value)

    def test_it_never_reads_disk(self, store, monkeypatch):
        """The point of the mode. `_project_dir_for` is what the disk path uses,
        and in database mode it must not be consulted at all -- if it were, an
        empty catalog would quietly serve whatever the image shipped."""
        config_import.import_files(store, root=config_import.content_root())
        stub = self.loader(store)

        def refuse(*args, **kwargs):
            raise AssertionError("the catalog path went to disk")

        monkeypatch.setattr(
            "vanna.project.Project.load", staticmethod(refuse), raising=True
        )

        _, manifest = asyncio.run(stub._load_project_from_catalog("chinook"))
        assert manifest is not None

    def test_no_control_plane_is_an_error_not_a_fallback(self):
        stub = self.loader(None)
        with pytest.raises(ConfigurationUnavailable):
            asyncio.run(stub._load_project_from_catalog("chinook"))


@pytest.mark.integration
class TestTheInstructionLibraryFromTheCatalog:
    def test_it_loads_what_was_imported(self, store):
        from vanna_app.instruction_library import InstructionLibrary
        from vanna_app.wiring import _load_instruction_library
        from types import SimpleNamespace

        config_import.import_files(store, root=config_import.content_root())
        library = _load_instruction_library(
            SimpleNamespace(config_source="database"), store
        )

        assert len(library.baseline) == len(InstructionLibrary.load().baseline)
        assert set(library.packs) == set(InstructionLibrary.load().packs)

    def test_an_empty_catalog_refuses_the_boot(self, store):
        from types import SimpleNamespace

        from vanna_app.instruction_library import InstructionContentError
        from vanna_app.wiring import _load_instruction_library

        with pytest.raises(InstructionContentError):
            _load_instruction_library(
                SimpleNamespace(config_source="database"), store
            )


@pytest.mark.integration
class TestDomainsFromTheCatalog:
    def test_provisioning_reads_the_catalog_when_that_is_the_source(self, store):
        from types import SimpleNamespace

        from vanna_app.domains import read_definitions

        config_import.import_files(store, root=config_import.content_root())
        domains = asyncio.run(
            read_definitions(
                SimpleNamespace(config_source="database"), store.db
            )
        )
        assert domains and all(d.get("id") for d in domains)

    def test_an_empty_catalog_is_an_error_not_a_silent_file_read(self, store):
        from types import SimpleNamespace

        from vanna_app.domains import read_definitions

        with pytest.raises(FileNotFoundError) as caught:
            asyncio.run(
                read_definitions(SimpleNamespace(config_source="database"), store.db)
            )
        assert "import_config_files" in str(caught.value)


@pytest.mark.integration
class TestTheManifestIsRecompiled:
    """A cube is a source; ``target/mdl.json`` is the build output the runtime
    reads. An edit that did not recompile would report success and change nothing,
    which is the most confusing possible outcome of a working save."""

    def edit_cube(self, store, tenant="chinook", name="sales", body=None):
        from vanna_app.config_projection import project_all

        record = ConfigRecord(
            relative_path=f"projects/{tenant}/cubes/{name}.yml",
            raw_content=body,
            parsed=yaml.safe_load(body),
        )
        return asyncio.run(
            store.put(
                record,
                source="api",
                actor="admin@example.com",
                project_into=lambda cursor, written: project_all(
                    cursor, written, actor="admin@example.com"
                ),
            )
        )

    def manifest(self, store, tenant="chinook"):
        return asyncio.run(
            store.get(f"projects/{tenant}/target/mdl.json")
        )

    def test_an_edited_cube_reaches_the_manifest(self, store):
        config_import.import_files(store, root=config_import.content_root())
        before = self.manifest(store)

        original = asyncio.run(store.get("projects/chinook/cubes/sales.yml"))
        edited = dict(original.parsed)
        edited["description"] = "Edited through the API."
        self.edit_cube(store, body=yaml.safe_dump(edited, sort_keys=False))

        after = self.manifest(store)
        assert after.version == before.version + 1
        assert after.checksum != before.checksum

        names = {cube["name"]: cube for cube in after.parsed["cubes"]}
        assert names["sales"]["description"] == "Edited through the API."

        # And it is still a manifest the runtime accepts, which is the only thing
        # that makes the edit worth anything.
        from vanna.semantic import Manifest

        rebuilt = Manifest.from_json_dict(after.parsed)
        assert len(rebuilt.models) == len(
            Manifest.from_json_dict(before.parsed).models
        )

    def test_the_recompiled_manifest_matches_the_file_build(self, store):
        """Byte for byte, so a re-import after an API edit reports "unchanged"
        rather than a phantom diff -- and so the two compilers cannot drift."""
        from vanna_app.config_projection import rebuild_manifest

        config_import.import_files(store, root=config_import.content_root())
        original = asyncio.run(store.get("projects/chinook/target/mdl.json"))

        with store.db.transaction() as connection:
            with connection.cursor() as cursor:
                result = rebuild_manifest(cursor, "chinook", actor="test")

        assert result == "unchanged", (
            "recompiling from the stored sources produced different bytes than "
            "`vanna project build` wrote"
        )
        assert asyncio.run(
            store.get("projects/chinook/target/mdl.json")
        ).version == original.version

    def test_a_cube_that_will_not_compile_does_not_get_stored(self, store):
        """The write and the recompile are one transaction, so a cube naming a
        model that does not exist leaves the catalog exactly as it was."""
        config_import.import_files(store, root=config_import.content_root())
        before = self.manifest(store)

        from vanna.core.errors import VannaError

        with pytest.raises(VannaError):
            self.edit_cube(
                store, name="broken", body="base_object: 7\nmeasures: nope\n"
            )

        assert asyncio.run(store.get("projects/chinook/cubes/broken.yml")) is None
        assert self.manifest(store).checksum == before.checksum

    def test_a_workspace_with_no_project_config_is_left_alone(self, store):
        """Nothing to compile, and inventing an empty manifest would take the
        semantic layer away from a workspace that never had one."""
        from vanna_app.config_projection import rebuild_manifest

        with store.db.transaction() as connection:
            with connection.cursor() as cursor:
                assert rebuild_manifest(cursor, "nobody") is None

    def test_starter_questions_follow_the_domain_document(self, store, directory):
        """Authoritative, not additive: a question removed from the document has
        to disappear from the screen, which the pre-catalog `provision` never did."""
        from vanna_app.config_projection import project_all

        asyncio.run(directory.create_tenant("acme", "Acme"))

        def write(starters):
            document = {
                "domains": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "database": "acme",
                        "starters": starters,
                    }
                ]
            }
            asyncio.run(
                store.put(
                    ConfigRecord(
                        relative_path="domains/domains.yml",
                        raw_content=yaml.safe_dump(document, sort_keys=False),
                        parsed=document,
                    ),
                    source="api",
                    actor="admin",
                    project_into=project_all,
                )
            )

        write(["How many orders?", "Revenue by region?"])
        assert [s["question"] for s in asyncio.run(directory.list_starters("acme"))] == [
            "How many orders?",
            "Revenue by region?",
        ]

        write(["Revenue by region?"])
        assert [s["question"] for s in asyncio.run(directory.list_starters("acme"))] == [
            "Revenue by region?"
        ]

    def test_a_domain_for_a_workspace_that_does_not_exist_is_not_invented(self, store):
        """Creating one means encrypting a warehouse URL, which is provisioning's
        job -- not a side effect of saving a file."""
        from vanna_app.config_projection import project_all

        document = {
            "domains": [
                {"id": "ghost", "name": "Ghost", "database": "ghost",
                 "starters": ["anything?"]}
            ]
        }
        asyncio.run(
            store.put(
                ConfigRecord(
                    relative_path="domains/domains.yml",
                    raw_content=yaml.safe_dump(document, sort_keys=False),
                    parsed=document,
                ),
                project_into=project_all,
            )
        )
        assert count_rows(store, "starter_questions") == 0


@pytest.mark.integration
class TestBootstrap:
    """The boot-time import. "Only into an empty catalog" is the whole safety
    property: once an administrator has edited a cube through the API, a restart
    must not quietly reinstate whatever YAML the image was built with."""

    def test_an_empty_catalog_is_filled(self, store):
        report = config_import.bootstrap_if_empty(
            store, root=config_import.content_root()
        )
        assert report is not None and report.ok
        # Compared against what the bootstrap reported creating, rather than a floor
        # tuned to however many projects happened to be on disk that week.
        assert len(asyncio.run(store.list(with_content=False))) == len(report.created)
        assert report.created, "nothing was imported, so nothing is being tested"

    def test_a_populated_catalog_is_left_alone(self, store):
        """Even when the files differ from the rows -- especially then."""
        edited = ConfigRecord(
            relative_path="projects/demo/cubes/sales.yml",
            raw_content="name: sales\nbase_object: invoices\n",
            parsed={"name": "sales", "base_object": "invoices"},
        )
        asyncio.run(store.put(edited, source="api", actor="admin"))

        assert config_import.bootstrap_if_empty(
            store, root=config_import.content_root()
        ) is None
        assert len(asyncio.run(store.list(with_content=False))) == 1
        assert asyncio.run(
            store.get("projects/demo/cubes/sales.yml")
        ).raw_content == edited.raw_content

    def test_the_boot_path_refuses_to_start_on_a_broken_bootstrap(self, store, tmp_path):
        """A refused file means the catalog does not hold what the deployment was
        told to run, and starting anyway would serve a partial configuration."""
        from types import SimpleNamespace

        from vanna_app.wiring import _bootstrap_configuration

        (tmp_path / "projects" / "acme").mkdir(parents=True)
        (tmp_path / "projects" / "acme" / "vanna_project.yml").write_text(
            "name: acme\npassword: hunter2\n", encoding="utf-8"
        )

        settings = SimpleNamespace(
            config_source="database", config_bootstrap=True
        )
        import vanna_app.config_import as module

        original = module.content_root
        module.content_root = lambda override="": tmp_path
        try:
            with pytest.raises(RuntimeError) as caught:
                _bootstrap_configuration(settings, store.db, store)
        finally:
            module.content_root = original
        assert "import_config_files" in str(caught.value)

    def test_it_does_nothing_in_disk_mode(self, store):
        from types import SimpleNamespace

        from vanna_app.wiring import _bootstrap_configuration

        _bootstrap_configuration(
            SimpleNamespace(config_source="disk", config_bootstrap=True),
            store.db,
            store,
        )
        assert asyncio.run(store.list(with_content=False)) == []

    def test_it_does_nothing_when_switched_off(self, store):
        from types import SimpleNamespace

        from vanna_app.wiring import _bootstrap_configuration

        _bootstrap_configuration(
            SimpleNamespace(config_source="database", config_bootstrap=False),
            store.db,
            store,
        )
        assert asyncio.run(store.list(with_content=False)) == []


class TestDiskModeStillRereadsFiles:
    """The cache revalidates against a database query. With no catalog behind it,
    an entry would live for the life of the process -- so editing a YAML file
    under a bind mount would stop taking effect, which is the entire local
    development loop."""

    def loader(self, tmp_path, source="disk"):
        from types import SimpleNamespace

        from vanna_app.config_store import ConfigCache
        from vanna_app.platform import Platform

        stub = SimpleNamespace(
            settings=SimpleNamespace(
                config_source=source,
                projects_dir=str(tmp_path),
                project_dir="",
                default_tenant="demo",
            ),
            config_store=None,
        )
        stub.config_cache = ConfigCache(None, refresh_seconds=60)
        for name in (
            "load_project",
            "_load_project",
            "_load_project_from_disk",
            "_project_dir_for",
        ):
            setattr(stub, name, getattr(Platform, name).__get__(stub))
        stub._log_semantic_layer = Platform._log_semantic_layer
        return stub

    def a_project(self, root, dialect="postgres"):
        (root / "acme").mkdir(parents=True, exist_ok=True)
        (root / "acme" / "vanna_project.yml").write_text(
            f"name: Acme\ndialect: {dialect}\n", encoding="utf-8"
        )
        (root / "acme" / "target").mkdir(exist_ok=True)
        (root / "acme" / "target" / "mdl.json").write_text(
            json.dumps({"models": [], "relationships": [], "cubes": []}),
            encoding="utf-8",
        )

    def test_an_edited_file_is_picked_up(self, tmp_path):
        stub = self.loader(tmp_path)
        self.a_project(tmp_path)

        first, _ = asyncio.run(stub.load_project("acme"))
        assert first.config.dialect == "postgres"

        self.a_project(tmp_path, dialect="mysql")
        second, _ = asyncio.run(stub.load_project("acme"))

        assert second.config.dialect == "mysql", (
            "disk mode served a cached project, so editing the file did nothing"
        )

    def test_a_workspace_with_no_project_directory_has_no_semantic_layer(
        self, tmp_path
    ):
        stub = self.loader(tmp_path)
        assert asyncio.run(stub.load_project("nobody")) == (None, None)
