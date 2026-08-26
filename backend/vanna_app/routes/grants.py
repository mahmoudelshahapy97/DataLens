"""Granting read and write access, per table and per column.

The administrator's half of the write feature. Everything else in the flow
answers "is this specific statement all right"; this answers the prior question
of what could ever be all right, and it is the only place that answer is set.

Two properties worth stating, because both are load-bearing elsewhere:

**Every mutation bumps the workspace's grant version.** That version is stamped
on a pending write when it is proposed and re-checked before it runs, so revoking
access here refuses a change that was already approved. The store does the bump
inside the same transaction as the grant; these routes only have to call it.

**Granting a table auto-fills its column grants, and never overwrites one.** The
alternative is an administrator granting a table and then facing ninety columns,
which ends in somebody granting everything. Gaps are filled; an explicit earlier
choice to withhold a column survives.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..datasources import UnknownDataSource
from ..authz import ROLES, require_tenant_admin
from . import Deps

logger = logging.getLogger("vanna.routes.grants")


class RolePolicyPayload(BaseModel):
    preset: str = Field(default="none", min_length=1, max_length=64)
    apply_to_new_tables: bool = False
    enforce_reads: bool = False


class PolicyPayload(BaseModel):
    #: role -> its default
    roles: Dict[str, RolePolicyPayload] = Field(default_factory=dict)
    #: Saving intent and changing permissions are separate decisions, and only
    #: the second moves the grant version and re-authorizes pending writes.
    apply: bool = False
    mode: str = Field(default="fill", pattern="^(fill|replace)$")


class ApplyPresetPayload(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    preset: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="fill", pattern="^(fill|replace)$")
    #: Restrict to these tables. None means the whole catalog.
    tables: Optional[List[str]] = None


class TableGrantPayload(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    table: str = Field(min_length=1, max_length=512)
    can_read: bool = False
    can_insert: bool = False
    can_update: bool = False
    can_delete: bool = False
    #: Fill in read grants for the table's columns, so a caller does not have to
    #: grant ninety of them by hand. Never overwrites an existing choice.
    autofill_columns: bool = True


class ColumnGrantPayload(BaseModel):
    role: str = Field(min_length=1, max_length=64)
    table: str = Field(min_length=1, max_length=512)
    column: str = Field(min_length=1, max_length=256)
    can_read: bool = False
    can_filter: Optional[bool] = None
    can_aggregate: Optional[bool] = None
    can_write: bool = False
    #: How a readable value is obscured. See MASK_STRATEGIES in
    #: vanna/core/grants/models.py -- and note that masking is *weaker* than
    #: withholding the column, which stays the default and the recommendation.
    mask: Optional[str] = None


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings

    async def _admin(request: Request, tenant_id: str) -> Any:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return user

    def _store() -> Any:
        store = getattr(deps.platform, "grants", None)
        if store is None:
            # No control plane means nowhere durable to record who granted what,
            # and a grant nobody can audit is worse than no grant at all.
            raise HTTPException(
                status_code=503,
                detail="Grants need the control-plane database.",
            )
        return store

    def _role(role: str) -> str:
        """Reject a role nobody holds, rather than granting into a typo.

        A grant for role ``analsyt`` is stored happily, resolves for nobody, and
        looks identical to a grant that works until someone asks why their
        analysts cannot see a table.
        """
        if role not in ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role {role!r}. Expected one of {', '.join(ROLES)}.",
            )
        return role

    def _context(user: Any, tenant_id: str) -> Any:
        """A context scoped to the tenant being *administered*.

        The caller stays as ``user`` for audit purposes, but the tenant comes
        from the path -- which ``_admin`` has already authorized. Using
        ``user.tenant_id`` here instead was a cross-tenant bug: a platform admin
        managing workspace B would read and write grants against their own
        workspace A, silently and with every permission check passing.
        """
        from vanna.core.tool import ToolContext

        return ToolContext(
            user=user,
            conversation_id="admin-grants",
            request_id="admin-grants",
            tenant_id=tenant_id,
            agent_memory=deps.agent_memory,
        )

    async def _data_source(tenant_id: str, data_source_id: Optional[str]) -> str:
        """Which of the workspace's databases is being administered.

        Takes the tenant id, not the user: ``Platform.runtime_for`` keys its
        cache by that string, and passing a ``User`` raised
        ``TypeError: unhashable type`` on the first line of every endpoint here.

        A workspace can now register several databases, and the grant tables have
        always been keyed ``(tenant_id, data_source_id, role, ...)`` -- so without
        a way to name one, every endpoint here silently administered the default
        and the second database's matrix was unreachable.

        Declared on every endpoint rather than read off ``request.query_params``,
        so it appears in the OpenAPI schema and a client can discover it. That is
        nine repetitions of one parameter, which is the cost of the schema being
        honest about what these routes accept.
        """
        try:
            runtime = await deps.runtime_for(tenant_id, data_source_id=data_source_id)
        except UnknownDataSource as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return runtime.data_source

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/grants")
    async def list_grants(
        tenant_id: str,
        request: Request,
        role: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store, context = _store(), _context(user, tenant_id)
        data_source = await _data_source(tenant_id, data_source_id)
        tables = await store.list_table_grants(
            context, data_source_id=data_source, role=role
        )
        columns = await store.list_column_grants(
            context, data_source_id=data_source, role=role
        )
        return {
            "version": await store.version(context, data_source_id=data_source),
            "roles": list(ROLES),
            # Every table the catalog knows, not only those with a grant row.
            # A matrix has to show what is *not* granted -- that is most of it,
            # and it is the half an administrator came here to change.
            "resources": await _resources(user, tenant_id, data_source),
            "tables": [
                {
                    "role": g.role, "table": g.table,
                    "can_read": g.can_select, "can_insert": g.can_insert,
                    "can_update": g.can_update, "can_delete": g.can_delete,
                }
                for g in tables
            ],
            "columns": [
                {
                    "role": g.role, "table": g.table, "column": g.column,
                    "can_read": g.can_read, "can_filter": g.can_filter,
                    "can_aggregate": g.can_aggregate, "can_write": g.can_write,
                    "mask": g.mask,
                }
                for g in columns
            ],
        }

    @app.put("/api/vanna/v2/admin/tenants/{tenant_id}/grants/table")
    async def set_table_grant(
        tenant_id: str,
        payload: TableGrantPayload,
        request: Request,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from vanna.core.grants import TableGrant

        user = await _admin(request, tenant_id)
        store, context = _store(), _context(user, tenant_id)
        data_source = await _data_source(tenant_id, data_source_id)

        role = _role(payload.role)
        try:
            grant = TableGrant(
                tenant_id=tenant_id,
                data_source_id=data_source,
                role=role,
                table=payload.table,
                can_select=payload.can_read,
                can_insert=payload.can_insert,
                can_update=payload.can_update,
                can_delete=payload.can_delete,
            )
        except ValueError as exc:
            # The write-implies-read invariant, refused before it reaches SQL.
            raise HTTPException(status_code=400, detail=str(exc))

        await store.set_table_grant(context, grant)

        if payload.autofill_columns and payload.can_read:
            columns, unassignable = await _columns_of(
                user, tenant_id, payload.table, data_source
            )
            if columns:
                await store.auto_grant_columns(
                    context,
                    data_source_id=data_source,
                    role=role,
                    table=payload.table,
                    columns=columns,
                    can_write=payload.can_insert or payload.can_update,
                    unassignable=unassignable,
                )

        logger.info(
            "Grant set tenant=%s role=%s table=%s read=%s insert=%s update=%s delete=%s",
            tenant_id, payload.role, payload.table, payload.can_read,
            payload.can_insert, payload.can_update, payload.can_delete,
        )
        if deps.admin_audit is not None:
            await deps.admin_audit.record(
                "tenant.update",
                actor_email=getattr(user, "email", "") or "",
                tenant_id=tenant_id,
                target="grants",
                details={"kind": "table_grant", **payload.model_dump()},
            )
        return {"version": await store.version(context, data_source_id=data_source)}

    @app.put("/api/vanna/v2/admin/tenants/{tenant_id}/grants/column")
    async def set_column_grant(
        tenant_id: str,
        payload: ColumnGrantPayload,
        request: Request,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from vanna.core.grants import ColumnGrant

        user = await _admin(request, tenant_id)
        store, context = _store(), _context(user, tenant_id)
        data_source = await _data_source(tenant_id, data_source_id)

        role = _role(payload.role)
        try:
            grant = ColumnGrant(
                tenant_id=tenant_id,
                data_source_id=data_source,
                role=role,
                table=payload.table,
                column=payload.column,
                can_read=payload.can_read,
                # Filter and aggregate follow read unless stated. Write does
                # not: it is the one flag nobody should acquire by omission.
                can_filter=payload.can_read
                if payload.can_filter is None
                else payload.can_filter,
                can_aggregate=payload.can_read
                if payload.can_aggregate is None
                else payload.can_aggregate,
                can_write=payload.can_write,
                # Absent means unchanged-from-default rather than "clear it":
                # a PUT that omits the mask is setting the read flags, and
                # silently unmasking a column as a side effect of that is the
                # kind of widening nobody would look for.
                mask=(payload.mask or "none") if payload.can_read else "none",
                # Naming the person takes this column out of autofill's hands:
                # a deliberate choice must survive the table's access level
                # being changed afterwards.
                granted_by=getattr(user, "email", None) or getattr(user, "id", None),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        await store.set_column_grant(context, grant)
        if deps.admin_audit is not None:
            await deps.admin_audit.record(
                "tenant.update",
                actor_email=getattr(user, "email", "") or "",
                tenant_id=tenant_id,
                target="grants",
                details={"kind": "column_grant", **payload.model_dump()},
            )
        return {"version": await store.version(context, data_source_id=data_source)}

    async def _catalog(
        user: Any, tenant_id: str, data_source_id: Optional[str] = None
    ) -> List[Any]:
        """The tenant's scanned tables, or an empty list.

        Read from the catalog rather than the live database because the catalog
        is what the write policy is built from -- offering a column the policy
        cannot see would produce a grant that silently does nothing.

        Explicitly *unfiltered*. The runtime's catalog may be narrowed to what
        the caller may read, and the matrix has to show every table there is --
        otherwise an administrator whose role had read enforcement on and no
        grants yet would open an empty screen with nothing to grant from.
        """
        from ..read_guard import unfiltered

        # The named database, not the workspace default. Resolving the default
        # here while the grants were written against another one listed the
        # wrong database's tables in the matrix -- an administrator would grant
        # on tables that are not in the database they were editing.
        runtime = await deps.runtime_for(tenant_id, data_source_id=data_source_id)
        try:
            tables = await unfiltered(runtime.catalog).get_tables(
                _context(user, tenant_id), data_source_id=runtime.data_source
            )
        except Exception as exc:
            logger.warning("Could not read the catalog for %s: %s", tenant_id, exc)
            return []
        return list(tables or [])

    async def _resources(
        user: Any, tenant_id: str, data_source_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every table and column the matrix should offer.

        Key and generated flags come from ``catalog_write_facts`` -- the same
        function ``build_write_policy`` uses -- so the console cannot show a
        column as assignable that the validator would then refuse.
        """
        from vanna.core.grants import catalog_write_facts, normalize_table

        tables = await _catalog(user, tenant_id, data_source_id)
        keys, generated = catalog_write_facts(tables)

        resources = []
        for table in tables:
            schema = getattr(table, "schema_name", None)
            name = getattr(table, "table_name", "")
            qualified = f"{schema}.{name}" if schema else name
            table_key = normalize_table(qualified)
            key_names = {n.casefold() for n in keys.get(table_key, ())}
            generated_names = {n.casefold() for n in generated.get(table_key, ())}
            resources.append({
                "schema": schema,
                "table": qualified,
                "columns": [
                    {
                        "name": column.name,
                        "data_type": getattr(column, "data_type", "unknown"),
                        "is_primary_key": column.name.casefold() in key_names,
                        # Unassignable: the database computes it, so a write
                        # grant on it can only ever produce a refused statement.
                        "is_generated": column.name.casefold() in generated_names,
                    }
                    for column in (getattr(table, "columns", None) or [])
                ],
            })
        resources.sort(key=lambda r: r["table"])
        return resources

    async def _columns_of(
        user: Any, tenant_id: str, table: str, data_source_id: Optional[str] = None
    ) -> tuple:
        """One table's column names, and the ones no write may assign."""
        from vanna.core.grants import normalize_table

        wanted = normalize_table(table)
        for candidate in await _catalog(user, tenant_id, data_source_id):
            schema = getattr(candidate, "schema_name", None)
            name = getattr(candidate, "table_name", "")
            qualified = f"{schema}.{name}" if schema else name
            if normalize_table(qualified) != wanted and normalize_table(name) != wanted:
                continue
            columns = list(getattr(candidate, "columns", []) or [])
            return (
                [c.name for c in columns],
                [c.name for c in columns if getattr(c, "is_generated", False)],
            )
        return [], []

    # -- presets and the workspace default -----------------------------

    def _policies() -> Any:
        store = getattr(deps.platform, "grant_policies", None)
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Grant defaults need the control-plane database.",
            )
        return store

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/grants/presets")
    async def list_presets(tenant_id: str, request: Request) -> Dict[str, Any]:
        await _admin(request, tenant_id)
        from vanna.core.grants import BUILTIN_PRESETS

        return {
            "presets": [
                {
                    "name": p.name,
                    "title": p.title,
                    "description": p.description,
                    "table": {
                        "can_read": p.can_select,
                        "can_insert": p.can_insert,
                        "can_update": p.can_update,
                        "can_delete": p.can_delete,
                    },
                    "column": {
                        "can_read": p.column_read,
                        "can_filter": p.column_filter,
                        "can_aggregate": p.column_aggregate,
                        "can_write": p.column_write,
                    },
                }
                for p in sorted(BUILTIN_PRESETS.values(), key=lambda x: x.name)
            ]
        }

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/grants/policy")
    async def get_policy(
        tenant_id: str, request: Request, data_source_id: Optional[str] = None
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store = _store()
        data_source = await _data_source(tenant_id, data_source_id)
        policies = await _policies().get(tenant_id, data_source)
        return {
            "data_source": data_source,
            "version": await store.version(
                _context(user, tenant_id), data_source_id=data_source
            ),
            "roles": {role: p.as_json() for role, p in policies.items()},
        }

    @app.put("/api/vanna/v2/admin/tenants/{tenant_id}/grants/policy")
    async def set_policy(
        tenant_id: str,
        payload: PolicyPayload,
        request: Request,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        from vanna.core.grants import get_preset

        from ..grant_policy import RolePolicy

        store = _store()
        policy_store = _policies()
        data_source = await _data_source(tenant_id, data_source_id)
        context = _context(user, tenant_id)

        wanted: List[Any] = []
        for role, entry in payload.roles.items():
            _role(role)
            if get_preset(entry.preset) is None:
                raise HTTPException(
                    status_code=400, detail=f"Unknown preset {entry.preset!r}."
                )
            if entry.enforce_reads:
                # Switching this on against an empty matrix denies every table at
                # once. Grants governed writes only until now, so a workspace
                # arriving here has no read grants unless it has just made some.
                held = await store.list_table_grants(
                    context, data_source_id=data_source, role=role
                )
                # A named preset only counts if it is actually being applied in
                # this call. Naming one with `apply: false` writes no grants, so
                # the role would be enforced against an empty matrix -- which is
                # the lockout this check exists to prevent, reached by a route
                # the first version of it left open.
                will_have = any(g.can_select for g in held) or (
                    entry.preset != "none" and payload.apply
                )
                if not will_have:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Role {role} would have no readable table, so "
                            "enforcing reads would deny everything. Grant a table, "
                            "or apply a preset in the same request."
                        ),
                    )
            wanted.append(
                RolePolicy(
                    role=role,
                    preset=entry.preset,
                    apply_to_new_tables=entry.apply_to_new_tables,
                    enforce_reads=entry.enforce_reads,
                )
            )

        await policy_store.set(
            tenant_id, data_source, wanted, updated_by=getattr(user, "email", "") or ""
        )

        applied: Dict[str, Any] = {}
        if payload.apply and wanted:
            from ..grant_defaults import materialize
            from ..read_guard import unfiltered

            runtime = await deps.runtime_for(tenant_id)
            applied = await materialize(
                store=store,
                policy_store=policy_store,
                context=context,
                tenant_id=tenant_id,
                data_source_id=data_source,
                catalog=unfiltered(runtime.catalog),
                roles=[p.role for p in wanted],
                mode=payload.mode,
                granted_by=getattr(user, "email", "") or "",
            )

        if deps.admin_audit is not None:
            await deps.admin_audit.record(
                "grants.policy_changed",
                actor_email=getattr(user, "email", "") or "",
                tenant_id=tenant_id,
                target="grants",
                details={
                    "roles": {p.role: p.preset for p in wanted},
                    "applied": bool(payload.apply),
                },
            )

        return {
            "version": await store.version(context, data_source_id=data_source),
            "applied": applied,
        }

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/grants/apply-preset")
    async def apply_preset_now(
        tenant_id: str,
        payload: ApplyPresetPayload,
        request: Request,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        from vanna.core.grants import get_preset, preset_grants

        from ..read_guard import unfiltered

        _role(payload.role)
        preset = get_preset(payload.preset)
        if preset is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown preset {payload.preset!r}."
            )

        store = _store()
        data_source = await _data_source(tenant_id, data_source_id)
        context = _context(user, tenant_id)
        runtime = await deps.runtime_for(tenant_id)
        tables = await unfiltered(runtime.catalog).get_tables(
            context, data_source_id=data_source
        )

        table_grants, column_grants = preset_grants(
            preset,
            tenant_id=tenant_id,
            data_source_id=data_source,
            role=payload.role,
            tables=tables or [],
            only=payload.tables,
        )
        counts = await store.apply_preset(
            context,
            data_source_id=data_source,
            role=payload.role,
            table_grants=table_grants,
            column_grants=column_grants,
            mode=payload.mode,
            granted_by=getattr(user, "email", "") or "",
        )

        version = await store.version(context, data_source_id=data_source)
        policy_store = getattr(deps.platform, "grant_policies", None)
        if policy_store is not None:
            await policy_store.mark_applied(
                tenant_id, data_source, payload.role, version=version
            )

        if deps.admin_audit is not None:
            await deps.admin_audit.record(
                "grants.preset_applied",
                actor_email=getattr(user, "email", "") or "",
                tenant_id=tenant_id,
                target="grants",
                details={
                    "role": payload.role,
                    "preset": preset.name,
                    "mode": payload.mode,
                    **counts,
                },
            )

        return {"version": version, **counts}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/grants/table")
    async def revoke_table(
        tenant_id: str,
        request: Request,
        role: str,
        table: str,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        _role(role)
        store = _store()
        data_source = await _data_source(tenant_id, data_source_id)
        context = _context(user, tenant_id)

        removed = await store.delete_table_grant(
            context, data_source_id=data_source, role=role, table=table
        )
        if deps.admin_audit is not None and removed:
            await deps.admin_audit.record(
                "grants.revoked",
                actor_email=getattr(user, "email", "") or "",
                tenant_id=tenant_id,
                target="grants",
                details={"kind": "table", "role": role, "table": table},
            )
        return {
            "version": await store.version(context, data_source_id=data_source),
            "deleted": int(removed),
        }

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/grants/column")
    async def revoke_column(
        tenant_id: str,
        request: Request,
        role: str,
        table: str,
        column: str,
        data_source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        _role(role)
        store = _store()
        data_source = await _data_source(tenant_id, data_source_id)
        context = _context(user, tenant_id)

        removed = await store.delete_column_grant(
            context,
            data_source_id=data_source,
            role=role,
            table=table,
            column=column,
        )
        if deps.admin_audit is not None and removed:
            await deps.admin_audit.record(
                "grants.revoked",
                actor_email=getattr(user, "email", "") or "",
                tenant_id=tenant_id,
                target="grants",
                details={
                    "kind": "column",
                    "role": role,
                    "table": table,
                    "column": column,
                },
            )
        return {
            "version": await store.version(context, data_source_id=data_source),
            "deleted": int(removed),
        }
