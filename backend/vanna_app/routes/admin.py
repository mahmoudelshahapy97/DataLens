"""Administration: workspaces, members, plans, datasources and the audit trail.

Every privileged operation in the product, in one file, so a reviewer can read the
authorisation model end to end rather than inferring it from eleven modules.

The tier each route needs is stated on its first line. Two changes from the original
are worth naming:

* **Billing is platform-admin.** Setting a plan and recording a payment used to need
  only tenant admin, so a workspace admin could POST ``{"plan": "enterprise"}`` and
  grant themselves a five-hundred-fold quota increase for free. Reading the plan and
  the payment history stays tenant-admin, which is what a customer actually needs.
* **Every mutation is audited.** Repointing a workspace, granting admin, changing a
  plan -- all of it previously existed only as a line on stdout.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..datasources import UnknownDataSource
from ..authz import (
    is_platform_admin,
    require_platform_admin,
    require_tenant_admin,
    visible_tenant,
)
from ..tenancy import describe_data_source
from . import Deps

logger = logging.getLogger("vanna.routes.admin")

#: Payload fields that describe a connection rather than the workspace itself.
#: Collected here so create and update agree on what to hand the engine registry.
CONNECTION_FIELDS = {
    "engine", "host", "port", "database", "username", "password", "sslmode",
    "path", "account", "warehouse", "role", "schema", "schema_name",
    "project", "dataset", "credentials_path", "catalog", "driver",
}


class ConnectionFields(BaseModel):
    """Structured connection fields, composed into a URL server-side.

    The browser never assembles a URL, so a password only ever travels as a single
    form field and percent-encoding happens once, correctly -- a password containing
    ``@`` otherwise produces a URL pointing at the wrong host.
    """

    engine: Optional[str] = "postgres"
    host: Optional[str] = None
    port: Optional[str] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    sslmode: Optional[str] = None
    path: Optional[str] = None
    account: Optional[str] = None
    warehouse: Optional[str] = None
    role: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")
    project: Optional[str] = None
    dataset: Optional[str] = None
    credentials_path: Optional[str] = None
    catalog: Optional[str] = None
    driver: Optional[str] = None


class TenantPayload(ConnectionFields):
    id: str
    name: str
    description: str = ""
    database_url: Optional[str] = None
    daily_quota: Optional[int] = Field(default=None, ge=0)
    max_rows: Optional[int] = Field(default=None, ge=1)


class TenantUpdate(ConnectionFields):
    name: Optional[str] = None
    description: Optional[str] = None
    database_url: Optional[str] = None
    is_active: Optional[bool] = None
    daily_quota: Optional[int] = Field(default=None, ge=0)
    max_rows: Optional[int] = Field(default=None, ge=1)
    allow_writes: Optional[bool] = None
    # Whether members may answer on their own LLM key. Turning it off is how a
    # workspace keeps its schema and questions away from accounts it does not
    # control.
    allow_byo_key: Optional[bool] = None


class UserPayload(BaseModel):
    email: str
    full_name: str = ""
    role: str = "analyst"


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class StarterPayload(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    sort_order: int = 0


class PlanPayload(BaseModel):
    plan: str = "free"
    months: int = Field(default=1, ge=0, le=120)


class PaymentPayload(BaseModel):
    reference: str = Field(min_length=1, max_length=200)
    plan: str = ""
    months: int = Field(default=1, ge=0, le=120)
    amount_cents: int = Field(default=0, ge=0)
    currency: str = "usd"
    description: str = ""


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings

    def _compose_url(payload: Dict[str, Any]) -> str:
        """Build a connection URL from form fields.

        Delegates to the engine registry rather than formatting a string here. This
        used to hard-code ``postgresql://`` and port 5432, which meant the form
        could only ever create a Postgres workspace no matter what was typed.
        """
        from vanna.core.datasource import get_engine

        engine = get_engine(str(payload.get("engine") or "postgres"))
        if engine is None:
            return ""
        return engine.url({k: str(v or "") for k, v in payload.items()})

    # ------------------------------------------------------------------
    # Workspaces
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants")
    async def admin_list_tenants(request: Request) -> Dict[str, Any]:
        """Every workspace with its headline numbers. Platform admin."""
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        # One query, not two per workspace in a Python loop.
        return {"tenants": await deps.require_directory().list_tenants_with_usage()}

    @app.post("/api/vanna/v2/admin/tenants")
    async def admin_create_tenant(payload: TenantPayload, request: Request) -> Dict[str, Any]:
        """Create a workspace. Platform admin."""
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        directory = deps.require_directory()

        if await directory.get_tenant(payload.id):
            raise HTTPException(status_code=409, detail="That workspace id already exists")

        database_url = payload.database_url or _compose_url(
            payload.model_dump(by_alias=True, include=CONNECTION_FIELDS)
        )

        try:
            row = await directory.create_tenant(
                payload.id,
                payload.name,
                description=payload.description,
                database_url=database_url or None,
                daily_quota=payload.daily_quota,
                max_rows=payload.max_rows,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # The creator becomes the first admin. A workspace nobody can administer is
        # a support ticket waiting to happen.
        await directory.add_user(payload.id, user.email or user.id, role="admin")
        await deps.admin_audit.record(
            "tenant.create",
            actor_email=user.email,
            tenant_id=payload.id,
            target=payload.id,
            details={"data_source": describe_data_source(database_url)},
            actor_ip=deps.client_ip(request),
        )
        return {"tenant": {"id": row["id"], "name": row["name"]}}

    @app.patch("/api/vanna/v2/admin/tenants/{tenant_id}")
    async def admin_update_tenant(
        tenant_id: str, payload: TenantUpdate, request: Request
    ) -> Dict[str, Any]:
        """Update a workspace.

        Two of these fields are platform decisions rather than tenant-admin ones:
        repointing a workspace at another database changes what every member can
        read, and granting writes changes what the workspace is permitted to do. An
        admin must not be able to grant their own workspace either.
        """
        user = await deps.caller(request)
        directory = deps.require_directory()

        changes = payload.model_dump(exclude_unset=True)
        supplied = {k: changes.pop(k) for k in list(changes) if k in CONNECTION_FIELDS}
        # The engine decides which fields are required, so "did they fill the form
        # in" is its question to answer, not a hard-coded host/database check --
        # SQLite has neither.
        if supplied and not changes.get("database_url"):
            composed = _compose_url({**supplied, "schema": supplied.get("schema_name")})
            if composed:
                changes["database_url"] = composed

        privileged = {"database_url", "allow_writes"} & set(changes)
        if privileged:
            require_platform_admin(user, settings)
        else:
            require_tenant_admin(user, tenant_id, settings)

        before = await directory.get_tenant(tenant_id)
        if before is None:
            raise HTTPException(status_code=404, detail="Not found")

        row = await directory.update_tenant(tenant_id, changes)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")

        if "database_url" in changes:
            # Drop the cached runtime so the next question uses the new connection
            # instead of the one built at first use.
            await deps.runtime_for(tenant_id, invalidate=True)

        await deps.admin_audit.record(
            "tenant.rebind" if "database_url" in changes else "tenant.update",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=tenant_id,
            details={
                # `changes` may hold a connection URL; `redact` in the audit writer
                # catches it by key name, and this keeps the shape readable.
                "fields": sorted(changes),
                "data_source": describe_data_source(row.get("database_url")),
                "allow_writes": row.get("allow_writes"),
            },
            actor_ip=deps.client_ip(request),
        )
        if changes.get("allow_writes") is True:
            logger.warning(
                "WRITES GRANTED to workspace %s by %s", tenant_id, user.email
            )
        return {"tenant": {"id": row["id"], "name": row["name"]}}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}")
    async def admin_delete_tenant(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        if tenant_id == settings.default_tenant:
            raise HTTPException(
                status_code=400, detail="The default workspace cannot be deleted"
            )
        if not await deps.require_directory().delete_tenant(tenant_id):
            raise HTTPException(status_code=404, detail="Not found")
        await deps.platform.runtime_for(tenant_id, invalidate=True)
        await deps.admin_audit.record(
            "tenant.delete",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=tenant_id,
            actor_ip=deps.client_ip(request),
        )
        return {"deleted": True}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/data")
    async def admin_purge_tenant_data(
        tenant_id: str, request: Request, confirm: str = ""
    ) -> Dict[str, Any]:
        """Erase everything a workspace generated. Platform admin.

        Deleting a workspace deliberately leaves ``generations`` behind so the
        record of what was asked survives -- correct for audit, wrong when somebody
        exercises a right to erasure. This is the explicit, separate operation, and
        it asks for the workspace id back before doing anything.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        if confirm != tenant_id:
            raise HTTPException(
                status_code=400,
                detail=f"Pass ?confirm={tenant_id} to erase this workspace's data. "
                       "This cannot be undone.",
            )
        deleted = await deps.require_directory().purge_tenant_data(tenant_id)
        await deps.admin_audit.record(
            "tenant.purge_data",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=tenant_id,
            details=deleted,
            actor_ip=deps.client_ip(request),
        )
        return {"deleted": deleted}

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/users")
    async def admin_list_users(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return {"users": await deps.require_directory().list_users(tenant_id)}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/users")
    async def admin_add_user(
        tenant_id: str, payload: UserPayload, request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        directory = deps.require_directory()
        if not await directory.get_tenant(tenant_id):
            raise HTTPException(status_code=404, detail="Not found")
        try:
            member = await directory.add_user(
                tenant_id, payload.email, full_name=payload.full_name, role=payload.role
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        await deps.admin_audit.record(
            "member.add",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=member["email"],
            details={"role": member["role"]},
            actor_ip=deps.client_ip(request),
        )
        return {
            "user": {
                "id": str(member["id"]),
                "email": member["email"],
                "role": member["role"],
            }
        }

    @app.patch("/api/vanna/v2/admin/tenants/{tenant_id}/users/{user_id}")
    async def admin_update_user(
        tenant_id: str, user_id: str, payload: UserUpdate, request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        directory = deps.require_directory()

        changes = payload.model_dump(exclude_unset=True)
        # Demoting or disabling the last admin locks the workspace out of its own
        # settings, and only a platform admin could then fix it. Refuse.
        if changes.get("role") not in (None, "admin") or changes.get("is_active") is False:
            await _guard_last_admin(directory, tenant_id, user_id)

        try:
            member = await directory.update_user(tenant_id, user_id, changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if member is None:
            raise HTTPException(status_code=404, detail="Not found")

        await deps.admin_audit.record(
            "member.role_change" if "role" in changes else "member.update",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=member["email"],
            details=changes,
            actor_ip=deps.client_ip(request),
        )
        return {
            "user": {
                "id": str(member["id"]),
                "email": member["email"],
                "role": member["role"],
            }
        }

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/users/{user_id}")
    async def admin_remove_user(
        tenant_id: str, user_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        directory = deps.require_directory()
        await _guard_last_admin(directory, tenant_id, user_id)
        if not await directory.remove_user(tenant_id, user_id):
            raise HTTPException(status_code=404, detail="Not found")
        await deps.admin_audit.record(
            "member.remove",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=user_id,
            actor_ip=deps.client_ip(request),
        )
        return {"deleted": True}

    async def _guard_last_admin(directory: Any, tenant_id: str, user_id: str) -> None:
        """Refuse a change that would leave the workspace with no active admin."""
        members = await directory.list_users(tenant_id)
        target = next((m for m in members if str(m["id"]) == str(user_id)), None)
        if target is None or target["role"] != "admin" or not target["is_active"]:
            return
        if await directory.count_admins(tenant_id) <= 1:
            raise HTTPException(
                status_code=400,
                detail="This is the workspace's last admin. Promote another member first.",
            )

    # ------------------------------------------------------------------
    # Starters
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/starters")
    async def admin_list_starters(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return {"starters": await deps.require_directory().list_starters(tenant_id)}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/starters")
    async def admin_add_starter(
        tenant_id: str, payload: StarterPayload, request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        directory = deps.require_directory()
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="A question is required")
        await directory.add_starter(tenant_id, question, payload.sort_order)
        return {"starters": await directory.list_starters(tenant_id)}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/starters/{starter_id}")
    async def admin_delete_starter(
        tenant_id: str, starter_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        if not await deps.require_directory().delete_starter(tenant_id, starter_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    # ------------------------------------------------------------------
    # Usage, cost and audit
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/usage")
    async def admin_tenant_usage(
        tenant_id: str, request: Request, days: int = 30
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return await deps.require_directory().tenant_usage(
            tenant_id, days=min(max(days, 1), 365)
        )

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/spend")
    async def admin_tenant_spend(
        tenant_id: str, request: Request, days: int = 30
    ) -> Dict[str, Any]:
        """What this workspace's questions cost. Platform admin.

        The columns have existed since the beginning and nothing populated or read
        them, so the number that decides pricing was uncollectable.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        return await deps.require_directory().spend(tenant_id, days=min(max(days, 1), 365))

    @app.get("/api/vanna/v2/admin/audit")
    async def admin_audit_log(
        request: Request,
        tenant_id: str = "",
        action: str = "",
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Administrative actions.

        A platform admin passing no workspace sees everything; anybody else is
        scoped to their own, and asking for another one is a 404.
        """
        user = await deps.caller(request)
        if tenant_id or not is_platform_admin(user, settings):
            scope = visible_tenant(user, tenant_id or None, settings)
            require_tenant_admin(user, scope, settings)
        else:
            scope = ""
        return {
            "events": await deps.admin_audit.recent(
                tenant_id=scope, action=action, limit=limit
            )
        }

    @app.get("/api/vanna/v2/admin/access-log")
    async def admin_access_log(
        request: Request,
        tenant_id: str = "",
        denied_only: bool = False,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Agent-level tool invocations and access decisions, for one workspace.

        ``tenant_id`` is resolved the same way ``/admin/audit`` resolves it, and
        defaults to the caller's own workspace. It used to be unconditionally
        ``user.tenant_id``: a platform admin administering somebody else's
        workspace was shown *their own* access log under that workspace's heading,
        with nothing on the screen to say so.
        """
        user = await deps.caller(request)
        scope = visible_tenant(user, tenant_id or None, settings)
        require_tenant_admin(user, scope, settings)
        if deps.agent_audit is None:
            return {"events": []}
        return {
            "events": await deps.agent_audit.recent(
                scope, limit=limit, denied_only=denied_only
            )
        }

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/billing")
    async def admin_billing(tenant_id: str, request: Request) -> Dict[str, Any]:
        """Plan, limits in force, usage against them, and payment history.

        Tenant admin: a workspace's own admin should be able to see what their
        workspace is on and what it has been charged. *Changing* any of it is
        platform admin -- see the routes below.
        """
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        store = deps.require_billing()

        from vanna.core.billing import PLANS, resolve_limits

        tenant = await deps.require_directory().get_tenant(tenant_id)
        subscription = await store.get_subscription(tenant_id)
        limits = resolve_limits(
            tenant,
            subscription,
            default_quota=settings.daily_quota,
            default_max_rows=settings.max_rows,
        )

        return {
            "subscription": subscription,
            "plan": limits.plan.name,
            "limits": {
                "daily_quota": limits.daily_quota,
                "max_rows": limits.max_rows,
                "quota_source": limits.quota_source,
                "rows_source": limits.rows_source,
            },
            "usage": await deps.require_directory().tenant_usage(tenant_id, days=30),
            "payments": await store.list_payments(tenant_id),
            "can_change": is_platform_admin(user, settings),
            "available_plans": [
                {
                    "name": p.name,
                    "label": p.label,
                    "daily_quota": p.daily_quota,
                    "max_rows": p.max_rows,
                    "description": p.description,
                }
                for p in PLANS.values()
            ],
        }

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/billing/plan")
    async def admin_set_plan(
        tenant_id: str, payload: PlanPayload, request: Request
    ) -> Dict[str, Any]:
        """Put a workspace on a plan. **Platform admin.**

        This used to require only tenant admin, which made the plan self-serve: a
        workspace admin could grant themselves the enterprise quota and row cap
        without paying, and the only record was a log line.

        The runtime is rebuilt afterwards because the row cap is baked into the SQL
        runner and the policy's default LIMIT when they are constructed.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        store = deps.require_billing()

        try:
            subscription = await store.set_subscription(
                tenant_id, payload.plan, months=payload.months
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        await deps.runtime_for(tenant_id, invalidate=True)
        await deps.admin_audit.record(
            "billing.plan",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=payload.plan,
            details={"months": payload.months},
            actor_ip=deps.client_ip(request),
        )
        logger.info(
            "Plan for %s set to %s by %s", tenant_id, subscription.get("plan"), user.email
        )
        return {"subscription": subscription}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/billing/cancel")
    async def admin_cancel_plan(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        cancelled = await deps.require_billing().cancel_subscription(tenant_id)
        await deps.runtime_for(tenant_id, invalidate=True)
        await deps.admin_audit.record(
            "billing.cancel",
            actor_email=user.email,
            tenant_id=tenant_id,
            target=tenant_id,
            actor_ip=deps.client_ip(request),
        )
        logger.info("Subscription for %s cancelled by %s", tenant_id, user.email)
        return {"cancelled": cancelled}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/billing/payments")
    async def admin_record_payment(
        tenant_id: str, payload: PaymentPayload, request: Request
    ) -> Dict[str, Any]:
        """Record a payment taken elsewhere, and extend the subscription.

        **Platform admin**, for the same reason as the plan route: whoever can
        record a payment can extend a subscription.

        Idempotent on the reference. Recording the same reference twice returns
        ``recorded: false`` and does *not* extend again -- which is the whole point
        of the unique constraint, and the behaviour a retried webhook or a
        double-clicked button depends on.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        store = deps.require_billing()

        from ..billing import build_provider

        provider = build_provider(settings.payment_provider)

        try:
            charge = await provider.charge(
                tenant_id,
                payload.plan or "free",
                payload.months,
                reference=payload.reference,
                amount_cents=payload.amount_cents,
                currency=payload.currency,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        recorded = await store.record_payment(
            tenant_id,
            provider=provider.name,
            provider_ref=charge["provider_ref"],
            amount_cents=charge["amount_cents"],
            currency=charge["currency"],
            status=charge["status"],
            description=payload.description,
        )

        # Only a payment that was actually new moves the expiry. A replay must not
        # buy another month.
        subscription = None
        if recorded:
            if payload.plan:
                try:
                    await store.set_subscription(
                        tenant_id, payload.plan, months=payload.months
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            else:
                await store.extend(tenant_id, payload.months)
            subscription = await store.get_subscription(tenant_id)
            await deps.runtime_for(tenant_id, invalidate=True)
            await deps.admin_audit.record(
                "billing.payment",
                actor_email=user.email,
                tenant_id=tenant_id,
                target=charge["provider_ref"],
                details={
                    "amount_cents": charge["amount_cents"],
                    "currency": charge["currency"],
                    "months": payload.months,
                },
                actor_ip=deps.client_ip(request),
            )
            logger.info(
                "Payment recorded for %s (%s) by %s",
                tenant_id, charge["provider_ref"], user.email,
            )

        return {
            "recorded": recorded,
            "subscription": subscription,
            "payments": await store.list_payments(tenant_id),
            "note": (
                ""
                if recorded
                else "That reference was already recorded. Nothing was charged or extended."
            ),
        }

    # ------------------------------------------------------------------
    # Datasources
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/engines")
    async def admin_engines(request: Request) -> Dict[str, Any]:
        """Every supported engine and the fields it needs.

        Drives the connection form, so the form and the URL builder cannot disagree
        about what an engine requires -- they read the same registry.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)

        from vanna.core.datasource import all_engines

        return {"engines": all_engines()}

    @app.post("/api/vanna/v2/admin/datasources/test")
    async def test_datasource(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """Try a connection before anything is stored.

        A workspace bound to an unreachable database is indistinguishable from a
        broken deployment, and the person who can tell them apart is the one filling
        in this form.

        Returns a sanitised error -- driver messages routinely echo the DSN,
        password included.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)

        url = str(payload.get("database_url") or "").strip() or _compose_url(payload)
        if not url:
            raise HTTPException(
                status_code=400, detail="Fill in the required connection fields first."
            )

        from vanna.capabilities.sql_runner import RunSqlToolArgs
        from vanna.core.errors import ErrorPhase, VannaError

        await deps.admin_audit.record(
            "datasource.test",
            actor_email=user.email,
            target=describe_data_source(url),
            actor_ip=deps.client_ip(request),
        )

        try:
            # The same builder the per-tenant runtime uses, so a connection that
            # tests green here is one the workspace can actually be created on.
            from vanna.core.datasource.runners import probe

            runner = probe(url)
            context = await deps.tool_context(user)
            await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context)
        except Exception as exc:
            error = VannaError.from_exception(exc, phase=ErrorPhase.PROFILE_RESOLUTION)
            logger.info("Datasource test failed for %s: %s", user.email, error)
            return {
                "ok": False,
                # The message is sanitised; the URL is never echoed back.
                "error": error.args[0] if error.args else "Could not connect.",
            }

        return {"ok": True, "data_source": describe_data_source(url)}

    @app.get("/api/vanna/v2/admin/datasources")
    async def admin_datasources(request: Request) -> Dict[str, Any]:
        """Databases on the configured server, as binding suggestions.

        Saves an operator from hand-typing a connection string for a database that
        already exists next to the one we are connected to. Platform admin only --
        it enumerates the server.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)

        base = settings.database_url
        if not base.startswith("postgres"):
            return {"datasources": [], "note": "Only PostgreSQL servers are enumerated."}

        from urllib.parse import urlsplit, urlunsplit

        import psycopg2

        parts = urlsplit(base)
        suggestions: List[Dict[str, str]] = []
        try:
            connection = psycopg2.connect(base, connect_timeout=5)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT datname FROM pg_database "
                        "WHERE NOT datistemplate AND datallowconn ORDER BY datname"
                    )
                    for (name,) in cursor.fetchall():
                        suggestions.append(
                            {
                                "name": name,
                                "url": urlunsplit(
                                    (parts.scheme, parts.netloc, f"/{name}", "", "")
                                ),
                                "label": f"{parts.hostname}/{name}",
                            }
                        )
            finally:
                connection.close()
        except Exception as exc:
            logger.warning("Could not enumerate databases: %s", exc)
            return {"datasources": [], "note": str(exc)[:200]}

        return {"datasources": suggestions}

    # ------------------------------------------------------------------
    # A workspace's databases
    # ------------------------------------------------------------------
    #
    # Platform admin, deliberately. The README states the rule these follow: a
    # workspace admin "cannot change their own plan, grant their own workspace
    # write access, or repoint it at another database. Those are platform
    # decisions." Adding a database to a workspace is the same decision as
    # repointing it, so it sits behind the same check.
    #
    # The permission matrix is not: that is workspace-level, and its endpoints in
    # `routes/grants.py` take a `data_source_id` naming one of the databases
    # registered here.

    def _registry() -> Any:
        registry = getattr(deps.platform, "datasources", None)
        if registry is None:
            raise HTTPException(
                status_code=503,
                detail="Registering databases needs the control-plane database.",
            )
        return registry

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/datasources")
    async def list_workspace_datasources(
        tenant_id: str, request: Request
    ) -> Dict[str, Any]:
        """Every database this workspace may be asked about. Platform admin.

        Credential-free: the connection strings are encrypted at rest and are not
        returned here, only the derived label.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        return {"data_sources": await _registry().list_sources(tenant_id)}

    @app.get(
        "/api/vanna/v2/admin/tenants/{tenant_id}/datasources/{data_source_id:path}/health"
    )
    async def check_workspace_datasource(
        tenant_id: str, data_source_id: str, request: Request
    ) -> Dict[str, Any]:
        """Try to reach one database now, and remember the answer.

        A source was probed once, when it was registered, and the result was
        discarded with the request. So a rotated password or a moved host looked
        exactly like a healthy source until somebody asked a question and got an
        error they had no way to interpret.

        A workspace admin may run this -- it is a fact about their own workspace,
        it reveals nothing a member cannot already infer from a failing question,
        and the person who needs it at 9am is not the platform administrator.
        Registering a database stays platform-only; asking whether it answers does
        not.
        """
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        try:
            result = await _registry().check(tenant_id, data_source_id)
        except UnknownDataSource as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"data_source_id": data_source_id, **result}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/datasources", status_code=201)
    async def add_workspace_datasource(
        tenant_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        """Register another database for a workspace. Platform admin.

        The connection is probed before it is stored. A workspace bound to an
        unreachable database is indistinguishable from a broken deployment, and
        the person who can tell them apart is the one filling in this form --
        which is the same argument ``/admin/datasources/test`` already makes.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)

        url = str(payload.get("database_url") or "").strip() or _compose_url(payload)
        if not url:
            raise HTTPException(
                status_code=400, detail="Fill in the required connection fields first."
            )

        from vanna.capabilities.sql_runner import RunSqlToolArgs
        from vanna.core.datasource.runners import probe

        try:
            # `probe` only *builds* a runner -- it is synchronous and connects
            # nothing. Awaiting it rejected every URL with a TypeError, valid ones
            # included. The statement below is the actual test, and is the same one
            # `/admin/datasources/test` runs.
            runner = probe(url)
            await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), await deps.tool_context(user))
        except Exception as exc:
            # Sanitised: driver messages routinely echo the DSN, password included.
            raise HTTPException(
                status_code=400,
                detail=f"Could not connect: {type(exc).__name__}.",
            ) from exc

        registered = await _registry().register(
            tenant_id,
            url,
            label=str(payload.get("label") or "").strip(),
            is_default=bool(payload.get("is_default")),
        )

        await deps.admin_audit.record(
            "datasource.add",
            actor_email=user.email,
            target=f"{tenant_id}:{registered['data_source_id']}",
            actor_ip=deps.client_ip(request),
        )
        # The workspace's cached runtimes predate this database; drop them so the
        # next request sees the new registry rather than a stale default.
        await deps.runtime_for(tenant_id, invalidate=True)
        return registered

    @app.patch("/api/vanna/v2/admin/tenants/{tenant_id}/datasources/{data_source_id:path}")
    async def update_workspace_datasource(
        tenant_id: str, data_source_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        """Rename one, or make it the workspace default. Platform admin."""
        user = await deps.caller(request)
        require_platform_admin(user, settings)

        registry = _registry()
        current = {s["data_source_id"]: s for s in await registry.list_sources(tenant_id)}
        if data_source_id not in current:
            raise HTTPException(status_code=404, detail="No such database here.")

        resolved = await registry.resolve(tenant_id, data_source_id)
        url = resolved["database_url"]
        label = payload.get("label")
        await registry.register(
            tenant_id,
            url,
            label=str(label if label is not None else current[data_source_id]["label"]),
            is_default=bool(payload.get("is_default", current[data_source_id]["is_default"])),
        )
        await deps.runtime_for(tenant_id, invalidate=True)
        return {"data_sources": await registry.list_sources(tenant_id)}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/datasources/{data_source_id:path}")
    async def remove_workspace_datasource(
        tenant_id: str, data_source_id: str, request: Request
    ) -> Dict[str, Any]:
        """Stop offering a database. Platform admin.

        The grants written against it are left alone. They are keyed on the data
        source, so they neither apply to anything else nor come back wrong if the
        database is registered again -- and deleting somebody's permission matrix
        as a side effect of tidying a connection list would be a poor trade.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)

        registry = _registry()
        sources = await registry.list_sources(tenant_id)
        if len(sources) <= 1:
            raise HTTPException(
                status_code=400,
                detail="A workspace needs at least one database. Add another first.",
            )
        if not await registry.remove(tenant_id, data_source_id):
            raise HTTPException(status_code=404, detail="No such database here.")

        await deps.admin_audit.record(
            "datasource.remove",
            actor_email=user.email,
            target=f"{tenant_id}:{data_source_id}",
            actor_ip=deps.client_ip(request),
        )
        await deps.runtime_for(tenant_id, invalidate=True)
        return {"removed": data_source_id}
