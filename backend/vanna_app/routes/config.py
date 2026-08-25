"""Editing configuration, without a file anywhere in the loop.

The point of moving configuration into the control plane: an administrator changes
a cube here, the write lands in ``config_files``, the manifest is recompiled in the
same transaction, and every worker picks it up within the refresh window. Nothing
is written to disk and nothing needs a rebuilt image.

Three rules govern this surface.

**Platform admin only.** A cube describes what a workspace's data *means*, and the
grants that restrict what a workspace may read name the models in it. Letting a
tenant admin rewrite their own models would let them widen their own access.

**Validated with the runtime's own types.** A saved cube that does not compile is
worse than a rejected one: the deployment keeps running until the next cold
runtime build, and then fails somewhere else entirely. Every payload goes through
the same pydantic models and the same ``manifest_from_documents`` the file-based
build uses, before it is stored.

**No credentials.** The same refusal the importer applies. ``tenant_datasources``
is where warehouse credentials live, encrypted; this table must not become a
second home for them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..authz import require_platform_admin
from . import Deps

logger = logging.getLogger("vanna.routes.config")


class FilePayload(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=4_000_000)
    note: str = Field(default="", max_length=500)


class RevertPayload(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=1)
    note: str = Field(default="", max_length=500)


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings
    base = "/api/vanna/v2/admin/config"

    async def _admin(request: Request) -> Any:
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        return user

    def _store() -> Any:
        store = getattr(deps.platform, "config_store", None)
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="The configuration catalog needs the control-plane database.",
            )
        return store

    async def _audit(user: Any, action: str, details: Dict[str, Any]) -> None:
        if deps.admin_audit is None:
            return
        await deps.admin_audit.record(
            action,
            actor_email=getattr(user, "email", "") or "",
            tenant_id=details.get("tenant_id") or "",
            target=details.get("path") or "configuration",
            details=details,
        )

    def _render(record: Any, *, content: bool = False) -> Dict[str, Any]:
        payload = {
            "path": record.relative_path,
            "kind": record.kind,
            "scope": record.scope,
            "tenant_id": record.tenant_id,
            "project": record.project,
            "checksum": record.checksum,
            "version": record.version,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
            "updated_by": record.updated_by,
            # A file the importer could not parse is stored but unusable, and the
            # screen has to be able to say so -- otherwise it looks like a cube
            # that simply has no effect.
            "parsed": record.parsed is not None,
        }
        if content:
            payload["content"] = record.raw_content
        return payload

    # -- reads ---------------------------------------------------------

    @app.get(f"{base}/files")
    async def list_files(
        request: Request, kind: str = "", tenant_id: str = ""
    ) -> Dict[str, Any]:
        await _admin(request)
        records = await _store().list(
            kind=kind or None,
            tenant_id=tenant_id or None,
            with_content=False,
        )
        return {
            "items": [_render(record) for record in records],
            "source": settings.config_source,
        }

    @app.get(f"{base}/file")
    async def read_file(request: Request, path: str) -> Dict[str, Any]:
        await _admin(request)
        record = await _store().get(path)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No configuration at {path}")
        return _render(record, content=True)

    @app.get(f"{base}/versions")
    async def list_versions(request: Request, path: str) -> Dict[str, Any]:
        await _admin(request)
        record = await _store().get(path)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No configuration at {path}")
        history = await _store().history(record.id, limit=50)
        return {
            "path": record.relative_path,
            "items": [
                {
                    "version": row["version"],
                    "checksum": row["checksum"],
                    "source": row["source"],
                    "created_by": row["created_by"],
                    "note": row["note"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                }
                for row in history
            ],
        }

    # -- writes --------------------------------------------------------

    @app.put(f"{base}/file")
    async def write_file(request: Request, payload: FilePayload) -> Dict[str, Any]:
        user = await _admin(request)
        record = _validated_record(payload.path, payload.content)

        result = await _write(record, user, note=payload.note or None)
        await _audit(
            user,
            "config.write",
            {
                "path": record.relative_path,
                "tenant_id": record.tenant_id,
                "kind": record.kind,
                "result": result,
                "version": record.version,
            },
        )
        return {
            "path": record.relative_path,
            "kind": record.kind,
            "result": result,
            "version": record.version,
            "refresh_seconds": settings.config_refresh_seconds,
        }

    @app.post(f"{base}/revert")
    async def revert_file(request: Request, payload: RevertPayload) -> Dict[str, Any]:
        """Put a previous version back, as a new version.

        Forward-only: reverting writes version N+1 with the old content rather
        than deleting versions. History that can be rewritten answers "what was
        running last Tuesday" with a guess.
        """
        user = await _admin(request)
        store = _store()
        current = await store.get(payload.path)
        if current is None:
            raise HTTPException(
                status_code=404, detail=f"No configuration at {payload.path}"
            )

        row = await store.db.fetch_one(
            "SELECT raw_content FROM vanna_app.config_versions "
            "WHERE config_file_id = %s AND version = %s",
            (current.id, payload.version),
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"{payload.path} has no version {payload.version}",
            )

        record = _validated_record(payload.path, row["raw_content"])
        result = await _write(
            record,
            user,
            note=payload.note or f"reverted to version {payload.version}",
        )
        await _audit(
            user,
            "config.revert",
            {
                "path": record.relative_path,
                "tenant_id": record.tenant_id,
                "to_version": payload.version,
                "result": result,
                "version": record.version,
            },
        )
        return {"path": record.relative_path, "result": result, "version": record.version}

    async def _write(record: Any, user: Any, *, note: Optional[str]) -> str:
        from ..config_projection import project_all

        actor = getattr(user, "email", "") or getattr(user, "id", "") or "admin"
        result = await _store().put(
            record,
            source="api",
            actor=actor,
            note=note,
            project_into=lambda cursor, written: project_all(
                cursor, written, actor=actor
            ),
        )
        if result != "unchanged":
            # This worker, immediately; the other three when their next
            # fingerprint check lands. There is no way to push to them, and a
            # bounded staleness is the honest thing to report to the caller --
            # which is why the response carries `refresh_seconds`.
            deps.platform.forget_configuration()
        return result


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def _validated_record(path: str, content: str) -> Any:
    """A record for ``path``, or an HTTP error saying why the content is not one.

    Every check the file loaders make, on the way in. A payload accepted here and
    refused at the next cold runtime build would take the semantic layer down for
    a workspace, minutes later, somewhere unrelated.
    """
    from ..config_store import (
        ConfigParseError,
        ConfigRecord,
        classify,
        find_secrets,
        parse_content,
    )

    where = classify(path)

    try:
        parsed = parse_content(path, content)
    except ConfigParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    secrets = find_secrets(parsed, content)
    if secrets:
        raise HTTPException(
            status_code=400,
            detail=(
                "This looks like it carries a credential at "
                + ", ".join(secrets[:4])
                + ". Warehouse credentials belong in the data source registry, "
                "where they are encrypted."
            ),
        )

    _validate_by_kind(where.kind, path, parsed)
    return ConfigRecord(relative_path=path, raw_content=content, parsed=parsed)


def _validate_by_kind(kind: str, path: str, parsed: Any) -> None:
    from vanna.core.errors import VannaError

    from ..config_store import (
        KIND_BASELINE,
        KIND_CUBE,
        KIND_DOMAIN,
        KIND_MANIFEST,
        KIND_MODEL,
        KIND_PACK,
        KIND_PROJECT_CONFIG,
        KIND_RELATIONSHIPS,
    )

    document = parsed if isinstance(parsed, dict) else {}

    try:
        if kind == KIND_PROJECT_CONFIG:
            from pathlib import Path

            from vanna.project.loader import ProjectConfig

            ProjectConfig.from_dict(document, source=Path(path))
        elif kind == KIND_MANIFEST:
            from vanna.semantic import Manifest

            Manifest.from_json_dict(document)
        elif kind == KIND_CUBE:
            from vanna.semantic import Cube

            Cube.model_validate({"name": _stem(path), **document})
        elif kind == KIND_MODEL:
            from vanna.semantic import SemanticModel

            SemanticModel.model_validate({"name": _model_name(path), **document})
        elif kind == KIND_RELATIONSHIPS:
            from vanna.semantic import Relationship

            for entry in document.get("relationships") or []:
                Relationship.model_validate(entry)
        elif kind == KIND_BASELINE:
            from ..instruction_library import baseline_from_raw

            baseline_from_raw(document, where=path)
        elif kind == KIND_PACK:
            from ..instruction_library import pack_from_raw

            pack_from_raw(document, stem=_stem(path), where=path)
        elif kind == KIND_DOMAIN:
            from ..domains import validate_definitions

            validate_definitions(document, where=path)
    except HTTPException:
        raise
    except VannaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - pydantic, yaml and our own errors
        raise HTTPException(status_code=400, detail=f"{path}: {exc}") from exc


def _stem(path: str) -> str:
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _model_name(path: str) -> str:
    parts = str(path).replace("\\", "/").split("/")
    return parts[-2] if len(parts) >= 2 else parts[-1]


__all__ = ["register"]
