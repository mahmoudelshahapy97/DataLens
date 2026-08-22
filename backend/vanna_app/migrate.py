"""Versioned schema migrations.

The schema used to be one ``CREATE TABLE IF NOT EXISTS`` blob re-executed on every
boot, with ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` appended as the schema grew.
That is idempotent, which is genuinely useful, and it is also the end of the road:
it cannot change a column's type, drop one, backfill a value, or tell you what a
given deployment is running. The first migration that is not "add a nullable
column" has nowhere to live.

This is the smallest thing that fixes that: numbered ``.sql`` files, a
``schema_migrations`` table recording which have run, and a runner that applies the
missing ones in order. No new dependency, no second description of the schema.

Two properties matter.

**Concurrency.** Several API replicas boot at once. Each takes a PostgreSQL
advisory lock before looking at the ledger, so exactly one applies and the others
wait and then find nothing to do. Without it, two replicas race and the loser
crashes on a duplicate object.

**Atomicity per migration.** Each file runs inside its own transaction together
with the ledger insert, so a migration either happened and is recorded, or neither.
A half-applied migration recorded as complete is the failure mode that makes people
distrust migrations, and it is entirely avoidable in PostgreSQL, which has
transactional DDL.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, List, NamedTuple, Optional, Sequence

from .db import SCHEMA
from .locks import KEY_MIGRATE

logger = logging.getLogger("vanna.migrate")


def _migrations_root() -> Path:
    """Where the ``.sql`` files live.

    They are not inside this package. The schema is the database's, not the
    application's: it is reviewed by people looking at ``database/``, applied by a
    deploy job as often as by this process, and read by anything else that needs to
    know what shape the control plane is in. Keeping it under ``vanna_app/`` said
    the opposite.

    Resolved by walking up rather than by a fixed number of ``..``, because the
    package sits at a different depth in each layout -- ``backend/vanna_app/`` in a
    checkout, ``/app/vanna_app/`` in the image -- and ``parents[2]`` is silently
    wrong in one of them. ``VANNA_MIGRATIONS_DIR`` overrides it for a deployment
    that mounts its own.
    """
    override = (os.getenv("VANNA_MIGRATIONS_DIR") or "").strip()
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "database" / "migrations"
        if candidate.is_dir():
            return candidate
    # Nothing found: return the checkout-relative path so the error names something
    # a reader can act on rather than a path assembled from a failed search.
    return here.parents[2] / "database" / "migrations"


MIGRATIONS_DIR = _migrations_root()

#: The migration lock. Declared in ``locks`` with every other key, so two callers
#: cannot pick the same number by accident.
_LOCK_KEY = KEY_MIGRATE

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

LEDGER_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migrations (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    -- How long it took. Worth keeping: the first thing anyone wants to know
    -- before running a migration in production is how long it took in staging.
    duration_ms integer     NOT NULL DEFAULT 0
);
"""


class Migration(NamedTuple):
    version: int
    name: str
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Every migration on disk, in order.

    A file that does not match ``NNNN_name.sql`` is an error rather than a skip: a
    migration silently ignored because of a typo in its filename is a schema
    difference nobody will find until it breaks something.
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"No migrations directory at {directory}")

    found: List[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME.match(path.name)
        if not match:
            raise ValueError(
                f"{path.name} is not a valid migration filename. "
                "Use NNNN_lowercase_with_underscores.sql"
            )
        found.append(Migration(int(match.group(1)), match.group(2), path))

    versions = [m.version for m in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise ValueError(
            f"Duplicate migration version(s): {sorted(duplicates)}. Two people "
            "numbered a migration the same; renumber the later one."
        )
    return found


def applied_versions(db: Any) -> List[int]:
    """Versions already recorded as applied."""
    db.run_sync(LEDGER_DDL)
    rows = db.run_sync(
        f"SELECT version FROM {SCHEMA}.schema_migrations ORDER BY version", fetch="all"
    )
    return [int(row["version"]) for row in rows or []]


def pending(db: Any, directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    done = set(applied_versions(db))
    return [m for m in discover(directory) if m.version not in done]


def upgrade(db: Any, directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Apply every pending migration. Returns the ones that ran.

    Safe to call on every boot and from several processes at once.
    """
    import time

    with db.transaction() as connection:
        with connection.cursor() as cursor:
            # Session-level lock: held until we release it or the connection ends,
            # which covers the whole run rather than one statement.
            cursor.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))

        try:
            with connection.cursor() as cursor:
                cursor.execute(LEDGER_DDL)
            connection.commit()

            with connection.cursor() as cursor:
                cursor.execute(f"SELECT version FROM {SCHEMA}.schema_migrations")
                done = {int(row[0]) for row in cursor.fetchall()}

            ran: List[Migration] = []
            for migration in discover(directory):
                if migration.version in done:
                    continue

                logger.info("Applying migration %s", migration.label)
                started = time.monotonic()
                sql = migration.path.read_text(encoding="utf-8")
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(sql)
                        elapsed = int((time.monotonic() - started) * 1000)
                        cursor.execute(
                            f"""INSERT INTO {SCHEMA}.schema_migrations
                                    (version, name, duration_ms)
                                VALUES (%s, %s, %s)""",
                            (migration.version, migration.name, elapsed),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    logger.error(
                        "Migration %s failed and was rolled back. The schema is "
                        "unchanged; fix the migration and restart.",
                        migration.label,
                    )
                    raise
                ran.append(migration)
                logger.info("Applied %s in %dms", migration.label, elapsed)

            if not ran:
                logger.info("Schema is up to date (%d migration(s) applied).", len(done))
            return ran
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            connection.commit()


def current_version(db: Any) -> int:
    """The highest applied version, or 0 on a fresh database."""
    versions = applied_versions(db)
    return max(versions) if versions else 0


def status(db: Any, directory: Path = MIGRATIONS_DIR) -> dict:
    """A summary for the readiness probe and the CLI."""
    done = set(applied_versions(db))
    all_migrations = discover(directory)
    outstanding = [m for m in all_migrations if m.version not in done]
    return {
        "current_version": max(done) if done else 0,
        "latest_version": max((m.version for m in all_migrations), default=0),
        "applied": len(done),
        "pending": [m.label for m in outstanding],
        "up_to_date": not outstanding,
    }


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vanna_app.migrate [status|upgrade]``.

    Exists so migrations can run as a deploy job rather than at boot -- which is
    what ``VANNA_AUTO_MIGRATE=false`` is for, and what you want the moment more
    than one replica starts at a time.
    """
    import argparse
    import sys

    from .config import load_settings
    from .db import AppDatabase

    parser = argparse.ArgumentParser(prog="vanna-app-migrate")
    parser.add_argument("command", choices=("status", "upgrade"), nargs="?", default="upgrade")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)-8s %(message)s")

    settings = load_settings()
    if not settings.app_database_url:
        print("VANNA_APP_DATABASE_URL is not set; nothing to migrate.", file=sys.stderr)
        return 2

    db = AppDatabase(settings.app_database_url, minconn=1, maxconn=2)
    try:
        if args.command == "status":
            report = status(db)
            print(f"current: {report['current_version']:04d}")
            print(f"latest:  {report['latest_version']:04d}")
            for label in report["pending"]:
                print(f"pending: {label}")
            return 0 if report["up_to_date"] else 1

        ran = upgrade(db)
        for migration in ran:
            print(f"applied: {migration.label}")
        if not ran:
            print("already up to date")
        return 0
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
