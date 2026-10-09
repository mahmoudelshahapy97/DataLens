#!/usr/bin/env python3
"""Write ``database/`` back out from the control plane.

    python tools/export_sql_schema.py                     # schema + config + reference
    python tools/export_sql_schema.py --full --out ~/dump.sql   # everything, incl. secrets

Two artefacts by default, and the split between them is the point:

    database/sql_schema.sql          every CREATE TABLE, no rows
    database/seed/configuration.sql  config_files + config_versions

Both are safe to commit -- the configuration is the thing this repository is
*for* -- which is why they are the default and why ``--config-only`` is not a flag
anybody has to remember.

Two other groups of rows exist, and neither is in the default output:

``--reference-data`` adds ``seed/reference_data.sql``: business domains, starter
questions, instructions, and the permission matrix. Not secrets, but *not
platform content either* -- a workspace's own business rules and its grants are
written by its administrators, and quietly committing them to a repository is a
decision to make on purpose rather than by default.

``--full`` adds everything: password hashes, token hashes, encrypted warehouse
credentials, and the questions customers asked. It insists on a path outside any
git repository and names the tables it is about to dump.

**The schema is the applied migrations, in order.** Not a reconstruction from
``information_schema``: rebuilding ``CREATE TABLE`` from catalog views loses
partial indexes, expression defaults and constraint names, and the loss is silent
-- you find it when a replay produces a schema that is almost right. The ledger
says exactly which migrations this database ran, so concatenating those files
reproduces it by construction, and the ledger rows are written out too so a
rebuilt database is not migrated a second time.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))

REPO = pathlib.Path(__file__).resolve().parents[2]
DATABASE_DIR = REPO / "database"

#: Written only under ``--reference-data``. No credentials and nothing about a
#: person -- but a workspace's own rules and grants, which belong to whoever
#: administers it rather than to this repository.
REFERENCE_TABLES = (
    "business_domains",
    "domain_tables",
    "starter_questions",
    "instructions",
    "instruction_overrides",
    "instruction_packs_enabled",
    "grant_policies",
    "table_grants",
    "column_grants",
    "table_annotations",
    "column_annotations",
)

#: Never written out without ``--full``, and named in the warning when it is.
SECRET_TABLES = (
    "users",            # password hashes
    "sessions",         # session token hashes
    "api_tokens",       # API token hashes
    "password_resets",  # reset token hashes
    "tenant_datasources",  # encrypted warehouse credentials
    "tenants",          # workspace database URLs
    "generations",      # the questions people asked and the SQL that ran
    "pending_writes",   # proposed changes, with their SQL
    "conversations",    # customer questions
    "saved_queries",    # customer SQL
    "audit_events",
    "admin_audit",
)

DOLLAR_TAG = "$vanna$"


def quote(value: Any) -> str:
    """One SQL literal.

    Text is dollar-quoted rather than escaped: a manifest is JSON full of double
    quotes and a YAML file is full of apostrophes, and doubling quotes in
    kilobytes of content is both unreadable and one missed case away from a file
    that will not replay.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dict, list)):
        import json

        return _dollar(json.dumps(value, ensure_ascii=False))
    if isinstance(value, (bytes, bytearray)):
        return "'\\x" + value.hex() + "'::bytea"
    return _dollar(str(value))


def _dollar(text: str) -> str:
    if DOLLAR_TAG in text:
        # Vanishingly unlikely in YAML or JSON, but a collision would end the
        # literal early and turn the rest of a file into SQL.
        tag = "$vanna_x$"
        while tag in text:
            tag += "x"
        return f"{tag}{text}{tag}"
    return f"{DOLLAR_TAG}{text}{DOLLAR_TAG}"


def schema_sql(database: Any) -> str:
    """Every applied migration, concatenated, plus the ledger that records them."""
    from vanna_app.migrate import discover

    applied = database.run_sync(
        "SELECT version, name, duration_ms FROM vanna_app.schema_migrations "
        "ORDER BY version",
        fetch="all",
    ) or []
    versions = {int(row["version"]) for row in applied}
    on_disk = {m.version: m for m in discover()}

    missing = sorted(versions - set(on_disk))
    if missing:
        raise SystemExit(
            f"The database has applied migration(s) {missing} that are not in "
            "database/migrations. Export from a checkout that has them, or the "
            "output would not rebuild this database."
        )

    parts: List[str] = [
        "-- Generated by tools/export_sql_schema.py. Do not edit.",
        "--",
        "-- The control-plane schema: every migration this database has applied,",
        "-- in order, followed by the ledger rows that record them -- so replaying",
        "-- this file produces a database the migration runner considers current.",
        "--",
        "-- Rows are not here. See database/seed/ for the configuration and",
        "-- reference data, which are the only rows safe to keep in the repository.",
        "",
        "BEGIN;",
        "",
    ]
    for version in sorted(versions):
        migration = on_disk[version]
        parts.append(f"-- ---- {migration.label} " + "-" * (56 - len(migration.label)))
        parts.append(migration.path.read_text(encoding="utf-8").rstrip())
        parts.append("")

    parts.append("-- ---- the ledger " + "-" * 52)
    parts.append(
        "CREATE TABLE IF NOT EXISTS vanna_app.schema_migrations (\n"
        "    version     integer     PRIMARY KEY,\n"
        "    name        text        NOT NULL,\n"
        "    applied_at  timestamptz NOT NULL DEFAULT now(),\n"
        "    duration_ms integer     NOT NULL DEFAULT 0\n"
        ");"
    )
    for row in applied:
        parts.append(
            "INSERT INTO vanna_app.schema_migrations (version, name, duration_ms) "
            f"VALUES ({int(row['version'])}, {quote(row['name'])}, "
            f"{int(row['duration_ms'] or 0)}) ON CONFLICT (version) DO NOTHING;"
        )

    parts += ["", "COMMIT;", ""]
    return "\n".join(parts)


