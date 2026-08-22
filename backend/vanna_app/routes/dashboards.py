"""Dashboards: create, read, execute and export.

The property that matters: **tiles are executed as the caller, through the
workspace's tool registry.** Two people opening the same dashboard can and should
see different numbers, because the row and column rules that apply to their
questions apply here too -- and an export can therefore never become a way around
them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import HTTPException, Request

from ..authz import forbid_viewer
from ..tenancy import describe_data_source
from . import Deps

logger = logging.getLogger("vanna.routes.dashboards")


def register(app: Any, deps: Deps) -> None:

    @app.get("/api/vanna/v2/dashboards")
    async def list_dashboards(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        return {"dashboards": await deps.require_directory().list_dashboards(user.tenant_id)}

    @app.get("/api/vanna/v2/dashboards/{dashboard_id}")
    async def get_dashboard(dashboard_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        row = await deps.require_directory().get_dashboard(user.tenant_id, dashboard_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return {"dashboard": row["document"]}

    @app.post("/api/vanna/v2/dashboards")
    async def save_dashboard(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """Create or replace a dashboard.

        Verified before it is stored *and* again before it is rendered. Storing a
        document known to be broken means a reader, not the author, discovers it --
        against something that looks saved and fine.
        """
        user = await deps.caller(request)
        forbid_viewer(user, "author dashboards")

        from vanna.dashboards import Dashboard, has_errors, verify_dashboard

        try:
            # tenant_id is forced from the caller, never read from the payload: a
            # document naming another workspace would otherwise be stored there.
            dashboard = Dashboard.model_validate({**payload, "tenant_id": user.tenant_id})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Malformed dashboard: {exc}")

        issues = verify_dashboard(dashboard)
        if has_errors(issues):
            raise HTTPException(
                status_code=400,
                detail=[str(i) for i in issues if i.severity == "error"],
            )

        saved = await deps.require_directory().save_dashboard(
            user.tenant_id, dashboard.to_json_dict(), created_by=user.email or user.id
        )
        return {
            "dashboard": saved["document"],
            "warnings": [str(i) for i in issues if i.severity == "warning"],
        }

    @app.delete("/api/vanna/v2/dashboards/{dashboard_id}")
    async def delete_dashboard(dashboard_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        forbid_viewer(user, "delete dashboards")
        if not await deps.require_directory().delete_dashboard(user.tenant_id, dashboard_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    async def _render(user: Any, dashboard_id: str) -> tuple:
        """Load, verify and execute a dashboard as the caller."""
        directory = deps.require_directory()

        row = await directory.get_dashboard(user.tenant_id, dashboard_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")

        from vanna.dashboards import Dashboard, has_errors, render_dashboard, verify_dashboard

        try:
            dashboard = Dashboard.model_validate(row["document"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Stored dashboard is invalid: {exc}")

        # Re-verified on the way out: this document may have been stored by an older
        # build with a weaker check.
        issues = verify_dashboard(dashboard)
        if has_errors(issues):
            raise HTTPException(
                status_code=400, detail=[str(i) for i in issues if i.severity == "error"]
            )

        saved_sql = {
            item["id"]: item["sql"] for item in await directory.list_saved(user.tenant_id)
        }
        runtime = await deps.runtime_for(user.tenant_id)
        results = await render_dashboard(
            dashboard,
            registry=runtime.agent.tool_registry,
            user=user,
            agent_memory=deps.agent_memory,
            saved_query_sql=saved_sql,
        )
        return dashboard, results

    @app.get("/api/vanna/v2/dashboards/{dashboard_id}/data")
    async def dashboard_data(dashboard_id: str, request: Request) -> Dict[str, Any]:
        """Execute every tile as the caller."""
        user = await deps.caller(request)
        _, results = await _render(user, dashboard_id)
        return {"results": [r.model_dump(mode="json") for r in results]}

    @app.get("/api/vanna/v2/dashboards/{dashboard_id}/export")
    async def export_dashboard(dashboard_id: str, request: Request) -> Any:
        """Download this dashboard as one self-contained HTML file.

        The tiles are executed here, as the caller, through exactly the path
        ``/data`` uses -- so the figures in the file are the ones this person is
        allowed to see.

        Nothing is hosted. The response is a file download; there is no URL that
        serves it afterwards, so there is no link to leak.
        """
        from fastapi.responses import Response

        user = await deps.caller(request)
        dashboard, results = await _render(user, dashboard_id)

        from vanna.dashboards import export_filename, export_html

        tenant = await deps.require_directory().get_tenant(user.tenant_id)
        html = export_html(
            dashboard,
            results,
            exported_by=user.email or user.id,
            workspace=(tenant or {}).get("name") or user.tenant_id,
            data_source=describe_data_source((tenant or {}).get("database_url")),
        )

        # Taking a copy of data out of the system is exactly the event somebody asks
        # about later, so it is recorded like any other generation.
        await _record_export(user, dashboard, results)

        logger.info(
            "Dashboard %s exported by %s (%d tiles)", dashboard_id, user.email, len(results)
        )
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export_filename(dashboard)}"'
            },
        )

    async def _record_export(user: Any, dashboard: Any, results: Any) -> None:
        """Log an export to the generation store. Never fails the download."""
        if deps.generation_store is None:
            return
        try:
            from vanna.core.generation import GenerationStatus, SqlGeneration

            rows = sum(r.row_count for r in results)
            failed = [r for r in results if r.error]
            await deps.generation_store.record(
                await deps.tool_context(user, conversation_id="export"),
                SqlGeneration(
                    tenant_id=user.tenant_id,
                    user_id=user.email or user.id,
                    question=f"[export] {dashboard.title or 'dashboard'}",
                    # The tile SQL, so the record answers "what data left the
                    # system", not merely "an export happened".
                    sql="\n".join(
                        f"-- tile {r.tile_id}: {r.row_count} row(s)"
                        + (f" -- FAILED: {r.error}" if r.error else "")
                        for r in results
                    )[:20_000],
                    status=GenerationStatus.VALID if not failed else GenerationStatus.INVALID,
                    row_count=rows,
                    metadata={
                        "kind": "dashboard_export",
                        "dashboard_id": dashboard.id,
                        "tiles": len(results),
                        "failed_tiles": len(failed),
                    },
                ),
            )
            await deps.admin_audit.record(
                "tenant.update",
                actor_email=user.email or user.id,
                tenant_id=user.tenant_id,
                target=f"dashboard:{dashboard.id}",
                details={"kind": "export", "tiles": len(results), "rows": rows},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record dashboard export: %s", type(exc).__name__)
