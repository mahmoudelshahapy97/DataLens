"""Table and column descriptions -- the part of the catalog a person writes.

The structural half of the catalog is machine-owned: a scan drops and rebuilds it,
so nothing a human types can live there. ``table_annotations`` and
``column_annotations`` are the other half, never touched by a scan, and both are
already merged into ``TableMetadata.description`` / ``ColumnMetadata.description``
on the way to the model's prompt.

Which made them the highest-leverage rows in the system with no way to write them:
the schema screen rendered descriptions read-only and the only editor was ``psql``.
``value_labels`` in particular is what stops the model inventing a literal --
``status = 'cancelled'`` when the column holds ``'C'`` returns zero rows rather than
an error anybody can act on.

Admin-only, and scoped to the workspace in the path rather than the caller's own,
for the reason spelled out in :mod:`vanna_app.routes.domains`: a platform admin
working on workspace B must not write against workspace A.

An annotation is descriptive, never permissive. Nothing here grants read on
anything -- ``table_grants`` and ``column_grants`` decide that, and a described
column a caller cannot read stays unreadable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..authz import require_tenant_admin
from . import Deps

logger = logging.getLogger("vanna.routes.catalog")

BASE = "/api/vanna/v2/admin/tenants/{tenant_id}/catalog"

#: Mirrors the CHECK constraint in migration 0010. Kept here as well so a bad value
#: is a 400 naming the options rather than a 500 from the database.
SENSITIVITIES = ("public", "internal", "confidential", "restricted")


class TableAnnotation(BaseModel):
    #: ``None`` means "leave as it is"; ``""`` means "clear it".
    description: Optional[str] = Field(default=None, max_length=4000)
    display_name: Optional[str] = Field(default=None, max_length=200)


class ColumnAnnotation(BaseModel):
    description: Optional[str] = Field(default=None, max_length=4000)
    display_name: Optional[str] = Field(default=None, max_length=200)
    #: {"A": "Active", "C": "Cancelled"} -- replaced as a set when given.
    value_labels: Optional[Dict[str, str]] = None
    sensitivity: Optional[str] = None


class CoreColumns(BaseModel):
    #: Replaced wholesale -- a curated set is edited as a set, and merging
    #: would make removing a column from it impossible.
    columns: List[str] = Field(default_factory=list)


class RelationshipReview(BaseModel):
    from_table: str = Field(max_length=400)
    from_column: str = Field(max_length=200)
    to_table: str = Field(max_length=400)
    to_column: str = Field(max_length=200)
    #: ``proposed`` withdraws a decision and hands the join back to its score.
    decision: Literal["accepted", "rejected", "proposed"]


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings

    async def _admin(request: Request, tenant_id: str) -> Any:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return user

    async def _store_and_source(tenant_id: str) -> tuple:
        """The control-plane catalog, and the database this workspace is bound to.

        Deliberately ``platform.catalog`` rather than ``runtime.catalog``. The
        runtime's is wrapped -- by the read guard always, and by
        ``SemanticSchemaCatalog`` when the workspace has a manifest -- and an
        annotation is keyed on a *physical* catalog row, which is the store
        underneath. Reaching for the wrapper is how this first shipped, and it
        answered 503 on every workspace with a semantic layer.

        The data source still comes from the runtime: it is what decides which
        database's rows are being described.
        """
        runtime = await deps.runtime_for(tenant_id)
        store = getattr(deps.platform, "catalog", None)
        if store is None or not hasattr(store, "annotate_table"):
            raise HTTPException(
                status_code=503,
                detail="Catalog descriptions need the control-plane database.",
            )
        return store, runtime.data_source

    async def _store_and_requested_source(tenant_id: str, request: Request) -> tuple:
        """As :func:`_store_and_source`, for the database the screen is showing.

        Inferred joins exist only on databases without declared foreign keys --
        rarely a workspace's default -- so these routes follow the
        ``X-Data-Source-Id`` the schema screen sends rather than the default.
        The id is checked against the workspace's registry; an unknown one is a
        404, never a quiet fall back to another database.
        """
        from ..datasources import UnknownDataSource

        store, _ = await _store_and_source(tenant_id)
        requested = request.headers.get("x-data-source-id")
        try:
            runtime = await deps.runtime_for(tenant_id, data_source_id=requested)
        except UnknownDataSource as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return store, runtime.data_source

    def _key(raw: str, *, what: str) -> str:
        """Normalize the way the catalog and the grant tables both do.

        Same function, so a description, a grant and a scanned row all join on the
        same key -- which is the property that makes the permission matrix and this
        screen agree about what a table is called.
        """
        from vanna.core.grants import normalize_identifier, normalize_table

        clean = (raw or "").strip()
        if not clean:
            raise HTTPException(status_code=400, detail=f"A {what} is required.")
        return normalize_table(clean) if what == "table" else normalize_identifier(clean)

    # Registered before the greedy `/tables/{table_key:path}` route below:
    # that path converter absorbs slashes, so a route for
    # "/tables/{table_key:path}/core-columns" registered *after* it would
    # never be reached -- the earlier, plainer route matches first and
    # swallows "orders/core-columns" whole as a table key.
    @app.get(BASE + "/tables/{table_key:path}/core-columns")
    async def get_core_columns(
        tenant_id: str, table_key: str, request: Request
    ) -> Dict[str, Any]:
        """Which columns of this table were curated as the ones that matter."""
        await _admin(request, tenant_id)
        store, data_source = await _store_and_source(tenant_id)
        key = _key(table_key, what="table")
        return {"columns": await store.get_core_columns(tenant_id, data_source, key)}

    @app.put(BASE + "/tables/{table_key:path}/core-columns")
    async def put_core_columns(
        tenant_id: str, table_key: str, payload: CoreColumns, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store, data_source = await _store_and_source(tenant_id)
        table = _key(table_key, what="table")

        if not await store.table_exists(tenant_id, data_source, table):
            raise HTTPException(
                status_code=404, detail="Not in this workspace's catalog."
            )

        keys = [_key(c, what="column") for c in payload.columns]
        unknown = [
            key
            for key in keys
            if not await store.column_exists(tenant_id, data_source, table, key)
        ]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Not in this table's catalog: {', '.join(unknown)}.",
            )

        columns = await store.set_core_columns(
            tenant_id, data_source, table, keys, marked_by=user.email or user.id
        )
        await deps.admin_audit.record(
            "catalog.core_columns_set",
            actor_email=user.email,
            target=f"{tenant_id}:{table}",
            actor_ip=deps.client_ip(request),
        )
        logger.info("%s set core columns for %s in %s", user.email, table, tenant_id)
        return {"columns": columns}

    # -- inferred relationships ----------------------------------------
    #
    # Joins the scanner guessed from column names for a database that declares
    # no foreign key for them. Confident guesses are used before anybody looks;
    # this is where an admin confirms the right ones (they then weigh the same
    # as a declared key) and rejects the wrong ones (never served again, even
    # after a rescan re-infers them).

    @app.get(BASE + "/relationships/inferred")
    async def list_inferred_relationships(
        tenant_id: str, request: Request
    ) -> Dict[str, Any]:
        from vanna.capabilities.schema_catalog.models import INFERRED_MIN_CONFIDENCE

        await _admin(request, tenant_id)
        store, data_source = await _store_and_requested_source(tenant_id, request)
        rows = await store.list_inferred_relationships(tenant_id, data_source)
        for row in rows:
            row["in_use"] = row["review_status"] == "accepted" or (
                row["review_status"] == "proposed"
                and (row.get("confidence") or 0) >= INFERRED_MIN_CONFIDENCE
            )
        return {"relationships": rows, "min_confidence": INFERRED_MIN_CONFIDENCE}

    @app.put(BASE + "/relationships/review")
    async def review_relationship(
        tenant_id: str, payload: RelationshipReview, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store, data_source = await _store_and_requested_source(tenant_id, request)
        found = await store.review_relationship(
            tenant_id,
            data_source,
            from_table=_key(payload.from_table, what="table"),
            from_column=_key(payload.from_column, what="column"),
            to_table=_key(payload.to_table, what="table"),
            to_column=_key(payload.to_column, what="column"),
            decision=payload.decision,
            reviewed_by=user.email or user.id,
        )
        if not found:
            raise HTTPException(
                status_code=404, detail="No inferred relationship with those columns."
            )
        target = (
            f"{tenant_id}:{payload.from_table}.{payload.from_column}"
            f"->{payload.to_table}.{payload.to_column}"
        )
        await deps.admin_audit.record(
            f"catalog.relationship_{payload.decision}",
            actor_email=user.email,
            target=target,
            actor_ip=deps.client_ip(request),
        )
        logger.info("%s marked %s %s", user.email, target, payload.decision)
        return {"review_status": payload.decision}

    @app.get(BASE + "/tables/{table_key:path}")
    async def get_table_annotation(
        tenant_id: str, table_key: str, request: Request
    ) -> Dict[str, Any]:
        """What was written about this table, and about each of its columns.

        Both in one response because an editor needs both: the description the
        schema screen *renders* has the code book folded into it -- that is the form
        that reaches the prompt -- so a screen cannot get the two fields back apart
        from what it already has.
        """
        await _admin(request, tenant_id)
        store, data_source = await _store_and_source(tenant_id)
        key = _key(table_key, what="table")
        return {
            "annotation": await store.get_table_annotation(tenant_id, data_source, key),
            "columns": await store.list_column_annotations(
                tenant_id, data_source, key
            ),
        }

    @app.patch(BASE + "/tables/{table_key:path}")
    async def patch_table_annotation(
        tenant_id: str, table_key: str, payload: TableAnnotation, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store, data_source = await _store_and_source(tenant_id)
        key = _key(table_key, what="table")

        # Refuse a table the catalog does not have. There is no foreign key to
        # enforce it -- annotations deliberately outlive a rescan -- so a typo
        # would otherwise be stored happily, reach nothing, and look like a
        # description that works.
        if not await store.table_exists(tenant_id, data_source, key):
            raise HTTPException(
                status_code=404, detail="Not in this workspace's catalog."
            )

        annotation = await store.annotate_table(
            tenant_id,
            data_source,
            key,
            description=payload.description,
            display_name=payload.display_name,
            updated_by=user.email or user.id,
        )
        await deps.admin_audit.record(
            "catalog.table_annotated",
            actor_email=user.email,
            target=f"{tenant_id}:{key}",
            actor_ip=deps.client_ip(request),
        )
        logger.info("%s described %s in %s", user.email, key, tenant_id)
        return {"annotation": annotation}

    @app.patch(BASE + "/columns/{table_key}/{column_key}")
    async def patch_column_annotation(
        tenant_id: str,
        table_key: str,
        column_key: str,
        payload: ColumnAnnotation,
        request: Request,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store, data_source = await _store_and_source(tenant_id)
        table = _key(table_key, what="table")
        column = _key(column_key, what="column")

        if payload.sensitivity is not None and payload.sensitivity not in SENSITIVITIES:
            raise HTTPException(
                status_code=400,
                detail=f"sensitivity must be one of: {', '.join(SENSITIVITIES)}.",
            )
        if not await store.column_exists(tenant_id, data_source, table, column):
            raise HTTPException(
                status_code=404, detail="Not in this workspace's catalog."
            )

        annotation = await store.annotate_column(
            tenant_id,
            data_source,
            table,
            column,
            description=payload.description,
            display_name=payload.display_name,
            value_labels=payload.value_labels,
            sensitivity=payload.sensitivity,
            updated_by=user.email or user.id,
        )
        await deps.admin_audit.record(
            "catalog.column_annotated",
            actor_email=user.email,
            target=f"{tenant_id}:{table}.{column}",
            actor_ip=deps.client_ip(request),
        )
        logger.info("%s described %s.%s in %s", user.email, table, column, tenant_id)
        return {"annotation": annotation}
