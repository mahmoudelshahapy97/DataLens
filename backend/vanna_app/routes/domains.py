"""Business domain administration.

Domains describe what a slice of a database is *for* -- the tables that belong
together, and the vocabulary people use about them. See
:mod:`vanna_app.domain_store` for why that is deliberately not an access control.

Admin-only, and scoped to the workspace in the path rather than the caller's own.
That distinction has bitten this codebase before: ``routes/grants.py`` carries a
comment about a platform admin managing workspace B while reading and writing
against workspace A, with every permission check passing. The same shape of bug
is available here, so the same defence is used -- ``require_tenant_admin`` on the
path tenant, and the path tenant is what reaches the store.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..authz import require_tenant_admin
from . import Deps

logger = logging.getLogger("vanna.routes.domains")

BASE = "/api/vanna/v2/admin/tenants/{tenant_id}/domains"


class DomainPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    #: {"churn": "no order in 90 days"} -- the terms of art a schema cannot carry.
    terminology: Dict[str, str] = Field(default_factory=dict)
    tables: List[str] = Field(default_factory=list)


class DomainPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=4000)
    terminology: Optional[Dict[str, str]] = None
    is_enabled: Optional[bool] = None


class DomainTables(BaseModel):
    tables: List[str] = Field(default_factory=list)


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings

    async def _admin(request: Request, tenant_id: str) -> Any:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return user

    def _store() -> Any:
        store = getattr(deps.platform, "domains", None)
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Business domains need the control-plane database.",
            )
        return store

    async def _runtime(tenant_id: str, request: Request) -> Any:
        """The runtime for the database this request is about.

        Domains are keyed ``(tenant_id, data_source_id)``, and this used to
        resolve the workspace *default* unconditionally -- so a workspace whose
        session was pinned to its second database was shown the first one's
        domains, and the screen looked empty while the rows existed. Worse, the
        "default" is ``ORDER BY is_default DESC, data_source_id``: with no
        ``is_default`` row it is whichever id sorts first, so registering a
        database could silently move every existing domain out of view.

        Not ``deps.runtime_for_request``, which resolves ``user.tenant_id`` --
        the *session* workspace. This route administers the workspace in the
        path, which for a platform admin is frequently somebody else's, and
        using the session's would read the wrong customer's domains.

        So: honour ``X-Data-Source-Id`` only when the caller is administering
        their own workspace, where the header can actually name one of its
        databases. Administering another workspace falls back to that
        workspace's default, because the session's data source is not a
        meaningful id over there.
        """
        from ..datasources import UnknownDataSource

        requested = request.headers.get("x-data-source-id")
        user = await deps.caller(request)
        if requested and getattr(user, "tenant_id", None) == tenant_id:
            try:
                return await deps.runtime_for(tenant_id, data_source_id=requested)
            except UnknownDataSource:
                # The header names a database this workspace no longer has.
                # Falling back beats a 404 on a read-only listing screen.
                pass
        return await deps.runtime_for(tenant_id)

    async def _data_source(tenant_id: str, request: Request) -> str:
        return (await _runtime(tenant_id, request)).data_source

    async def _known_tables(tenant_id: str, user: Any, request: Request) -> set:
        """Every table in the workspace's catalog, normalized.

        Unfiltered on purpose, exactly as the permission matrix is: an
        administrator assigning tables to a domain has to see every table there
        is, and membership grants nothing on its own.
        """
        from vanna.core.grants import normalize_table

        from ..read_guard import unfiltered

        runtime = await _runtime(tenant_id, request)
        try:
            tables = await unfiltered(runtime.catalog).get_tables(
                await deps.tool_context(user, conversation_id="domains"),
                data_source_id=runtime.data_source
            )
        except Exception as exc:
            logger.warning("Could not read the catalog for %s: %s", tenant_id, exc)
            return set()

        known = set()
        for table in tables or []:
            schema = getattr(table, "schema_name", None)
            name = getattr(table, "table_name", "")
            known.add(normalize_table(f"{schema}.{name}" if schema else name))
        return known

    async def _validate_tables(
        tenant_id: str, user: Any, tables: List[str], request: Request
    ) -> List[str]:
        """Reject a table that is not in the catalog.

        A typo would otherwise be stored happily, match nothing, and look exactly
        like a domain that works -- the same failure mode ``_role`` guards against
        in the grants routes.

        Takes the request so the catalog it checks against is the *same* database
        the domain is being written to. Validating against the default while
        writing against another would reject a table that genuinely exists.
        """
        from vanna.core.grants import normalize_table

        if not tables:
            return []
        known = await _known_tables(tenant_id, user, request)
        unknown = [t for t in tables if normalize_table(t) not in known]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Not in this workspace's catalog: {', '.join(sorted(unknown)[:5])}."
                ),
            )
        return tables

    @app.get(BASE)
    async def list_domains(tenant_id: str, request: Request) -> Dict[str, Any]:
        await _admin(request, tenant_id)
        data_source = await _data_source(tenant_id, request)
        return {
            "data_source": data_source,
            "domains": await _store().list_domains(tenant_id, data_source_id=data_source),
        }

    @app.post(BASE, status_code=201)
    async def create_domain(
        tenant_id: str, payload: DomainPayload, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        data_source = await _data_source(tenant_id, request)
        await _validate_tables(tenant_id, user, payload.tables, request)

        store = _store()
        try:
            domain = await store.create(
                tenant_id,
                data_source_id=data_source,
                name=payload.name,
                description=payload.description,
                terminology=payload.terminology,
                created_by=getattr(user, "id", None),
            )
        except Exception as exc:
            # The unique index is case-insensitive: "Sales" and "sales" are the
            # same domain to everybody except a database.
            if "business_domains_name_idx" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"A domain called {payload.name!r} already exists here.",
                ) from exc
            raise

        if payload.tables:
            await store.replace_tables(
                tenant_id, domain["id"], payload.tables, added_by=getattr(user, "id", None)
            )
            domain = await store.get_domain(tenant_id, domain["id"])
        return domain

    @app.patch(BASE + "/{domain_id}")
    async def update_domain(
        tenant_id: str, domain_id: str, payload: DomainPatch, request: Request
    ) -> Dict[str, Any]:
        await _admin(request, tenant_id)
        store = _store()
        if await store.get_domain(tenant_id, domain_id) is None:
            raise HTTPException(status_code=404, detail="No such domain.")

        updated = await store.update(
            tenant_id,
            domain_id,
            name=payload.name,
            description=payload.description,
            terminology=payload.terminology,
            is_enabled=payload.is_enabled,
        )
        return updated or {}

    @app.put(BASE + "/{domain_id}/tables")
    async def set_domain_tables(
        tenant_id: str, domain_id: str, payload: DomainTables, request: Request
    ) -> Dict[str, Any]:
        """Membership is its own call.

        Separate from the patch above because it is a different decision: naming a
        domain is editorial, choosing which tables it covers is what the retrieval
        layer will act on.
        """
        user = await _admin(request, tenant_id)
        store = _store()
        if await store.get_domain(tenant_id, domain_id) is None:
            raise HTTPException(status_code=404, detail="No such domain.")

        await _validate_tables(tenant_id, user, payload.tables, request)
        await store.replace_tables(
            tenant_id, domain_id, payload.tables, added_by=getattr(user, "id", None)
        )
        return await store.get_domain(tenant_id, domain_id) or {}

    @app.delete(BASE + "/{domain_id}")
    async def delete_domain(
        tenant_id: str, domain_id: str, request: Request
    ) -> Dict[str, Any]:
        await _admin(request, tenant_id)
        if not await _store().delete(tenant_id, domain_id):
            raise HTTPException(status_code=404, detail="No such domain.")
        # Table annotations survive: `table_annotations.domain_id` is ON DELETE
        # SET NULL, because deleting a grouping must not delete the descriptions
        # somebody wrote for the tables in it.
        return {"deleted": domain_id}