def columns_of(database: Any, table: str) -> List[str]:
    rows = database.run_sync(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'vanna_app' AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
        fetch="all",
    )
    return [str(row["column_name"]) for row in rows or []]


def serial_columns(database: Any, table: str) -> List[str]:
    """Columns whose default draws from a sequence.

    The export writes ids explicitly, so that ``config_versions.config_file_id``
    still points at the right file. That leaves the sequence where a fresh
    database put it -- at 1 -- and the first row inserted after a restore collides
    with the id of the first row restored. So each sequence is set past the data
    it now holds.
    """
    rows = database.run_sync(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'vanna_app' AND table_name = %s "
        "AND column_default LIKE 'nextval%%' ORDER BY ordinal_position",
        (table,),
        fetch="all",
    )
    return [str(row["column_name"]) for row in rows or []]


def _setval(table: str, column: str) -> str:
    """Move one sequence past the rows just inserted.

    A DO block because ``pg_get_serial_sequence`` returns NULL for a column that
    has no sequence in the *target* database, and ``setval(NULL, ...)`` is an
    error -- which would make a restore fail on a schema difference this file
    cannot see.
    """
    return (
        "DO $vanna_seq$ DECLARE s text; BEGIN "
        f"s := pg_get_serial_sequence('vanna_app.{table}', '{column}'); "
        "IF s IS NOT NULL THEN PERFORM setval(s, GREATEST("
        f"coalesce((SELECT max({column}) FROM vanna_app.{table}), 0), 1), true); "
        "END IF; END $vanna_seq$;"
    )


def data_sql(database: Any, tables: Sequence[str], *, title: str) -> str:
    """``INSERT``s for every row of every named table, in the order given.

    ``ON CONFLICT DO NOTHING`` on nothing in particular: the statements are
    plain inserts, and the file is meant for an empty database. Making them
    idempotent would need each table's key, and a seed that half-merges into a
    populated database is a worse thing to offer than one that refuses.
    """
    parts: List[str] = [
        f"-- {title}",
        "-- Generated by tools/export_sql_schema.py. Do not edit.",
        "--",
        "-- Replay into a database that already has the schema:",
        "--     psql -f database/sql_schema.sql -f <this file>",
        "",
        "BEGIN;",
        "",
    ]
    total = 0
    for table in tables:
        names = columns_of(database, table)
        if not names:
            parts.append(f"-- {table}: not in this database, skipped")
            continue
        rows = database.run_sync(
            f"SELECT {', '.join(names)} FROM vanna_app.{table}", fetch="all"
        ) or []
        parts.append(f"-- {table}: {len(rows)} row(s)")
        total += len(rows)
        for row in rows:
            values = ", ".join(quote(row[name]) for name in names)
            parts.append(
                f"INSERT INTO vanna_app.{table} ({', '.join(names)}) "
                f"VALUES ({values});"
            )
        for column in serial_columns(database, table):
            parts.append(_setval(table, column))
        parts.append("")

    parts += ["COMMIT;", ""]
    logging.info("%s: %d row(s) across %d table(s)", title, total, len(tables))
    return "\n".join(parts)


def all_tables(database: Any) -> List[str]:
    rows = database.run_sync(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'vanna_app' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name",
        fetch="all",
    )
    return [str(row["table_name"]) for row in rows or []]


