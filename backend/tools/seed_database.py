#!/usr/bin/env python3
"""Bring a control plane up to date: migrate, import the configuration, export.

    python tools/seed_database.py                # migrate + import + export
    python tools/seed_database.py --no-export    # leave database/ alone
    python tools/seed_database.py --dry-run      # say what would change

One command, because the three steps are only ever useful together and doing them
in the wrong order is the mistake worth designing out: importing before migrating
fails on a missing table, and exporting before importing writes a seed of the old
configuration over the new one.

It is safe to run repeatedly. Migrations skip what is applied, the import is
checksum-gated, and the export is regenerated from whatever the database now
holds.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))
# ...and this directory, so the export step is an import rather than a subprocess.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed_database")
    parser.add_argument("--root", default="", help="Configuration tree to import")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report the import; write nothing"
    )
    parser.add_argument(
        "--no-export", action="store_true", help="Skip regenerating database/"
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Mask credential-looking values rather than refusing the file",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)-8s %(message)s")

    from vanna_app.config import get_settings
    from vanna_app.config_import import content_root, describe, import_files
    from vanna_app.config_store import PostgresConfigStore
    from vanna_app.db import build_app_database
    from vanna_app.locks import KEY_CONFIG, advisory_lock
    from vanna_app.migrate import upgrade

    settings = get_settings()
    database = build_app_database(settings)
    if database is None:
        print("No control plane configured (VANNA_APP_DATABASE_URL).", file=sys.stderr)
        return 2

    try:
        print("== migrate")
        if args.dry_run:
            from vanna_app.migrate import status

            report = status(database)
            for label in report["pending"]:
                print(f"pending: {label}")
            if not report["pending"]:
                print("already up to date")
        else:
            ran = upgrade(database)
            for migration in ran:
                print(f"applied: {migration.label}")
            if not ran:
                print("already up to date")

        print("\n== import configuration")
        store = PostgresConfigStore(database)
        with advisory_lock(database, KEY_CONFIG):
            report = import_files(
                store,
                root=pathlib.Path(args.root) if args.root else content_root(),
                dry_run=args.dry_run,
                redact_secrets=args.redact,
                actor="seed_database",
            )
        for line in describe(report):
            print(line)
        if not report.ok:
            print(
                "\nThe import refused or failed on the files above; not exporting.",
                file=sys.stderr,
            )
            return 1
    finally:
        database.close()

    if args.no_export or args.dry_run:
        return 0

    print("\n== export database/")
    from export_sql_schema import main as export

    return export([])


if __name__ == "__main__":
    raise SystemExit(main())
