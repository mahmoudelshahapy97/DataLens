"""Writing ``database/`` back out, and refusing to write it somewhere dangerous.

Two things are being tested, and only one of them is about SQL.

**The artefact replays.** A schema file that almost rebuilds the database is worse
than none: the failure shows up as a subtly different schema, not as an error. So
the export is fed into a genuinely empty database and the result is compared with
what it came from.

**The dangerous option is hard to fire.** ``--full`` writes password hashes, token
hashes, encrypted warehouse credentials and the questions customers asked. A
``.gitignore`` entry is not protection -- ``git add -f`` exists, and content that
reaches git history once is there for good -- so the tool refuses a path inside any
repository and says which tables it is about to dump.
"""

from __future__ import annotations

import pathlib
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))

import export_sql_schema as exporter  # noqa: E402

from vanna_app import config_import  # noqa: E402
from vanna_app.config_store import PostgresConfigStore  # noqa: E402


class TestQuoting:
    """A manifest is JSON full of double quotes and YAML is full of apostrophes.
    Doubling quotes across kilobytes of that is one missed case away from a file
    that will not replay, so the content is dollar-quoted instead."""

    def test_text_is_dollar_quoted(self):
        assert exporter.quote("it's a 'quote'") == "$vanna$it's a 'quote'$vanna$"

    def test_json_survives_unescaped(self):
        assert exporter.quote({"a": 'say "hi"'}) == '$vanna$1$vanna$'.replace(
            "1", '{"a": "say \\"hi\\""}'
        )

    def test_a_colliding_tag_is_moved_out_of_the_way(self):
        """Vanishingly unlikely, and it would end the literal early and turn the
        rest of a configuration file into SQL."""
        quoted = exporter.quote("a $vanna$ b")
        assert quoted.startswith("$vanna_x$") and quoted.endswith("$vanna_x$")

    def test_none_is_null_not_the_word(self):
        assert exporter.quote(None) == "NULL"

    def test_booleans_and_numbers_are_not_quoted(self):
        assert exporter.quote(True) == "true"
        assert exporter.quote(7) == "7"


def _help() -> str:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
        exporter.main(["--help"])
    return buffer.getvalue()


class TestWhereItRefusesToWrite:
    def test_the_repository_is_off_limits(self):
        assert exporter.inside_repository(exporter.REPO / "database" / "dump.sql")

    def test_full_without_a_destination_is_refused(self, capsys):
        assert exporter.main(["--full"]) == 2
        assert "needs --out" in capsys.readouterr().err

    def test_full_into_the_repository_is_refused(self, capsys):
        target = exporter.REPO / "database" / "everything.sql"
        assert exporter.main(["--full", "--out", str(target)]) == 2
        assert "inside a git repository" in capsys.readouterr().err
        assert not target.exists(), "it wrote the file it said it refused to write"

    def test_reference_data_is_not_written_by_default(self, capsys, monkeypatch):
        """No credentials in it, but a workspace's own rules and grants are its
        administrators' content, not this repository's."""
        monkeypatch.setenv("VANNA_APP_DATABASE_URL", "")
        assert exporter.main([]) == 2
        # It got as far as needing a database, so the destination guard passed and
        # nothing about reference data was in the way -- the flag is what adds it.
        assert "--reference-data" in _help()

    def test_the_secret_tables_are_named(self):
        """So that a reader of the warning knows what they now have to protect."""
        assert "tenant_datasources" in exporter.SECRET_TABLES
        assert "users" in exporter.SECRET_TABLES
        assert not set(exporter.REFERENCE_TABLES) & set(exporter.SECRET_TABLES)


