"""Data lineage and erasure requests.

One module for two features because they answer halves of the same question. "Who
reads this table?" and "erase this person" are both asked by somebody who has to
account for where data went, and splitting them across two files would put the
same guard, the same imports and the same 404 convention in both.

The authorisation differs sharply between them, and deliberately:

**Lineage** is workspace-scoped and read-only. Any admin of the workspace may see
which tables feed which dashboards -- it names schema objects and asset titles,
not rows.

**Erasure** is platform-admin only, and requires *two* of them. A workspace admin
cannot erase a person: the request spans workspaces by default, and even scoped to
one it deletes an account's history irreversibly. See ``compliance.py`` for the
full argument.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import HTTPException, Request

from ..authz import require_platform_admin, require_tenant_admin
from ..compliance import ComplianceError
from . import Deps

logger = logging.getLogger("vanna.routes.governance")


def register(app: Any, deps: Deps) -> None:

    # ------------------------------------------------------------------
    # Lineage
    # ------------------------------------------------------------------

    def _lineage() -> Any:
        service = getattr(deps, "lineage", None)
        if service is None:
            raise HTTPException(status_code=404, detail="Not found")
        return service

    @app.get("/api/vanna/v2/lineage")
    async def lineage_graph(request: Request) -> Dict[str, Any]:
        """Tables -> saved queries -> dashboards -> reports, for this workspace.

        Admin-only. It does not expose rows, but it does enumerate every table
        name the workspace's queries touch, which is a map of the warehouse.
        """
        user = await deps.caller(request)
        require_tenant_admin(user, user.tenant_id, deps.settings)
        return await _lineage().graph(user.tenant_id, dialect=await _dialect(user))

    @app.get("/api/vanna/v2/lineage/impact/{table:path}")
    async def lineage_impact(table: str, request: Request) -> Dict[str, Any]:
        """What breaks if this table changes.

        ``{table:path}`` because a qualified name contains a dot and may contain a
        slash in some warehouses; the default converter would truncate at the
        first separator and silently answer about a different table.
        """
        user = await deps.caller(request)
        require_tenant_admin(user, user.tenant_id, deps.settings)
        return await _lineage().impact(
            user.tenant_id, table, dialect=await _dialect(user)
        )

    async def _dialect(user: Any) -> str:
        """The SQL dialect this workspace's warehouse speaks.

        Passed to sqlglot so a statement parses under the grammar it was written
        for. Wrong-but-close usually still parses, which is why this falls back
        rather than failing: a dialect nobody could determine yields a slightly
        less accurate graph, not an error page.
        """
        try:
            runtime = await deps.runtime_for(user.tenant_id)
            return str(getattr(runtime, "dialect", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # Erasure
    # ------------------------------------------------------------------

    def _compliance() -> Any:
        service = getattr(deps, "compliance", None)
        if service is None:
            raise HTTPException(status_code=404, detail="Not found")
        return service

    @app.get("/api/vanna/v2/compliance/deletion-requests")
    async def list_requests(request: Request, status: str = "") -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, deps.settings)
        return {"requests": await _compliance().list(status=status)}

    @app.post("/api/vanna/v2/compliance/deletion-requests")
    async def create_request(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, deps.settings)
        try:
            created = await _compliance().create(
                subject_email=str(payload.get("subject_email") or ""),
                requested_by=user.email or user.id,
                tenant_id=str(payload.get("tenant_id") or ""),
                notes=str(payload.get("notes") or ""),
            )
        except ComplianceError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"request": created}

    @app.post("/api/vanna/v2/compliance/deletion-requests/{request_id}/execute")
    async def execute_request(request_id: str, request: Request) -> Dict[str, Any]:
        """Carry out an approved request. Irreversible.

        The two-person rule lives in ``Compliance.execute`` rather than here, so a
        future CLI or migration script reaching the same method cannot route
        around it. This layer only turns its refusal into a status code.
        """
        user = await deps.caller(request)
        require_platform_admin(user, deps.settings)
        try:
            done = await _compliance().execute(request_id, user.email or user.id)
        except ComplianceError as exc:
            # 409, not 400: the request exists and is well-formed. What is wrong
            # is its *state* -- already executed, or this admin is the one who
            # asked for it.
            raise HTTPException(status_code=409, detail=str(exc))
        return {"request": done}

    @app.post("/api/vanna/v2/compliance/deletion-requests/{request_id}/cancel")
    async def cancel_request(request_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, deps.settings)
        cancelled = await _compliance().cancel(request_id, user.email or user.id)
        if cancelled is None:
            raise HTTPException(status_code=404, detail="Not found")
        return {"request": cancelled}