def inside_repository(path: pathlib.Path) -> bool:
    """Whether ``path`` would land in version control.

    Asks git, and falls back to a path comparison when git is not there. Checking
    ``.gitignore`` would not be enough: the refusal is about the file existing in
    a working tree somebody can ``git add -f``, not about whether it is ignored.
    """
    resolved = path.resolve()
    try:
        resolved.relative_to(REPO)
        return True
    except ValueError:
        pass
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=resolved.parent if resolved.parent.exists() else pathlib.Path.cwd(),
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        return False
    if root.returncode != 0:
        return False
    return bool(root.stdout.strip())


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_sql_schema")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include every table, secrets and customer data included. Requires "
        "--out, and refuses a path inside a git repository.",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Where to write. Required with --full; ignored otherwise (the three "
        "committable artefacts go to database/).",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Write database/sql_schema.sql and nothing else",
    )
    parser.add_argument(
        "--reference-data",
        action="store_true",
        help="Also write database/seed/reference_data.sql: business domains, "
        "starter questions, instructions and the permission matrix. No secrets, "
        "but workspace-authored content.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)-8s %(message)s")

    # Before the database is opened, deliberately. The destination check is the
    # guard rail on the option that writes credentials, and a guard rail that only
    # fires once a connection has succeeded is one that does not fire when the
    # database is down -- and says something about connectivity instead of about
    # the dangerous path it was asked for.
    target = _destination(args)
    if isinstance(target, int):
        return target

    from vanna_app.config import get_settings
    from vanna_app.db import build_app_database

    settings = get_settings()
    database = build_app_database(settings)
    if database is None:
        print("No control plane configured (VANNA_APP_DATABASE_URL).", file=sys.stderr)
        return 2

    try:
        if args.full:
            return _export_full(database, target)

        schema = DATABASE_DIR / "sql_schema.sql"
        schema.write_text(schema_sql(database), encoding="utf-8", newline="\n")
        print(f"wrote {schema.relative_to(REPO)}")
        if args.schema_only:
            return 0

        seed = DATABASE_DIR / "seed"
        seed.mkdir(parents=True, exist_ok=True)

        configuration = seed / "configuration.sql"
        configuration.write_text(
            data_sql(
                database,
                ("config_files", "config_versions"),
                title="The configuration catalog",
            ),
            encoding="utf-8", newline="\n",
        )
        print(f"wrote {configuration.relative_to(REPO)}")

        written = {"config_files", "config_versions", "schema_migrations"}
        if args.reference_data:
            reference = seed / "reference_data.sql"
            reference.write_text(
                data_sql(
                    database,
                    REFERENCE_TABLES,
                    title="Reference data. No secrets, but workspace-authored "
                    "rules and grants -- read it before committing it.",
                ),
                encoding="utf-8", newline="\n",
            )
            print(f"wrote {reference.relative_to(REPO)}")
            written |= set(REFERENCE_TABLES)
        else:
            print(
                "\nnot exported, add --reference-data: "
                + ", ".join(sorted(REFERENCE_TABLES))
            )

        skipped = sorted(set(all_tables(database)) - written)
        print(
            "\nnot exported (use --full --out <path outside the repo>): "
            + ", ".join(skipped)
        )
        return 0
    finally:
        database.close()


def _destination(args: Any) -> Any:
    """The path ``--full`` may write to, or an exit code refusing it.

    ``None`` when ``--full`` was not asked for: the three committable artefacts
    have fixed locations and nothing to validate.
    """
    if not args.full:
        return None

    if not args.out:
        print(
            "--full needs --out: it writes password hashes, token hashes, "
            "encrypted warehouse credentials and customer questions, and there is "
            "no safe default location for that.",
            file=sys.stderr,
        )
        return 2

    target = pathlib.Path(args.out).expanduser()
    if inside_repository(target):
        print(
            f"Refusing to write {target} -- it is inside a git repository. A dump "
            "with credentials in it is one `git add -f` away from being permanent, "
            "and deleting the file later does not remove it from history. Choose a "
            "path outside any checkout.",
            file=sys.stderr,
        )
        return 2
    return target


def _export_full(database: Any, target: pathlib.Path) -> int:
    tables = all_tables(database)
    present = [t for t in SECRET_TABLES if t in tables]
    print(
        "This dump contains secrets and customer data from: "
        + ", ".join(present)
        + "\nTreat the file as a credential.",
        file=sys.stderr,
    )

    ordered = _dependency_order(database, tables)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        schema_sql(database)
        + "\n"
        + data_sql(database, ordered, title="Every table (contains secrets)"),
        encoding="utf-8", newline="\n",
    )
    print(f"wrote {target}")
    return 0


def _dependency_order(database: Any, tables: Sequence[str]) -> List[str]:
    """Tables ordered so a foreign key never points at a row that is not there yet.

    A topological sort over ``information_schema``'s foreign keys. A cycle -- which
    this schema does not have -- degrades to "whatever order is left", because
    refusing to export is a worse answer than an insert that needs its constraints
    deferred.
    """
    rows = database.run_sync(
        """
        SELECT tc.table_name AS child, ccu.table_name AS parent
          FROM information_schema.table_constraints tc
          JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
           AND ccu.table_schema = tc.table_schema
         WHERE tc.constraint_type = 'FOREIGN KEY'
           AND tc.table_schema = 'vanna_app'
        """,
        fetch="all",
    ) or []
    parents: Dict[str, set] = {t: set() for t in tables}
    for row in rows:
        child, parent = str(row["child"]), str(row["parent"])
        if child in parents and parent in parents and child != parent:
            parents[child].add(parent)

    ordered: List[str] = []
    remaining = dict(parents)
    while remaining:
        ready = sorted(t for t, needs in remaining.items() if not (needs - set(ordered)))
        if not ready:  # a cycle; emit the rest in a stable order
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        for table in ready:
            remaining.pop(table)
    return ordered


if __name__ == "__main__":
    raise SystemExit(main())