@pytest.mark.integration
class TestTheExportRebuildsTheDatabase:
    def test_schema_and_configuration_replay_into_an_empty_database(
        self, app_db, database_url
    ):
        import psycopg2

        store = PostgresConfigStore(app_db)
        report = config_import.import_files(store, root=config_import.content_root())
        expected = len(store.list_sync(with_content=False))
        # Derived from what the importer actually walked, not a hard-coded floor: a
        # threshold tuned to the tree of the day fails the moment a project is added
        # or removed, which says nothing about the export.
        assert report.ok, report.summary()
        assert expected == len(report.created), report.summary()
        assert expected, "nothing was imported, so nothing is being tested"

        schema = exporter.schema_sql(app_db)
        data = exporter.data_sql(
            app_db, ("config_files", "config_versions"), title="test"
        )

        name = f"vanna_replay_{uuid.uuid4().hex[:12]}"
        parts = urlsplit(database_url)
        admin = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
        target = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

        connection = psycopg2.connect(admin, connect_timeout=10)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{name}"')
        connection.close()

        try:
            replayed = psycopg2.connect(target, connect_timeout=10)
            replayed.autocommit = True
            try:
                with replayed.cursor() as cursor:
                    cursor.execute(schema)
                    cursor.execute(data)
                    cursor.execute("SELECT count(*) FROM vanna_app.config_files")
                    assert cursor.fetchone()[0] == expected
                    # The ledger came across, so the migration runner considers
                    # this database current rather than replaying every file.
                    cursor.execute(
                        "SELECT count(*) FROM vanna_app.schema_migrations"
                    )
                    assert cursor.fetchone()[0] > 0
                    # And the content is byte-identical, which is the property the
                    # raw_content column exists for.
                    cursor.execute(
                        "SELECT raw_content FROM vanna_app.config_files "
                        "WHERE relative_path = 'projects/chinook/target/mdl.json'"
                    )
                    stored = cursor.fetchone()[0]
                original = (
                    config_import.content_root()
                    / "projects" / "chinook" / "target" / "mdl.json"
                ).read_text(encoding="utf-8")
                assert stored == original
            finally:
                replayed.close()
        finally:
            connection = psycopg2.connect(admin, connect_timeout=10)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
            connection.close()

    def test_the_schema_export_refuses_to_lie(self, app_db):
        """A database that applied a migration this checkout does not have cannot
        be exported: the file would rebuild a *different* schema, and silently."""
        app_db.run_sync(
            "INSERT INTO vanna_app.schema_migrations (version, name) "
            "VALUES (9999, 'from_the_future')"
        )
        with pytest.raises(SystemExit) as caught:
            exporter.schema_sql(app_db)
        assert "9999" in str(caught.value)

    def test_a_restored_database_can_still_insert(self, app_db, database_url):
        """The ids are written explicitly, so `config_versions.config_file_id`
        still points at the right file. Without moving the sequence past them, the
        first row written after a restore collides with the first row restored."""
        import psycopg2

        store = PostgresConfigStore(app_db)
        config_import.import_files(store, root=config_import.content_root())

        schema = exporter.schema_sql(app_db)
        data = exporter.data_sql(
            app_db, ("config_files", "config_versions"), title="test"
        )
        assert "setval" in data

        name = f"vanna_seq_{uuid.uuid4().hex[:12]}"
        parts = urlsplit(database_url)
        admin = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
        target = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

        connection = psycopg2.connect(admin, connect_timeout=10)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{name}"')
        connection.close()

        try:
            restored = psycopg2.connect(target, connect_timeout=10)
            restored.autocommit = True
            try:
                with restored.cursor() as cursor:
                    cursor.execute(schema)
                    cursor.execute(data)
                    # The highest restored id, read rather than assumed: the property
                    # is that the sequence was moved past it, and a hard-coded number
                    # only tests that by coincidence.
                    cursor.execute("SELECT max(id) FROM vanna_app.config_files")
                    highest = cursor.fetchone()[0]
                    assert highest, "nothing was restored, so nothing is being tested"
                    cursor.execute(
                        "INSERT INTO vanna_app.config_files "
                        "(scope, relative_path, kind, checksum, raw_content) "
                        "VALUES ('global', 'after/restore.yml', 'other', 'x', 'y') "
                        "RETURNING id"
                    )
                    assert cursor.fetchone()[0] > highest
            finally:
                restored.close()
        finally:
            connection = psycopg2.connect(admin, connect_timeout=10)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
            connection.close()

    def test_reference_data_carries_no_secret_table(self, app_db):
        sql = exporter.data_sql(
            app_db, exporter.REFERENCE_TABLES, title="reference"
        )
        for table in exporter.SECRET_TABLES:
            assert f"INTO vanna_app.{table} " not in sql
