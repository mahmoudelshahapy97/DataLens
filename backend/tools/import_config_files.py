#!/usr/bin/env python3
"""Load the configuration files under ``backend/`` into the control plane.

    python tools/import_config_files.py                # import everything
    python tools/import_config_files.py --dry-run      # say what would change
    python tools/import_config_files.py --root backend # a different tree
    python tools/import_config_files.py --redact       # mask credential-ish values

This is the one-way bridge from the files to the database. Once
``VANNA_CONFIG_SOURCE=database`` is set, the catalog is what the application runs
and the files are what it was seeded from -- so re-running this is how a change to
a YAML file reaches a running deployment, and *not* re-running it is why an edited
file can appear to have no effect.

Two things it will not do.

**It will not store a credential.** A file with a ``password`` or a
``postgres://user:pass@host`` URL fails the import, naming the key. Warehouse
credentials live in ``tenant_datasources``, encrypted, and a second unencrypted
copy in ``config_files`` would be a credential store nobody thinks of as one.

**It will not silently skip.** A YAML file that does not parse is stored with
``parsed = NULL`` and reported. The exit status is non-zero if anything was
refused or failed, so a deploy step can trust it.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="import_config_files")
    parser.add_argument(
        "--root",
        default="",
        help="Directory the stored paths are relative to (default: the backend/ "
        "tree this checkout ships, found by walking up)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and write nothing",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Store credential-looking values as a placeholder instead of "
        "refusing the file",
    )
    parser.add_argument(
        "--allow-secrets",
        action="store_true",
        help="Store credential-looking values verbatim. Do not use this on a "
        "shared deployment.",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level="WARNING" if args.quiet else "INFO",
                        format="%(levelname)-8s %(message)s")

    from vanna_app.config import get_settings
    from vanna_app.config_import import content_root, describe, import_files
    from vanna_app.config_store import PostgresConfigStore
    from vanna_app.db import build_app_database
    from vanna_app.locks import KEY_CONFIG, advisory_lock
    from vanna_app.migrate import status

    settings = get_settings()
    database = build_app_database(settings)
    if database is None:
        print("No control plane configured (VANNA_APP_DATABASE_URL).", file=sys.stderr)
        return 2

    try:
        report = status(database)
        if not report["up_to_date"]:
            print(
                f"The schema is at {report['current_version']:04d} and "
                f"{len(report['pending'])} migration(s) are pending. Run "
                "`python -m vanna_app.migrate upgrade` first.",
                file=sys.stderr,
            )
            return 2

        store = PostgresConfigStore(database)
        root = pathlib.Path(args.root) if args.root else content_root()

        # The same lock the boot-time bootstrap takes. Two importers running at
        # once would both read a row's version and both write `version + 1`, and
        # one of them would lose its history insert to the unique constraint.
        with advisory_lock(database, KEY_CONFIG):
            outcome = import_files(
                store,
                root=root,
                dry_run=args.dry_run,
                allow_secrets=args.allow_secrets,
                redact_secrets=args.redact,
                actor="import_config_files",
            )
    finally:
        database.close()

    for line in describe(outcome):
        print(line)

    if not outcome.ok:
        print(
            "\nNothing was written for the refused files above. Fix them, or "
            "re-run with --redact.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
