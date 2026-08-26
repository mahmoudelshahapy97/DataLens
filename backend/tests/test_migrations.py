"""The migration runner.

The schema used to be one ``CREATE TABLE IF NOT EXISTS`` blob replayed on every
boot: idempotent, and unable to change a column, drop one, or say what version a
given deployment is running.

What has to hold:

* the baseline applies to an empty database and is recorded,
* running twice changes nothing,
* two replicas booting together do not race,
* a failing migration leaves the schema untouched rather than half-applied.
"""

from __future__ import annotations

import pytest

from vanna_app.db import SCHEMA
from vanna_app.migrate import current_version, discover, pending, status, upgrade

pytestmark = pytest.mark.integration


class TestDiscovery:
    def test_migrations_are_ordered_and_well_named(self):
        found = discover()
        assert found, "no migrations found"
        assert [m.version for m in found] == sorted(m.version for m in found)
        assert found[0].version == 1

    def test_versions_are_unique(self):
        versions = [m.version for m in discover()]
        assert len(versions) == len(set(versions))


class TestUpgrade:
    def test_a_fresh_database_gets_every_migration(self, app_db):
        # The `app_db` fixture has already run `upgrade`, so this asserts the
        # end state rather than performing it.
        report = status(app_db)
        assert report["up_to_date"]
        assert report["applied"] == len(discover())
        assert current_version(app_db) == max(m.version for m in discover())

    def test_running_again_is_a_no_op(self, app_db):
        assert upgrade(app_db) == []

    def test_pending_is_empty_afterwards(self, app_db):
        assert pending(app_db) == []

    def test_the_ledger_records_what_ran(self, app_db):
        rows = app_db.run_sync(
            f"SELECT version, name, duration_ms FROM {SCHEMA}.schema_migrations ORDER BY version",
            fetch="all",
        )
        assert [r["version"] for r in rows] == [m.version for m in discover()]
        assert all(r["name"] for r in rows)


class TestSchema:
    """Spot-checks that the migrations produced what the application expects.

    Not an exhaustive schema diff -- the application's own tests exercise the
    columns. These are the objects whose absence would fail at runtime in a way that
    is hard to trace back to a migration.
    """

    @pytest.mark.parametrize(
        "table",
        [
            "tenants", "tenant_users", "users", "sessions", "api_tokens",
            "password_resets", "subscriptions", "payments", "conversations",
            "dashboards", "saved_queries", "generations", "counters",
            "audit_events", "admin_audit", "schema_migrations",
        ],
    )
    def test_table_exists(self, app_db, table):
        row = app_db.run_sync(
            "SELECT to_regclass(%s) AS oid", (f"{SCHEMA}.{table}",), fetch="one"
        )
        assert row["oid"] is not None, f"{table} is missing"

    def test_sessions_carry_a_scope(self, app_db):
        """0002. Without it, a temporary password is a full credential."""
        row = app_db.run_sync(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'sessions' AND column_name = 'scope'",
            (SCHEMA,),
            fetch="one",
        )
        assert row is not None

    def test_the_scope_check_constraint_is_enforced(self, app_db):
        import psycopg2

        app_db.run_sync(
            f"INSERT INTO {SCHEMA}.users (email, password_hash) VALUES ('a@b.c', 'x')"
        )
        with pytest.raises(psycopg2.errors.CheckViolation):
            app_db.run_sync(
                f"""INSERT INTO {SCHEMA}.sessions (token_hash, email, expires_at, scope)
                    VALUES ('h', 'a@b.c', now() + interval '1 hour', 'anything')"""
            )

    def test_payment_references_are_unique(self, app_db):
        """The one thing standing between a replayed webhook and a double charge."""
        import psycopg2

        app_db.run_sync(f"INSERT INTO {SCHEMA}.tenants (id, name) VALUES ('acme', 'Acme')")
        app_db.run_sync(
            f"""INSERT INTO {SCHEMA}.payments (tenant_id, provider_ref, amount_cents)
                VALUES ('acme', 'ref-1', 100)"""
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            app_db.run_sync(
                f"""INSERT INTO {SCHEMA}.payments (tenant_id, provider_ref, amount_cents)
                    VALUES ('acme', 'ref-1', 100)"""
            )

    def test_trigram_indexes_exist(self, app_db):
        """0005. Without them history search is a sequential scan of the largest table."""
        rows = app_db.run_sync(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = 'generations'",
            (SCHEMA,),
            fetch="all",
        )
        names = {r["indexname"] for r in rows}
        assert "generations_question_trgm_idx" in names
        assert "generations_sql_trgm_idx" in names

    def test_deleting_a_tenant_keeps_its_generations(self, app_db):
        """Generations are an audit trail; erasure is a separate, explicit act."""
        app_db.run_sync(f"INSERT INTO {SCHEMA}.tenants (id, name) VALUES ('acme', 'Acme')")
        app_db.run_sync(
            f"INSERT INTO {SCHEMA}.generations (id, tenant_id) VALUES ('g1', 'acme')"
        )
        app_db.run_sync(f"DELETE FROM {SCHEMA}.tenants WHERE id = 'acme'")
        row = app_db.run_sync(
            f"SELECT count(*) AS n FROM {SCHEMA}.generations WHERE tenant_id = 'acme'",
            fetch="one",
        )
        assert row["n"] == 1

    def test_deleting_a_tenant_removes_its_members(self, app_db):
        app_db.run_sync(f"INSERT INTO {SCHEMA}.tenants (id, name) VALUES ('acme', 'Acme')")
        app_db.run_sync(
            f"INSERT INTO {SCHEMA}.tenant_users (tenant_id, email) VALUES ('acme', 'a@b.c')"
        )
        app_db.run_sync(f"DELETE FROM {SCHEMA}.tenants WHERE id = 'acme'")
        row = app_db.run_sync(
            f"SELECT count(*) AS n FROM {SCHEMA}.tenant_users WHERE tenant_id = 'acme'",
            fetch="one",
        )
        assert row["n"] == 0
