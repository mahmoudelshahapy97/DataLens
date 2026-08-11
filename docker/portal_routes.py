"""HTTP surface for the portal: identity, tenancy, schema, history, saved SQL.

Split from ``tenancy.py`` on purpose -- that module owns storage and knows
nothing about HTTP, this one owns HTTP and holds no state. The knowledge-review
endpoints stay where they were, in ``vanna.servers.fastapi.admin_routes``; these
are the routes the *product* needs that the library does not ship.

Authorisation model
-------------------

Two tiers, because "admin" means two different things in a multi-tenant system:

* **Platform admin** -- an address in ``VANNA_ADMIN_EMAILS``. May create and
  delete tenants and manage members of any of them. This is the operator.
* **Tenant admin** -- ``role = 'admin'`` on a ``tenant_users`` row. May manage
  members, starters and knowledge *for their own tenant only*.

Every write route names which tier it needs. Nothing derives permission from
what the browser sent beyond the identity the resolver produced, and no route
trusts a ``tenant_id`` in the path without checking it against the caller.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Protocol

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from tenancy import Directory, PostgresGenerationStore, describe_data_source

logger = logging.getLogger("vanna.portal")

#: When true, ``GET /tenants/{id}/users`` returns the member list without
#: authentication so the sign-in screen can offer a click-to-pick roster. That
#: is a convenience for a demo and an email-address disclosure in production --
#: which is why it is a flag and why it defaults off outside the demo compose
#: file. Real deployments authenticate first and never need it.
PUBLIC_ROSTER = os.getenv("VANNA_PUBLIC_USER_DIRECTORY", "false").lower() == "true"


# ----------------------------------------------------------------------
# Payloads
# ----------------------------------------------------------------------


class RuntimeProvider(Protocol):
    """``Platform.runtime_for`` -- resolves a tenant to its agent and runner."""

    async def __call__(self, tenant_id: str, *, invalidate: bool = False) -> Any: ...


class TenantPayload(BaseModel):
    id: str
    name: str
    description: str = ""
    database_url: Optional[str] = None
    daily_quota: Optional[int] = None
    max_rows: Optional[int] = None

    # Structured connection fields, composed into a URL server-side -- so the
    # browser never assembles one and percent-encoding happens once, correctly.
    host: Optional[str] = None
    port: Optional[str] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    sslmode: Optional[str] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    database_url: Optional[str] = None
    is_active: Optional[bool] = None
    daily_quota: Optional[int] = None
    max_rows: Optional[int] = None
    allow_writes: Optional[bool] = None
    # Whether members may answer on their own LLM key. See the column comment in
    # tenancy.py -- turning it off is how a workspace keeps its schema and
    # questions away from accounts it does not control.
    allow_byo_key: Optional[bool] = None

    # Structured connection fields, composed into a URL server-side. The
    # browser never assembles one, so a password only ever travels as a single
    # form field and is never concatenated into a string the page holds.
    host: Optional[str] = None
    port: Optional[str] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    sslmode: Optional[str] = None


class UserPayload(BaseModel):
    email: str
    full_name: str = ""
    role: str = "analyst"


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class StarterPayload(BaseModel):
    question: str
    sort_order: int = 0


class SavedQueryPayload(BaseModel):
    title: str
    sql: str
    question: str = ""


class RunSqlPayload(BaseModel):
    sql: str
    limit: int = Field(default=200, ge=1, le=5000)


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------


def register_portal_routes(
    app: Any,
    *,
    directory: Optional[Directory],
    generation_store: Optional[PostgresGenerationStore],
    user_resolver: Any,
    runtime_for: RuntimeProvider,
    agent_memory: Any,
    conversation_store: Any = None,
    accounts: Any = None,
    billing: Any = None,
    platform_admin_emails: set,
    default_tenant: str,
) -> None:
    """Register every portal route.

    Args:
        directory: Control-plane directory, or None when the control plane is
            not configured. Routes that need it answer 503 rather than 500 --
            "this feature needs a database" is actionable, "internal error" is
            not.
        generation_store: Postgres generation store, for the history view.
        user_resolver: The same resolver the chat routes use, so identity and
            tenant scoping cannot drift between them.
        runtime_for: Returns the per-tenant runtime (runner, catalog, dialect)
            for a tenant id. Awaited lazily so a tenant whose database is down
            only breaks that tenant.
        agent_memory: Only needed to construct a ``ToolContext``; these routes
            never read or write memories. ``ToolContext`` requires one, and
            passing the real partitioned instance is cheaper than inventing a
            null object that would then need to satisfy the same interface.
        platform_admin_emails: Addresses that may administer every tenant.
        default_tenant: Tenant assumed when a request names none.
    """

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------

    async def _caller(request: Request):
        """Resolve the caller, or 401."""
        from vanna.core.user import RequestContext

        try:
            return await user_resolver.resolve_user(
                RequestContext(
                    headers=dict(request.headers),
                    cookies=dict(request.cookies),
                    metadata={},
                )
            )
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except Exception as exc:
            logger.warning("Identity resolution failed: %s", exc)
            raise HTTPException(status_code=401, detail="Could not resolve identity")

    def _is_platform_admin(user: Any) -> bool:
        email = (getattr(user, "email", "") or "").lower()
        # An empty allow-list means the deployment is unconfigured (a demo);
        # app.py already treats everyone as an admin in that case, and this
        # must agree with it or the console shows buttons that 403.
        return not platform_admin_emails or email in platform_admin_emails

    def _require_platform_admin(user: Any) -> None:
        if not _is_platform_admin(user):
            raise HTTPException(status_code=404, detail="Not found")

    def _require_tenant_admin(user: Any, tenant_id: str) -> None:
        """Admin of this specific tenant, or a platform admin."""
        if _is_platform_admin(user):
            return
        same_tenant = getattr(user, "tenant_id", None) == tenant_id
        is_admin = "admin" in (getattr(user, "group_memberships", None) or [])
        if not (same_tenant and is_admin):
            # 404 rather than 403, matching admin_routes: a 403 confirms the
            # tenant exists to someone who has no business knowing.
            raise HTTPException(status_code=404, detail="Not found")

    def _require_directory() -> Directory:
        if directory is None:
            raise HTTPException(
                status_code=503,
                detail="No control-plane database configured. Set VANNA_APP_DATABASE_URL.",
            )
        return directory

    async def _tool_context(user: Any):
        """A ToolContext for the caller, for catalog and runner calls."""
        from vanna.core.tool import ToolContext

        return ToolContext(
            user=user,
            conversation_id="portal",
            request_id=str(uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=agent_memory,
        )

    # ------------------------------------------------------------------
    # Sign-in surface
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/tenants")
    async def list_tenants() -> Dict[str, Any]:
        """Tenants available to sign in to.

        Unauthenticated by necessity -- it is what the sign-in screen reads
        before anyone has identified themselves. Returns names only.
        """
        if directory is None:
            return {
                "tenants": [{"id": default_tenant, "name": default_tenant.title()}],
                "control_plane": False,
            }
        return {"tenants": await directory.list_tenants(), "control_plane": True}

    @app.get("/api/vanna/v2/tenants/{tenant_id}/users")
    async def list_tenant_roster(tenant_id: str) -> Dict[str, Any]:
        """Member roster for the sign-in picker. Off unless explicitly enabled."""
        if not PUBLIC_ROSTER:
            raise HTTPException(status_code=404, detail="Not found")
        users = await _require_directory().list_users(tenant_id)
        return {
            "users": [
                {"email": u["email"], "full_name": u["full_name"], "role": u["role"]}
                for u in users
                if u["is_active"]
            ]
        }

    @app.get("/api/vanna/v2/me")
    async def whoami(request: Request) -> Dict[str, Any]:
        """Who the caller is, what they may do, and what they are querying.

        The portal calls this immediately after sign-in and treats a non-200 as
        "those credentials do not work here" -- so this is where a user who is
        not a member of the tenant they picked finds out.
        """
        user = await _caller(request)
        groups = list(getattr(user, "group_memberships", None) or [])

        tenant: Dict[str, Any] = {"id": user.tenant_id, "name": user.tenant_id}
        role = "admin" if "admin" in groups else "analyst"
        memberships: List[str] = []

        if directory is not None:
            row = await directory.get_tenant(user.tenant_id)
            if row:
                tenant = {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "data_source": describe_data_source(row["database_url"]),
                    # So the account screen can hide the personal-key form when
                    # the workspace forbids it. The server enforces this
                    # independently -- hiding a form is a courtesy, not a control.
                    "allow_byo_key": row.get("allow_byo_key", True),
                }
            member = await directory.get_member(user.tenant_id, user.email or user.id)
            if member:
                role = member["role"]
                await directory.touch_last_seen(user.tenant_id, member["email"])
            memberships = await directory.tenants_for_email(user.email or user.id)

        return {
            "user": {
                "id": user.id,
                "email": user.email,
                "name": getattr(user, "username", None) or "",
                "role": role,
            },
            "tenant": tenant,
            "memberships": memberships,
            "is_platform_admin": _is_platform_admin(user),
            "is_admin": "admin" in groups,
            "control_plane": directory is not None,
        }

    @app.get("/api/vanna/v2/starters")
    async def starters(request: Request) -> Dict[str, Any]:
        """Suggested questions for the caller's tenant."""
        user = await _caller(request)
        if directory is None:
            return {"starters": []}
        return {"starters": await directory.list_starters(user.tenant_id)}

    # ------------------------------------------------------------------
    # Schema explorer
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/schema")
    async def schema(request: Request, layer: str = "active") -> Dict[str, Any]:
        """The catalog for the caller's tenant.

        Reads the catalog, never the database. The catalog is what the model is
        actually shown, so a user browsing it is seeing the same picture the
        agent has -- including where a scan found nothing, which is the usual
        explanation for a bad answer.

        ``layer=physical`` asks for the raw scan even when a semantic layer is
        active. Useful for checking that a model points where its author
        thought; the agent still only ever sees the active layer.
        """
        user = await _caller(request)
        runtime = await runtime_for(user.tenant_id)
        ctx = await _tool_context(user)

        catalog = runtime.catalog
        is_semantic = type(catalog).__name__ == "SemanticSchemaCatalog"
        if layer == "physical" and is_semantic:
            catalog = getattr(catalog, "physical", None) or catalog

        tables = await catalog.get_tables(ctx)
        relationships = await catalog.get_relationships(ctx)

        return {
            "dialect": runtime.dialect,
            "data_source": runtime.data_source,
            # Tells the UI whether a Semantic/Physical toggle is meaningful
            # and which side it is currently showing.
            "semantic": is_semantic,
            # The backend really serving retrieval. Reported because a
            # configured-but-unavailable vector index silently degrades to
            # keyword search, and that is invisible from the outside.
            "index_backend": getattr(
                getattr(catalog, "index", None), "name", "none"
            ),
            "layer": "physical" if (layer == "physical" and is_semantic) else
                     ("semantic" if is_semantic else "physical"),
            "tables": [
                {
                    "name": t.table_name,
                    "schema": t.schema_name,
                    "description": t.description,
                    "row_count_estimate": t.row_count_estimate,
                    "last_synced_at": (
                        t.last_synced_at.isoformat() if t.last_synced_at else None
                    ),
                    "columns": [
                        {
                            "name": c.name,
                            "data_type": c.data_type,
                            "nullable": c.nullable,
                            "is_primary_key": c.is_primary_key,
                            "description": c.description,
                            "categories": c.categories,
                            "sample_values": c.sample_values,
                            "foreign_key": (
                                {
                                    "column": c.foreign_key.column,
                                    "references_table": c.foreign_key.references_table,
                                    "references_column": c.foreign_key.references_column,
                                }
                                if c.foreign_key
                                else None
                            ),
                        }
                        for c in t.columns
                    ],
                }
                for t in tables
            ],
            "relationships": [
                {
                    "name": r.name,
                    "from_table": r.from_table,
                    "from_column": r.from_column,
                    "to_table": r.to_table,
                    "to_column": r.to_column,
                    "join_type": r.join_type,
                }
                for r in relationships
            ],
        }

    # ------------------------------------------------------------------
    # Semantic layer
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/semantic/manifest")
    async def semantic_manifest(request: Request) -> Dict[str, Any]:
        """The manifest this tenant's agent is using.

        Read-only. A manifest is authored in git and compiled by
        ``vanna project build``; editing it over HTTP would put the definition
        of record somewhere nobody reviews.
        """
        user = await _caller(request)
        runtime = await runtime_for(user.tenant_id)
        manifest = getattr(runtime, "manifest", None) or _manifest_of(runtime)

        if manifest is None:
            return {"semantic": False, "models": [], "cubes": []}

        return {
            "semantic": True,
            "models": [
                {
                    "name": model.name,
                    "description": model.description,
                    "source": model.source,
                    "primary_key": model.primary_key,
                    "columns": [
                        {
                            "name": column.name,
                            "type": column.type,
                            "description": column.description,
                            "calculated": column.is_calculated,
                            "expression": column.expression if column.is_calculated else None,
                            "categories": column.categories,
                        }
                        for column in model.visible_columns
                    ],
                    "relationships": [
                        {"name": c.name, "target": c.type, "via": c.relationship}
                        for c in model.relationship_columns
                    ],
                    "row_rules": [r.name for r in model.row_level_access_controls],
                }
                for model in manifest.models
            ],
            "relationships": [
                {
                    "name": r.name,
                    "models": r.models,
                    "join_type": r.join_type.value,
                    "condition": r.condition,
                }
                for r in manifest.relationships
            ],
            "cubes": [
                {
                    "name": cube.name,
                    "base_object": cube.base_object,
                    "measures": [
                        {"name": m.name, "expression": m.expression} for m in cube.measures
                    ],
                    "dimensions": [d.name for d in cube.dimensions],
                    "time_dimensions": [d.name for d in cube.time_dimensions],
                }
                for cube in manifest.cubes
            ],
            "views": [v.name for v in manifest.views],
        }

    @app.post("/api/vanna/v2/semantic/compile")
    async def semantic_compile(payload: RunSqlPayload, request: Request) -> Dict[str, Any]:
        """Show what a semantic statement compiles to, without running it.

        The debugging endpoint for the compiler. Admin-only because the output
        is the physical schema -- table names, join conditions, and any row
        predicate that was injected.
        """
        user = await _caller(request)
        _require_tenant_admin(user, user.tenant_id)

        runtime = await runtime_for(user.tenant_id)
        registry = getattr(runtime.agent, "tool_registry", None)
        compile_for = getattr(registry, "compile_for", None)
        if compile_for is None:
            raise HTTPException(
                status_code=503,
                detail="This workspace is not using a semantic layer.",
            )

        from vanna.core.errors import VannaError

        ctx = await _tool_context(user)
        try:
            compiled = compile_for(payload.sql, user, ctx)
        except VannaError as exc:
            # redact=False: an admin debugging the compiler is exactly who the
            # semantic and dialect SQL are for.
            raise HTTPException(status_code=400, detail=exc.to_dict(redact=False))

        if compiled is None:
            raise HTTPException(status_code=503, detail="No manifest configured.")

        return {
            "sql": compiled.sql,
            "dialect": compiled.dialect,
            "referenced_models": compiled.referenced_models,
            "referenced_views": compiled.referenced_views,
            "applied_row_rules": compiled.applied_row_rules,
            "dropped_columns": compiled.dropped_columns,
            "warnings": [
                {"code": w.code, "message": w.message} for w in compiled.warnings
            ],
        }

    @app.post("/api/vanna/v2/admin/access/preview")
    async def access_preview(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """The SQL a *given* user's question would actually run.

        The most useful thing in the access-control feature. Without it, an
        operator confirms a rule works by noticing that someone sees fewer rows,
        which is indistinguishable from the rule being broken in the other
        direction. This shows the predicate.
        """
        caller = await _caller(request)
        _require_tenant_admin(caller, caller.tenant_id)

        target_email = str(payload.get("email") or "").strip().lower()
        sql = str(payload.get("sql") or "").strip()
        if not target_email or not sql:
            raise HTTPException(status_code=400, detail="email and sql are required")

        directory_ = _require_directory()
        member = await directory_.get_member(caller.tenant_id, target_email)
        if member is None:
            raise HTTPException(
                status_code=404, detail=f"{target_email} is not a member of this workspace."
            )

        from vanna.core.errors import VannaError
        from vanna.core.tool import ToolContext
        from vanna.core.user import User

        subject = User(
            id=member["email"],
            email=member["email"],
            tenant_id=caller.tenant_id,
            group_memberships=["user"] + (["admin"] if member["role"] == "admin" else []),
            metadata=dict(member.get("attributes") or {}),
        )

        runtime = await runtime_for(caller.tenant_id)
        registry = getattr(runtime.agent, "tool_registry", None)
        compile_for = getattr(registry, "compile_for", None)
        if compile_for is None:
            raise HTTPException(
                status_code=503, detail="This workspace is not using a semantic layer."
            )

        ctx = ToolContext(
            user=subject,
            conversation_id="access-preview",
            request_id=str(uuid.uuid4()),
            tenant_id=caller.tenant_id,
            agent_memory=agent_memory,
        )

        try:
            compiled = compile_for(sql, subject, ctx)
        except VannaError as exc:
            # A refusal *is* the answer here: it shows the rule denied them.
            return {
                "email": target_email,
                "allowed": False,
                "reason": exc.args[0] if exc.args else str(exc),
            }

        return {
            "email": target_email,
            "allowed": True,
            "sql": compiled.sql,
            "applied_row_rules": compiled.applied_row_rules,
            "dropped_columns": compiled.dropped_columns,
        }

    def _manifest_of(runtime: Any):
        """The manifest behind a runtime, however it was wired."""
        registry = getattr(runtime.agent, "tool_registry", None)
        return getattr(registry, "manifest", None)

    @app.post("/api/vanna/v2/schema/rescan")
    async def rescan(request: Request) -> Dict[str, Any]:
        """Re-scan the tenant's database into its catalog. Tenant admin only."""
        user = await _caller(request)
        _require_tenant_admin(user, user.tenant_id)

        from vanna.capabilities.schema_catalog import SchemaScanner

        runtime = await runtime_for(user.tenant_id)
        ctx = await _tool_context(user)
        try:
            report = await SchemaScanner(runtime.runner, dialect=runtime.dialect).scan(
                ctx, runtime.catalog
            )
        except Exception as exc:
            logger.error("Rescan failed for %s: %s", user.tenant_id, exc)
            raise HTTPException(status_code=502, detail=f"Scan failed: {exc}")

        return {
            "tables_scanned": report.tables_scanned,
            "columns_profiled": report.columns_profiled,
            "relationships_found": report.relationships_found,
            "duration_ms": report.duration_ms,
            "errors": report.errors,
        }

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/history")
    async def history(
        request: Request,
        limit: int = 50,
        search: Optional[str] = None,
        mine: bool = False,
    ) -> Dict[str, Any]:
        """Past questions for the tenant, newest first.

        ``mine=true`` narrows to the caller. The default is the whole tenant
        because the point of shared history is seeing what colleagues already
        asked -- which is also why a viewer sees it: reading someone else's
        question is not a privilege escalation, they are all scoped to a tenant
        that everyone here belongs to.
        """
        user = await _caller(request)
        if generation_store is None:
            return {"history": [], "control_plane": False}
        rows = await generation_store.history(
            user.tenant_id,
            limit=limit,
            search=search,
            user_id=user.id if mine else None,
        )
        return {"history": rows, "control_plane": True}

    # ------------------------------------------------------------------
    # Saved queries
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/saved-queries")
    async def list_saved(request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        if directory is None:
            return {"saved": []}
        return {"saved": await directory.list_saved(user.tenant_id)}

    @app.post("/api/vanna/v2/saved-queries")
    async def create_saved(payload: SavedQueryPayload, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _forbid_viewer(user)
        title = payload.title.strip()
        sql = payload.sql.strip()
        if not title or not sql:
            raise HTTPException(status_code=400, detail="A title and SQL are required")
        saved = await _require_directory().save_query(
            user.tenant_id,
            title=title,
            sql=sql,
            question=payload.question.strip(),
            created_by=user.email or user.id,
        )
        return {"saved": saved}

    @app.delete("/api/vanna/v2/saved-queries/{saved_id}")
    async def delete_saved(saved_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _forbid_viewer(user)
        if not await _require_directory().delete_saved(user.tenant_id, saved_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    def _forbid_viewer(user: Any) -> None:
        """Viewers may read everything in their tenant and write nothing."""
        if getattr(user, "metadata", {}).get("role") == "viewer":
            raise HTTPException(status_code=403, detail="Viewers cannot modify saved queries")

    @app.get("/api/vanna/v2/usage")
    async def usage(request: Request) -> Dict[str, Any]:
        """This workspace's question count against its daily quota.

        Read from the control plane -- the same rows the history view uses --
        rather than from ``InMemoryQuotaHook``, which enforces the limit but is
        per-process. With more than one worker the hook's counter and any number
        shown here would disagree, and inventing a third source of truth would
        only add a way for them to disagree differently.
        """
        user = await _caller(request)
        if directory is None or generation_store is None:
            return {"enabled": False}

        row = await directory.db.fetch_one(
            "SELECT count(*) AS used FROM vanna_app.generations "
            "WHERE tenant_id = %s AND created_at > now() - interval '1 day'",
            (user.tenant_id,),
        )
        used = int((row or {}).get("used") or 0)

        from vanna.core.billing import resolve_limits

        tenant = await directory.get_tenant(user.tenant_id)
        subscription = (
            await billing.get_subscription(user.tenant_id) if billing else None
        )
        limits = resolve_limits(
            tenant,
            subscription,
            default_quota=int(os.getenv("VANNA_DAILY_QUOTA", "200")),
            default_max_rows=int(os.getenv("VANNA_MAX_ROWS", "1000")),
        )

        return {
            "enabled": True,
            "used": used,
            "limit": limits.daily_quota,
            "window": "24h",
            # Counted per workspace, not per person -- which is how the quota is
            # actually enforced, and saying so avoids the obvious misreading.
            "scope": "workspace",
            "plan": limits.plan.name,
            "plan_label": limits.plan.label,
            "max_rows": limits.max_rows,
            # A subscription that ran out falls back to free silently, which from
            # the user's side is indistinguishable from a bug. Saying so turns
            # "my limit dropped" into "the subscription ended".
            "expired": bool(
                subscription
                and limits.plan.name == "free"
                and (subscription.get("plan") or "free") != "free"
            ),
            # "Why am I capped at 200?" is the question this answers without
            # anyone having to read code: override, plan, or deployment default.
            "limit_source": limits.quota_source,
        }

    @app.get("/api/vanna/v2/prompt-preview")
    async def prompt_preview(request: Request, question: str = "") -> Dict[str, Any]:
        """Exactly what the model would be told for this question.

        Not a reconstruction -- this calls the same
        ``RetrievalContextEnhancer.build_context`` the agent uses, so what is
        shown is what would be sent, including which sections the token budget
        dropped. That last part is usually the interesting answer: "your
        examples were cut" explains a bad response better than the response
        does.

        Admin-only. The assembled context is the entire schema plus every
        business rule the workspace has.
        """
        user = await _caller(request)
        _require_tenant_admin(user, user.tenant_id)

        if not question.strip():
            raise HTTPException(status_code=400, detail="A question is required")

        runtime = await runtime_for(user.tenant_id)
        enhancer = getattr(runtime.agent, "llm_context_enhancer", None)
        if enhancer is None or not hasattr(enhancer, "build_context"):
            raise HTTPException(
                status_code=503,
                detail="This workspace does not use the retrieval enhancer.",
            )

        result = await enhancer.build_context(question, user)
        if result is None:
            return {
                "question": question,
                "sections": [],
                "tokens_used": 0,
                "budget": getattr(enhancer.budget, "total_tokens", 0),
                "note": "Nothing was retrieved for this question.",
            }

        budget = getattr(enhancer.budget, "total_tokens", 0)
        return {
            "question": question,
            "text": result.text,
            "tokens_used": result.tokens_used,
            "budget": budget,
            "sections": [
                {
                    "name": name,
                    "tokens": tokens,
                    "dropped_items": result.dropped_items.get(name, 0),
                    "truncated": name in (result.truncated_sections or []),
                }
                for name, tokens in result.section_tokens.items()
            ],
        }

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def _require_conversations():
        if conversation_store is None:
            raise HTTPException(
                status_code=503,
                detail="Conversation history needs a control-plane database.",
            )
        return conversation_store

    @app.get("/api/vanna/v2/conversations")
    async def list_conversations(request: Request, limit: int = 50) -> Dict[str, Any]:
        """This user's threads, newest first.

        Titles and counts only. A sidebar should not pay for every message in
        every thread.
        """
        user = await _caller(request)
        if conversation_store is None:
            return {"conversations": [], "persisted": False}
        return {
            "conversations": await conversation_store.summaries(
                user.tenant_id, user.id, limit=limit
            ),
            "persisted": True,
        }

    @app.get("/api/vanna/v2/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> Dict[str, Any]:
        """One thread's messages, for replaying the transcript."""
        user = await _caller(request)
        found = await _require_conversations().get_conversation(conversation_id, user)
        if found is None:
            # The store filters by owner, so another user's id is a 404 here --
            # which is the right answer, and does not confirm it exists.
            raise HTTPException(status_code=404, detail="Not found")

        return {
            "id": found.id,
            "title": (found.metadata or {}).get("title") or "",
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "timestamp": message.timestamp.isoformat(),
                }
                for message in found.messages
                # Tool traffic is machinery, not transcript. Replaying it would
                # show the reader a conversation they never had.
                if message.role in ("user", "assistant") and message.content
            ],
        }

    @app.patch("/api/vanna/v2/conversations/{conversation_id}")
    async def rename_conversation(
        conversation_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="A title is required")
        if not await _require_conversations().rename(
            user.tenant_id, user.id, conversation_id, title
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return {"renamed": True}

    @app.delete("/api/vanna/v2/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request) -> Dict[str, Any]:
        """Delete a thread and its messages together.

        SQL Chat deletes the conversation and orphans its messages, which live
        in a flat list keyed by id -- so they become unreachable and are never
        collected. Here the messages live inside the row and go with it.
        """
        user = await _caller(request)
        if not await _require_conversations().delete_conversation(conversation_id, user):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    # ------------------------------------------------------------------
    # Cubes
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/cubes")
    async def list_cubes(request: Request) -> Dict[str, Any]:
        """Cubes available to this workspace, with their measures."""
        user = await _caller(request)
        runtime = await runtime_for(user.tenant_id)
        manifest = _manifest_of(runtime)
        if manifest is None:
            return {"cubes": []}

        return {
            "cubes": [
                {
                    "name": cube.name,
                    "base_object": cube.base_object,
                    "description": cube.description,
                    "measures": [
                        {"name": m.name, "expression": m.expression,
                         "description": m.description}
                        for m in cube.measures
                    ],
                    "dimensions": [d.name for d in cube.dimensions],
                    "time_dimensions": [d.name for d in cube.time_dimensions],
                }
                for cube in manifest.cubes
            ]
        }

    @app.post("/api/vanna/v2/cubes/{cube_name}/query")
    async def query_cube(
        cube_name: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        """Compute measures grouped by dimensions.

        Takes names, not SQL. A caller cannot express an aggregate at the wrong
        grain through this endpoint, because the aggregation was decided when
        the cube was defined -- which is the whole reason cubes exist.
        """
        user = await _caller(request)
        runtime = await runtime_for(user.tenant_id)
        manifest = _manifest_of(runtime)
        if manifest is None:
            raise HTTPException(status_code=503, detail="No semantic layer configured.")

        cube = manifest.cube(cube_name)
        if cube is None:
            raise HTTPException(status_code=404, detail="Not found")

        measures = [m for m in (payload.get("measures") or []) if cube.measure(m)]
        dimensions = [d for d in (payload.get("dimensions") or []) if cube.dimension(d)]
        time_dimension = payload.get("time_dimension") or None
        granularity = payload.get("granularity") or None
        limit = min(int(payload.get("limit") or 200), 1000)

        if not measures:
            raise HTTPException(status_code=400, detail="Select at least one measure.")
        if time_dimension and not cube.dimension(time_dimension):
            raise HTTPException(status_code=400, detail="Unknown time dimension.")
        if time_dimension and not granularity:
            raise HTTPException(
                status_code=400, detail="A time dimension needs a granularity."
            )

        from vanna.core.errors import VannaError
        from vanna.semantic.compiler import truncate

        selected: List[str] = []
        grouping: List[str] = []

        if time_dimension:
            bucket = truncate(
                cube.dimension(time_dimension).expression, granularity, runtime.dialect
            )
            selected.append(f"{bucket} AS {time_dimension}")
            grouping.append(bucket)

        for name in dimensions:
            expression = cube.dimension(name).expression
            selected.append(f"{expression} AS {name}")
            grouping.append(expression)

        for name in measures:
            selected.append(f"{cube.measure(name).expression} AS {name}")

        statement = f"SELECT {', '.join(selected)} FROM {cube.base_object}"
        if grouping:
            statement += " GROUP BY " + ", ".join(grouping)
            statement += " ORDER BY " + ", ".join(grouping)

        # Executed through the registry, so the caller's row and column rules
        # apply to a cube exactly as they do to a typed query.
        import uuid as _uuid

        from vanna.core.tool import ToolCall, ToolContext

        ctx = ToolContext(
            user=user,
            conversation_id="cube",
            request_id=str(_uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=agent_memory,
        )

        try:
            result = await runtime.agent.tool_registry.execute(
                ToolCall(
                    id=str(_uuid.uuid4()),
                    name="run_sql",
                    arguments={"sql": f"{statement} LIMIT {limit}"},
                ),
                ctx,
            )
        except VannaError as exc:
            raise HTTPException(status_code=400, detail=exc.to_dict())

        if not result.success:
            raise HTTPException(status_code=400, detail=result.error or "Query failed.")

        meta = result.metadata or {}
        rows = meta.get("results") or []
        return {
            "columns": list(meta.get("columns") or []),
            "rows": [
                list(row.values()) if isinstance(row, dict) else list(row)
                for row in rows
            ],
            "row_count": int(meta.get("row_count") or len(rows)),
            "warnings": list((ctx.metadata or {}).get("semantic_warnings") or []),
        }

    # ------------------------------------------------------------------
    # Dashboards
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/dashboards")
    async def list_dashboards(request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        return {"dashboards": await _require_directory().list_dashboards(user.tenant_id)}

    @app.get("/api/vanna/v2/dashboards/{dashboard_id}")
    async def get_dashboard(dashboard_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        row = await _require_directory().get_dashboard(user.tenant_id, dashboard_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return {"dashboard": row["document"]}

    @app.post("/api/vanna/v2/dashboards")
    async def save_dashboard(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """Create or replace a dashboard.

        Verified before it is stored *and* again before it is rendered. Storing
        a document known to be broken means a reader, not the author,
        discovers it -- against something that looks saved and fine.
        """
        user = await _caller(request)
        _forbid_viewer(user)

        from vanna.dashboards import Dashboard, has_errors, verify_dashboard

        try:
            dashboard = Dashboard.model_validate({**payload, "tenant_id": user.tenant_id})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Malformed dashboard: {exc}")

        issues = verify_dashboard(dashboard)
        if has_errors(issues):
            raise HTTPException(
                status_code=400,
                detail=[str(i) for i in issues if i.severity == "error"],
            )

        saved = await _require_directory().save_dashboard(
            user.tenant_id,
            dashboard.to_json_dict(),
            created_by=user.email or user.id,
        )
        return {
            "dashboard": saved["document"],
            "warnings": [str(i) for i in issues if i.severity == "warning"],
        }

    @app.delete("/api/vanna/v2/dashboards/{dashboard_id}")
    async def delete_dashboard(dashboard_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _forbid_viewer(user)
        if not await _require_directory().delete_dashboard(user.tenant_id, dashboard_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    @app.get("/api/vanna/v2/dashboards/{dashboard_id}/data")
    async def dashboard_data(dashboard_id: str, request: Request) -> Dict[str, Any]:
        """Execute every tile as the caller.

        Each tile runs through the tenant's tool registry, so the SQL policy,
        semantic compilation and the caller's own row and column rules apply --
        two people opening the same dashboard can and should see different
        numbers.
        """
        user = await _caller(request)
        directory_ = _require_directory()

        row = await directory_.get_dashboard(user.tenant_id, dashboard_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")

        from vanna.dashboards import Dashboard, has_errors, render_dashboard, verify_dashboard

        try:
            dashboard = Dashboard.model_validate(row["document"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Stored dashboard is invalid: {exc}")

        # Re-verified on the way out: this document may have been stored by an
        # older build with a weaker check.
        issues = verify_dashboard(dashboard)
        if has_errors(issues):
            raise HTTPException(
                status_code=400,
                detail=[str(i) for i in issues if i.severity == "error"],
            )

        saved_sql = {
            item["id"]: item["sql"] for item in await directory_.list_saved(user.tenant_id)
        }

        runtime = await runtime_for(user.tenant_id)
        results = await render_dashboard(
            dashboard,
            registry=runtime.agent.tool_registry,
            user=user,
            agent_memory=agent_memory,
            saved_query_sql=saved_sql,
        )
        return {"results": [r.model_dump(mode="json") for r in results]}

    # ------------------------------------------------------------------
    # Ad-hoc SQL
    # ------------------------------------------------------------------

    @app.post("/api/vanna/v2/run-sql")
    async def run_sql(payload: RunSqlPayload, request: Request) -> Dict[str, Any]:
        """Run a saved or edited statement, through the tool registry.

        Not straight to the driver, and not through a hand-rolled policy check
        either. Everything that decides what a caller may run lives in
        ``ToolRegistry.transform_args``: the SQL policy, the per-user policy
        (which is what permits an admin a write), semantic compilation, and the
        row and column rules.

        A previous version of this endpoint validated against the tenant's
        static policy and then called the runner itself. That was a hole: the
        "Run SQL" button applied neither row-level rules nor the caller's own
        policy, so two users with different permissions got the same rows. The
        registry is the only path that cannot drift from the agent's.
        """
        user = await _caller(request)
        runtime = await runtime_for(user.tenant_id)

        sql = payload.sql.strip().rstrip(";")
        if not sql:
            raise HTTPException(status_code=400, detail="No SQL provided")

        from vanna.core.errors import ErrorPhase, VannaError
        from vanna.core.tool import ToolCall, ToolContext

        ctx = ToolContext(
            user=user,
            conversation_id="run-sql",
            request_id=str(uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=agent_memory,
        )

        try:
            result = await runtime.agent.tool_registry.execute(
                ToolCall(
                    id=str(uuid.uuid4()),
                    name="run_sql",
                    arguments={"sql": f"{sql} LIMIT {payload.limit}"
                               if not _has_limit(sql) else sql},
                ),
                ctx,
            )
        except VannaError as exc:
            raise HTTPException(status_code=400, detail=exc.to_dict())
        except Exception as exc:
            error = VannaError.from_exception(
                exc, phase=ErrorPhase.SQL_EXECUTION, metadata={"sql": sql}
            )
            logger.warning("run-sql failed for %s: %s", user.tenant_id, error)
            raise HTTPException(status_code=400, detail=error.to_dict())

        if not result.success:
            # A rejection from the policy or the compiler arrives here rather
            # than as an exception, and is the caller's to fix.
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "policy_violation",
                    "phase": "sql_policy_check",
                    "message": result.error or result.result_for_llm or "Rejected.",
                },
            )

        meta = result.metadata or {}
        rows = meta.get("results") or []

        return {
            "columns": [str(c) for c in (meta.get("columns") or [])],
            "rows": [
                list(row.values()) if isinstance(row, dict) else list(row)
                for row in rows
            ],
            "row_count": int(meta.get("row_count") or meta.get("rows_affected") or len(rows)),
            "rows_affected": meta.get("rows_affected"),
            "truncated": bool(meta.get("truncated")),
            "warnings": list((ctx.metadata or {}).get("semantic_warnings") or []),
        }

    def _has_limit(sql: str) -> bool:
        """Whether the statement already caps its own rows.

        Appending a second LIMIT would be a syntax error, and appending one to
        a write statement would be nonsense.
        """
        from vanna.core.sql_policy import has_row_limit

        try:
            if not sql.lstrip()[:6].lower().startswith(("select", "with")):
                return True  # not a row-returning statement; nothing to cap
            return has_row_limit(sql)
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Tenant administration
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants")
    async def admin_list_tenants(request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_platform_admin(user)
        directory_ = _require_directory()
        tenants = await directory_.list_tenants(active_only=False)
        for tenant in tenants:
            tenant["usage"] = await directory_.tenant_usage(tenant["id"])
        return {"tenants": tenants}

    @app.post("/api/vanna/v2/admin/tenants")
    async def admin_create_tenant(payload: TenantPayload, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_platform_admin(user)
        directory_ = _require_directory()

        if await directory_.get_tenant(payload.id):
            raise HTTPException(status_code=409, detail="That tenant id already exists")

        database_url = payload.database_url or _compose_url(
            payload.model_dump(
                include={"host", "port", "database", "username", "password", "sslmode"}
            )
        )

        try:
            row = await directory_.create_tenant(
                payload.id,
                payload.name,
                description=payload.description,
                database_url=database_url or None,
                daily_quota=payload.daily_quota,
                max_rows=payload.max_rows,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # The creator becomes the first admin. A tenant nobody can administer is
        # a support ticket waiting to happen.
        await directory_.add_user(payload.id, user.email or user.id, role="admin")
        return {"tenant": {"id": row["id"], "name": row["name"]}}

    @app.patch("/api/vanna/v2/admin/tenants/{tenant_id}")
    async def admin_update_tenant(
        tenant_id: str, payload: TenantUpdate, request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        directory_ = _require_directory()

        changes = payload.model_dump(exclude_unset=True)

        # Structured fields win over an empty database_url: the form sends both,
        # and composing here keeps percent-encoding in one place. A password
        # containing '@' otherwise produces a URL pointing at the wrong host.
        connection_fields = {"host", "port", "database", "username", "password", "sslmode"}
        supplied = {k: changes.pop(k) for k in list(changes) if k in connection_fields}
        if supplied.get("host") and supplied.get("database") and not changes.get("database_url"):
            changes["database_url"] = _compose_url(supplied)

        # Repointing a tenant at another database changes what every member can
        # read. That is a platform decision, not a tenant-admin one.
        # Both of these change what the workspace is permitted to do, not just
        # how it looks, so they are platform decisions rather than tenant-admin
        # ones -- an admin must not be able to grant their own workspace writes.
        if "database_url" in changes or "allow_writes" in changes:
            _require_platform_admin(user)
        else:
            _require_tenant_admin(user, tenant_id)

        row = await directory_.update_tenant(tenant_id, changes)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        if "database_url" in changes:
            # Drop the cached runtime so the next question uses the new
            # connection instead of the one built at first use.
            await runtime_for(tenant_id, invalidate=True)
        return {"tenant": {"id": row["id"], "name": row["name"]}}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}")
    async def admin_delete_tenant(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_platform_admin(user)
        if tenant_id == default_tenant:
            raise HTTPException(status_code=400, detail="The default tenant cannot be deleted")
        if not await _require_directory().delete_tenant(tenant_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/users")
    async def admin_list_users(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        return {"users": await _require_directory().list_users(tenant_id)}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/users")
    async def admin_add_user(
        tenant_id: str, payload: UserPayload, request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        directory_ = _require_directory()
        if not await directory_.get_tenant(tenant_id):
            raise HTTPException(status_code=404, detail="Not found")
        try:
            member = await directory_.add_user(
                tenant_id, payload.email, full_name=payload.full_name, role=payload.role
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"user": {"id": str(member["id"]), "email": member["email"], "role": member["role"]}}

    @app.patch("/api/vanna/v2/admin/tenants/{tenant_id}/users/{user_id}")
    async def admin_update_user(
        tenant_id: str, user_id: str, payload: UserUpdate, request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        directory_ = _require_directory()

        changes = payload.model_dump(exclude_unset=True)
        # Demoting or disabling the last admin locks the tenant out of its own
        # settings, and only a platform admin could then fix it. Refuse.
        if changes.get("role") not in (None, "admin") or changes.get("is_active") is False:
            await _guard_last_admin(directory_, tenant_id, user_id)

        try:
            member = await directory_.update_user(tenant_id, user_id, changes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if member is None:
            raise HTTPException(status_code=404, detail="Not found")
        return {"user": {"id": str(member["id"]), "email": member["email"], "role": member["role"]}}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/users/{user_id}")
    async def admin_remove_user(
        tenant_id: str, user_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        directory_ = _require_directory()
        await _guard_last_admin(directory_, tenant_id, user_id)
        if not await directory_.remove_user(tenant_id, user_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    async def _guard_last_admin(directory_: Directory, tenant_id: str, user_id: str) -> None:
        """Refuse a change that would leave the tenant with no active admin."""
        members = await directory_.list_users(tenant_id)
        target = next((m for m in members if str(m["id"]) == str(user_id)), None)
        if target is None or target["role"] != "admin" or not target["is_active"]:
            return
        if await directory_.count_admins(tenant_id) <= 1:
            raise HTTPException(
                status_code=400,
                detail="This is the tenant's last admin. Promote another member first.",
            )

    # -- starters ------------------------------------------------------

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/starters")
    async def admin_add_starter(
        tenant_id: str, payload: StarterPayload, request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="A question is required")
        await _require_directory().add_starter(tenant_id, question, payload.sort_order)
        return {"starters": await _require_directory().list_starters(tenant_id)}

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/starters")
    async def admin_list_starters(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        return {"starters": await _require_directory().list_starters(tenant_id)}

    @app.delete("/api/vanna/v2/admin/tenants/{tenant_id}/starters/{starter_id}")
    async def admin_delete_starter(
        tenant_id: str, starter_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        if not await _require_directory().delete_starter(tenant_id, starter_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    # -- usage ---------------------------------------------------------

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/usage")
    async def admin_tenant_usage(
        tenant_id: str, request: Request, days: int = 30
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        return await _require_directory().tenant_usage(tenant_id, days=days)

    # -- billing -------------------------------------------------------
    #
    # Tenant-admin, not platform-admin: a workspace's own admin should be able to
    # see what their workspace is on and what it has been charged. Changing the
    # plan is the same permission because in this deployment the person recording
    # a payment is the person who took it.

    def _require_billing():
        if billing is None:
            raise HTTPException(
                status_code=503, detail="Billing needs a control-plane database."
            )
        return billing

    @app.get("/api/vanna/v2/admin/tenants/{tenant_id}/billing")
    async def admin_billing(tenant_id: str, request: Request) -> Dict[str, Any]:
        """Plan, limits in force, usage against them, and payment history."""
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        store = _require_billing()

        from vanna.core.billing import PLANS, resolve_limits

        tenant = await _require_directory().get_tenant(tenant_id)
        subscription = await store.get_subscription(tenant_id)
        limits = resolve_limits(
            tenant,
            subscription,
            default_quota=int(os.getenv("VANNA_DAILY_QUOTA", "200")),
            default_max_rows=int(os.getenv("VANNA_MAX_ROWS", "1000")),
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
            "usage": await _require_directory().tenant_usage(tenant_id, days=30),
            "payments": await store.list_payments(tenant_id),
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
        tenant_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        """Put a workspace on a plan.

        The runtime is rebuilt afterwards because the row cap is baked into the
        SQL runner and the policy's default LIMIT when they are constructed --
        without this the new plan would only take effect at the next restart.
        """
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        store = _require_billing()

        try:
            subscription = await store.set_subscription(
                tenant_id,
                str(payload.get("plan") or "free"),
                months=int(payload.get("months") or 1),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        await runtime_for(tenant_id, invalidate=True)
        logger.info(
            "Plan for %s set to %s by %s",
            tenant_id,
            subscription.get("plan"),
            user.email,
        )
        return {"subscription": subscription}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/billing/cancel")
    async def admin_cancel_plan(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        cancelled = await _require_billing().cancel_subscription(tenant_id)
        await runtime_for(tenant_id, invalidate=True)
        logger.info("Subscription for %s cancelled by %s", tenant_id, user.email)
        return {"cancelled": cancelled}

    @app.post("/api/vanna/v2/admin/tenants/{tenant_id}/billing/payments")
    async def admin_record_payment(
        tenant_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        """Record a payment taken elsewhere, and extend the subscription.

        Idempotent on the reference. Recording the same reference twice returns
        ``recorded: false`` and does *not* extend again -- which is the whole
        point of the unique constraint, and the behaviour a retried webhook or a
        double-clicked button depends on.
        """
        user = await _caller(request)
        _require_tenant_admin(user, tenant_id)
        store = _require_billing()

        from billing import build_provider

        provider = build_provider(os.getenv("VANNA_PAYMENT_PROVIDER", "manual"))
        months = int(payload.get("months") or 1)

        try:
            charge = await provider.charge(
                tenant_id,
                str(payload.get("plan") or "free"),
                months,
                reference=payload.get("reference"),
                amount_cents=payload.get("amount_cents"),
                currency=payload.get("currency") or "usd",
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
            description=str(payload.get("description") or ""),
        )

        # Only a payment that was actually new moves the expiry. A replay must
        # not buy another month.
        subscription = None
        if recorded:
            plan = str(payload.get("plan") or "").strip().lower()
            if plan:
                try:
                    await store.set_subscription(tenant_id, plan, months=months)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            else:
                await store.extend(tenant_id, months)
            subscription = await store.get_subscription(tenant_id)
            await runtime_for(tenant_id, invalidate=True)
            logger.info(
                "Payment recorded for %s (%s) by %s",
                tenant_id,
                charge["provider_ref"],
                user.email,
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

    @app.post("/api/vanna/v2/admin/datasources/test")
    async def test_datasource(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """Try a connection before anything is stored.

        SQL Chat gets this right and it is worth copying: a tenant bound to an
        unreachable database is indistinguishable from a broken deployment, and
        the person who can tell them apart is the one filling in this form.

        Accepts either a whole URL or the structured fields, and returns a
        sanitised error -- driver messages routinely echo the DSN, password
        included.
        """
        user = await _caller(request)
        _require_platform_admin(user)

        url = str(payload.get("database_url") or "").strip()
        if not url:
            url = _compose_url(payload)
        if not url:
            raise HTTPException(status_code=400, detail="A host and database are required")

        from vanna.capabilities.sql_runner import ExecutionPolicy, RunSqlToolArgs
        from vanna.core.errors import ErrorPhase, VannaError

        try:
            from vanna.integrations.postgres import PostgresRunner

            runner = PostgresRunner(
                connection_string=url,
                policy=ExecutionPolicy(max_rows=1, timeout_seconds=8),
                read_only=True,
            )
            ctx = await _tool_context(user)
            await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), ctx)
        except Exception as exc:
            error = VannaError.from_exception(exc, phase=ErrorPhase.PROFILE_RESOLUTION)
            logger.info("Datasource test failed for %s: %s", user.email, error)
            return {
                "ok": False,
                # The message is sanitised; the URL is never echoed back.
                "error": error.args[0] if error.args else "Could not connect.",
            }

        return {"ok": True, "data_source": describe_data_source(url)}

    def _compose_url(payload: Dict[str, Any]) -> str:
        """Build a connection URL from form fields.

        Assembled server-side so the browser never has to hold a credential
        long enough to concatenate one, and so percent-encoding is done once,
        correctly -- a password with an `@` in it silently produces a URL
        pointing at the wrong host otherwise.
        """
        from urllib.parse import quote

        host = str(payload.get("host") or "").strip()
        database = str(payload.get("database") or "").strip()
        if not host or not database:
            return ""

        user_name = quote(str(payload.get("username") or ""), safe="")
        password = quote(str(payload.get("password") or ""), safe="")
        port = str(payload.get("port") or "5432").strip()

        credentials = f"{user_name}:{password}@" if user_name else ""
        url = f"postgresql://{credentials}{host}:{port}/{database}"

        sslmode = str(payload.get("sslmode") or "").strip()
        if sslmode:
            url += f"?sslmode={quote(sslmode, safe='')}"
        return url

    @app.get("/api/vanna/v2/admin/datasources")
    async def admin_datasources(request: Request) -> Dict[str, Any]:
        """Databases on the configured server, as binding suggestions.

        Saves an operator from hand-typing a connection string for a database
        that already exists next to the one we are connected to. Platform admin
        only -- it enumerates the server.
        """
        user = await _caller(request)
        _require_platform_admin(user)

        base = os.getenv("VANNA_DATABASE_URL", "")
        if not base.startswith("postgres"):
            return {"datasources": [], "note": "Only PostgreSQL servers are enumerated."}

        from urllib.parse import urlsplit, urlunsplit

        import psycopg2

        parts = urlsplit(base)
        suggestions: List[Dict[str, str]] = []
        try:
            conn = psycopg2.connect(base, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT datname FROM pg_database "
                        "WHERE NOT datistemplate AND datallowconn ORDER BY datname"
                    )
                    for (name,) in cur.fetchall():
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
                conn.close()
        except Exception as exc:
            logger.warning("Could not enumerate databases: %s", exc)
            return {"datasources": [], "note": str(exc)[:200]}

        return {"datasources": suggestions}
