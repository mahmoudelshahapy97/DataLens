"""Provisioning workspaces from ``domains/domains.yml``.

One workspace per seeded database, each bound to its own data source and loaded with
the business rules that database needs. It is what turns eight empty schemas into
eight workspaces somebody can actually ask questions of, and it is the fastest way to
exercise multi-tenancy with real data rather than one demo table.

Run it against a running stack:

    python -m vanna_app.domains provision          # create or update everything
    python -m vanna_app.domains provision --only chinook northwind
    python -m vanna_app.domains list               # what is defined, what exists

**Idempotent.** Creating a workspace that exists updates it; adding a rule whose text
is already present is skipped. Running it twice is a no-op, which is what makes it
safe to put in a deploy script.

It talks to the control plane directly rather than over HTTP. The alternative needs a
session, a CSRF token and a platform-admin account before it can do anything, which
is a lot of ceremony for a job that runs on the same host with the same database URL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("vanna.domains")


def _definitions_path() -> Path:
    """Where the definitions live.

    Outside the package, because they are content to be reviewed in a diff rather
    than code -- the same argument ``instructions/`` makes.

    Found by walking up, not by a fixed ``parents[2]``, because the package sits at
    a different depth in each layout: ``backend/vanna_app/`` in a checkout puts the
    file at ``backend/domains/domains.yml``, and ``/app/vanna_app/`` in the image
    puts it at ``/app/domains/domains.yml``. ``parents[2]`` resolved to ``/`` in
    the image and looked for ``/domains/domains.yml``, so provisioning failed with
    a path nobody had ever written down. ``VANNA_DOMAINS_FILE`` overrides it.
    """
    override = (os.getenv("VANNA_DOMAINS_FILE") or "").strip()
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "domains" / "domains.yml"
        if candidate.is_file():
            return candidate
    return here.parents[1] / "domains" / "domains.yml"


DEFINITIONS = _definitions_path()


def load_definitions(path: Path = DEFINITIONS) -> List[Dict[str, Any]]:
    """Read and validate the domain file."""
    import yaml

    if not path.is_file():
        raise FileNotFoundError(f"No domain definitions at {path}")

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return validate_definitions(document, where=str(path))


def validate_definitions(document: Any, *, where: str) -> List[Dict[str, Any]]:
    """Check an already-parsed domain document and return its domains.

    Validation is not decoration: a typo in a database name produces a workspace
    bound to a database that does not exist, which fails later, per question, as a
    connection error nobody traces back to here.

    Taken apart from the file read so the catalog gets the same checks. The
    document is the same either way -- only where it was read from differs.
    """
    domains = (document or {}).get("domains") or []
    if not domains:
        raise ValueError(f"{where} defines no domains")

    seen = set()
    for domain in domains:
        for field in ("id", "name", "database"):
            if not domain.get(field):
                raise ValueError(f"A domain is missing {field!r}: {domain}")
        if domain["id"] in seen:
            raise ValueError(f"Duplicate domain id {domain['id']!r}")
        seen.add(domain["id"])

        for rule in domain.get("instructions") or []:
            if not str(rule.get("text") or "").strip():
                raise ValueError(f"{domain['id']}: an instruction has no text")

    return domains


async def read_definitions(
    settings: Any, database: Any = None, *, path: Path = DEFINITIONS
) -> List[Dict[str, Any]]:
    """The domain definitions, from wherever this deployment keeps them.

    With ``VANNA_CONFIG_SOURCE=database`` this reads the catalog, so provisioning
    applies what the deployment is actually running rather than what happens to be
    in the image -- which are different things the moment somebody edits a domain
    through the API.
    """
    if settings.config_source != "database" or database is None:
        return load_definitions(path)

    from .config_store import KIND_DOMAIN, PostgresConfigStore

    rows = await PostgresConfigStore(database).list(kind=KIND_DOMAIN)
    if not rows:
        raise FileNotFoundError(
            "VANNA_CONFIG_SOURCE=database, but the catalog holds no domain "
            "definitions. Import them with `python backend/tools/import_config_files.py`."
        )
    if rows[0].parsed is None:
        raise ValueError(
            f"{rows[0].relative_path} is stored in the catalog but could not be "
            "parsed. Fix the file and re-import it."
        )
    return validate_definitions(rows[0].parsed, where=rows[0].relative_path)


def database_url_for(domain: Dict[str, Any], template: str) -> str:
    """The connection URL for one domain's database.

    Derived from the deployment's own ``VANNA_DATABASE_URL`` by swapping the database
    name, so credentials and host are configured in exactly one place.
    """
    from urllib.parse import urlsplit, urlunsplit

    explicit = domain.get("database_url")
    if explicit:
        return str(explicit)

    parts = urlsplit(template)
    if not parts.scheme:
        raise ValueError(
            "VANNA_DATABASE_URL is not set, so per-domain URLs cannot be derived. "
            "Set it, or give each domain an explicit database_url."
        )
    return urlunsplit((parts.scheme, parts.netloc, f"/{domain['database']}", "", ""))


# ----------------------------------------------------------------------
# Provisioning
# ----------------------------------------------------------------------


async def provision(
    settings: Any,
    *,
    only: Optional[Sequence[str]] = None,
    path: Path = DEFINITIONS,
) -> Dict[str, Any]:
    """Create or update every defined workspace. Returns a summary."""
    from vanna.capabilities.knowledge import Instruction, InstructionScope
    from vanna.core.tool import ToolContext
    from vanna.core.user import User

    from .db import build_app_database
    from .secrets import Cipher
    from .tenancy import Directory

    database = build_app_database(settings)
    if database is None:
        raise RuntimeError("No control plane configured; nothing to provision into.")

    # Read inside the try, so a definition problem still closes the pool it just
    # opened -- the definitions now come *from* the database, so the read can
    # itself fail.
    directory = Directory(database, Cipher(settings.secret_key))

    # Rules go wherever the running app reads them from. With a control plane
    # that is the database -- writing them to files here would leave every
    # provisioned rule somewhere nothing looks.
    from .instruction_store import PostgresInstructionStore

    instructions: Any = PostgresInstructionStore(database)

    summary: Dict[str, Any] = {"created": [], "updated": [], "rules": 0, "starters": 0}

    try:
        domains = await read_definitions(settings, database, path=path)
        if only:
            wanted = set(only)
            unknown = wanted - {d["id"] for d in domains}
            if unknown:
                raise ValueError(f"Unknown domain(s): {', '.join(sorted(unknown))}")
            domains = [d for d in domains if d["id"] in wanted]

        for domain in domains:
            tenant_id = domain["id"]
            url = database_url_for(domain, settings.database_url)

            existing = await directory.get_tenant(tenant_id)
            if existing is None:
                await directory.create_tenant(
                    tenant_id,
                    domain["name"],
                    description=domain.get("description", ""),
                    database_url=url,
                )
                summary["created"].append(tenant_id)
                logger.info("Created workspace %s -> %s", tenant_id, domain["database"])
            else:
                await directory.update_tenant(
                    tenant_id,
                    {
                        "name": domain["name"],
                        "description": domain.get("description", ""),
                        "database_url": url,
                    },
                )
                summary["updated"].append(tenant_id)
                logger.info("Updated workspace %s", tenant_id)

            # Every platform admin is a member, so somebody can actually open it.
            for email in sorted(settings.admin_emails) or ["demo@example.com"]:
                await directory.add_user(tenant_id, email, role="admin")

            summary["rules"] += await _load_rules(
                instructions, tenant_id, domain, Instruction, InstructionScope, ToolContext, User
            )
            summary["starters"] += await _load_starters(directory, tenant_id, domain)
    finally:
        database.close()

    return summary


async def _load_rules(
    store: Any,
    tenant_id: str,
    domain: Dict[str, Any],
    Instruction: Any,
    InstructionScope: Any,
    ToolContext: Any,
    User: Any,
) -> int:
    """Add this domain's business rules, skipping any already present.

    Compared on text rather than on an id: the file is the source of truth and has no
    ids, so "already there" can only mean "says the same thing". Editing a rule's
    wording therefore adds a new one -- which is the right default for a knowledge
    base somebody curates, and is why removal is a deliberate act in the console
    rather than something this script does.
    """
    # ToolContext requires a real AgentMemory -- it is a pydantic model and None
    # fails validation. The stores never touch it; the platform's own partitioned
    # in-memory one is the cheapest thing that satisfies the contract.
    from .platform import _build_memory

    context = ToolContext(
        user=User(id="provisioner", tenant_id=tenant_id, group_memberships=["admin"]),
        conversation_id="provision",
        request_id=f"provision:{tenant_id}",
        tenant_id=tenant_id,
        agent_memory=_build_memory(),
    )

    try:
        current = {i.text.strip() for i in await store.list_all(context)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read existing rules for %s: %s", tenant_id, exc)
        current = set()

    added = 0
    for rule in domain.get("instructions") or []:
        text = " ".join(str(rule["text"]).split())
        if text in current:
            continue
        from vanna.capabilities.knowledge import InstructionOrigin

        await store.add(
            context,
            Instruction(
                text=text,
                scope=InstructionScope.GLOBAL,
                priority=int(rule.get("priority", 0)),
                # Marked as coming from a curated set rather than typed by an
                # admin, so the console can say where a rule came from and a
                # later `provision` run can tell its own rules from theirs.
                origin=InstructionOrigin.LIBRARY,
                source_pack=f"domain:{tenant_id}",
                created_by="provisioner",
            ),
        )
        added += 1

    if added:
        logger.info("Added %d business rule(s) to %s", added, tenant_id)
    return added


async def _load_starters(directory: Any, tenant_id: str, domain: Dict[str, Any]) -> int:
    """Add this domain's starter questions, skipping duplicates."""
    existing = {s["question"].strip() for s in await directory.list_starters(tenant_id)}
    added = 0
    for order, question in enumerate(domain.get("starters") or []):
        question = " ".join(str(question).split())
        if question in existing:
            continue
        await directory.add_starter(tenant_id, question, order)
        added += 1
    return added


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


async def _definitions_for_listing(settings: Any) -> List[Dict[str, Any]]:
    """For the CLI's `list`: the catalog when that is the source, else the file."""
    if settings.config_source != "database":
        return load_definitions()

    from .db import build_app_database

    database = build_app_database(settings)
    try:
        return await read_definitions(settings, database)
    finally:
        if database is not None:
            database.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="vanna-app-domains")
    parser.add_argument("command", choices=("provision", "list"), nargs="?", default="list")
    parser.add_argument("--only", nargs="*", help="Provision only these domain ids")
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)-8s %(message)s")

    from .config import get_settings

    settings = get_settings()

    if args.command == "list":
        for domain in asyncio.run(_definitions_for_listing(settings)):
            rules = len(domain.get("instructions") or [])
            starters = len(domain.get("starters") or [])
            print(
                f"{domain['id']:12} {domain['database']:12} "
                f"{rules:2} rules  {starters} starters   {domain['name']}"
            )
        return 0

    summary = asyncio.run(provision(settings, only=args.only))

    print(f"created:  {', '.join(summary['created']) or 'none'}")
    print(f"updated:  {', '.join(summary['updated']) or 'none'}")
    print(f"rules:    {summary['rules']} added")
    print(f"starters: {summary['starters']} added")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
