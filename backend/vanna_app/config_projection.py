"""Keeping the derived configuration in step with the canonical rows.

``config_files`` is the canonical record. Some of what the runtime reads is
*derived* from it, and the deriving has to happen inside the write's transaction
-- otherwise a failure between the two leaves the catalog and the derived data
disagreeing, with nothing to say which one is right.

Two projections today.

**The manifest.** ``target/mdl.json`` is a build output: the compiled form of
``vanna_project.yml``, ``models/``, ``relationships.yml`` and ``cubes/``. The
runtime reads only the compiled form, so editing a cube and leaving the manifest
alone changes *nothing* -- the most confusing possible outcome of a successful
save. Writing a cube therefore recompiles the manifest, through the same
:func:`~vanna.semantic.manifest_from_documents` the file-based
``vanna project build`` uses.

**Starter questions.** ``domains.yml`` carries the questions a workspace opens
with; ``starter_questions`` is the table the UI reads. Workspaces that do not
exist yet are left alone: creating one means encrypting a warehouse URL and is
``vanna_app.domains provision``'s job, not a side effect of saving a file.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from .config_store import (
    KIND_CUBE,
    KIND_DOMAIN,
    KIND_MANIFEST,
    KIND_MODEL,
    KIND_MODEL_SQL,
    KIND_PROJECT_CONFIG,
    KIND_RELATIONSHIPS,
    KIND_VIEW,
    SCHEMA,
    SCOPE_PROJECT,
    ConfigRecord,
    apply_put,
)

logger = logging.getLogger("vanna.config_projection")

#: Editing any of these changes what the compiled manifest should say.
MANIFEST_SOURCES = frozenset(
    {
        KIND_PROJECT_CONFIG,
        KIND_MODEL,
        KIND_MODEL_SQL,
        KIND_RELATIONSHIPS,
        KIND_CUBE,
        KIND_VIEW,
    }
)


def project_all(cursor: Any, record: ConfigRecord, *, actor: Optional[str] = None) -> None:
    """Bring everything derived from ``record`` up to date, on this cursor.

    Passed to :meth:`PostgresConfigStore.put` as ``project_into``. Ordinary
    exceptions propagate and roll the write back, which is the intended
    behaviour: a cube that cannot be compiled has not been saved.
    """
    if record.kind in MANIFEST_SOURCES:
        rebuild_manifest(cursor, record.tenant_id, actor=actor)
    if record.kind == KIND_DOMAIN:
        project_starters(cursor, record)


# ----------------------------------------------------------------------
# The manifest
# ----------------------------------------------------------------------


def rebuild_manifest(
    cursor: Any, tenant_id: str, *, actor: Optional[str] = None
) -> Optional[str]:
    """Recompile one workspace's manifest from its stored sources.

    Reads through the *same cursor* as the write that triggered it, so it sees the
    row that has just been written and not the committed state without it.

    Returns what happened to the manifest row, or None when the workspace has no
    project config -- in which case there is nothing to compile and nothing that
    should be invented.
    """
    from vanna.semantic import manifest_from_documents

    cursor.execute(
        f"""SELECT relative_path, kind, raw_content, parsed
              FROM {SCHEMA}.config_files
             WHERE scope = %s AND tenant_id = %s
             ORDER BY relative_path""",
        (SCOPE_PROJECT, tenant_id),
    )
    rows = cursor.fetchall()

    config: Optional[Dict[str, Any]] = None
    models: List[Tuple[str, Dict[str, Any]]] = []
    ref_sql: Dict[str, str] = {}
    cubes: List[Tuple[str, Dict[str, Any]]] = []
    views: List[Tuple[str, str]] = []
    relationships: Dict[str, Any] = {}

    for path, kind, raw, parsed in rows:
        if kind == KIND_PROJECT_CONFIG:
            config = parsed or {}
        elif kind == KIND_MODEL:
            models.append((_model_name(path), dict(parsed or {})))
        elif kind == KIND_MODEL_SQL:
            text = (raw or "").strip()
            if text:
                ref_sql[_model_name(path)] = text
        elif kind == KIND_RELATIONSHIPS:
            relationships = parsed or {}
        elif kind == KIND_CUBE:
            cubes.append((_stem(path), dict(parsed or {})))
        elif kind == KIND_VIEW:
            views.append((_stem(path), (raw or "").strip()))

    if config is None:
        return None

    # The sidecar wins over an inline ref_sql, the same precedence the file build
    # applies -- it is the form that gets syntax highlighting and a real diff, so
    # it is the form people maintain.
    for name, document in models:
        if name in ref_sql:
            document["ref_sql"] = ref_sql[name]

    manifest = manifest_from_documents(
        models=models,
        relationships=relationships,
        cubes=cubes,
        views=views,
        dialect=str(config.get("dialect") or ""),
        where=f"workspace {tenant_id}",
    )

    document = manifest.to_json_dict()
    # Byte-for-byte what `vanna project build` writes, so a manifest compiled here
    # and one compiled from the files have the same checksum -- and re-importing
    # after an edit through the API reports "unchanged" rather than a phantom diff.
    raw_content = json.dumps(document, indent=2, sort_keys=False) + "\n"

    result = apply_put(
        cursor,
        ConfigRecord(
            relative_path=f"projects/{tenant_id}/target/mdl.json",
            raw_content=raw_content,
            parsed=document,
            kind=KIND_MANIFEST,
        ),
        source="api",
        actor=actor,
        note="recompiled from the project sources",
    )
    if result != "unchanged":
        logger.info(
            "Recompiled the manifest for %s: %d model(s), %d relationship(s), "
            "%d cube(s) (%s)",
            tenant_id, len(manifest.models), len(manifest.relationships),
            len(manifest.cubes), result,
        )
    return result


def _model_name(path: str) -> str:
    """``projects/x/models/customers/metadata.yml`` -> ``customers``."""
    parts = path.split("/")
    return parts[-2] if len(parts) >= 2 else parts[-1]


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


# ----------------------------------------------------------------------
# Starter questions
# ----------------------------------------------------------------------


def project_starters(cursor: Any, record: ConfigRecord) -> None:
    """Sync ``starter_questions`` to what the domain document now says.

    Authoritative, not additive: a question removed from the document is removed
    from the table. That is what "derived" means, and the additive behaviour in
    ``domains.provision`` -- which predates the catalog -- leaves deleted
    questions on screen forever.

    Only for workspaces that already exist. A domain document naming a workspace
    nobody has provisioned is a definition waiting to be applied, not a reason to
    create a tenant row with no data source behind it.
    """
    document = record.parsed or {}
    for domain in document.get("domains") or []:
        tenant_id = str(domain.get("id") or "")
        if not tenant_id:
            continue

        cursor.execute(f"SELECT 1 FROM {SCHEMA}.tenants WHERE id = %s", (tenant_id,))
        if cursor.fetchone() is None:
            continue

        wanted = [
            " ".join(str(question).split())
            for question in (domain.get("starters") or [])
        ]
        cursor.execute(
            f"DELETE FROM {SCHEMA}.starter_questions WHERE tenant_id = %s",
            (tenant_id,),
        )
        for order, question in enumerate(wanted):
            if not question:
                continue
            cursor.execute(
                f"""INSERT INTO {SCHEMA}.starter_questions
                        (tenant_id, question, sort_order)
                    VALUES (%s, %s, %s)""",
                (tenant_id, question, order),
            )
