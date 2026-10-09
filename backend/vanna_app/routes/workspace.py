"""Identity, the workspace a caller is in, and the schema it exposes.

The read surface a signed-in user sees before they ask anything: who am I, what
workspace is this, what is in it, and what is the model actually being told.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from ..authz import is_platform_admin, require_tenant_admin, role_of
from ..tenancy import describe_data_source
from . import Deps

logger = logging.getLogger("vanna.routes.workspace")


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings

    # ------------------------------------------------------------------
    # Sign-in surface
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/tenants")
    async def list_tenants() -> Dict[str, Any]:
        """Workspaces available to sign in to.

        Unauthenticated by necessity -- it is what the sign-in screen reads before
        anyone has identified themselves. Names only, and in a deployment that has
        not opted into a public directory it is the *only* thing published.
        """
        if deps.directory is None:
            return {
                "tenants": [{"id": settings.default_tenant, "name": settings.default_tenant.title()}],
                "control_plane": False,
            }
        tenants = await deps.directory.list_tenants()
        return {
            "tenants": [{"id": t["id"], "name": t["name"]} for t in tenants],
            "control_plane": True,
        }

    @app.get("/api/vanna/v2/datasources")
    async def list_datasources(request: Request) -> Dict[str, Any]:
        """The databases this caller's workspace can be asked about.

        What the database picker in the chat reads. Credential-free by
        construction: ``data_source_id`` is the label ``describe_data_source``
        derives from the URL with the password stripped, and the connection string
        itself is never returned by this route.

        A workspace that has registered nothing reports the single database it has
        always had, so the picker looks the same either way and a client needs no
        special case for the pre-registry world.
        """
        user = await deps.caller(request)

        registry = getattr(deps.platform, "datasources", None)
        sources = []
        if registry is not None:
            try:
                sources = await registry.list_sources(user.tenant_id)
            except Exception as exc:
                logger.warning("Could not list databases for %s: %s", user.tenant_id, exc)
                sources = []

        if not sources:
            runtime = await deps.runtime_for_request(user, request)
            sources = [
                {
                    "data_source_id": runtime.data_source,
                    "label": runtime.data_source,
                    "is_default": True,
                }
            ]

        return {"data_sources": sources}

    @app.get("/api/vanna/v2/tenants/{tenant_id}/users")
    async def list_tenant_roster(tenant_id: str) -> Dict[str, Any]:
        """Member roster for the sign-in picker. Off unless explicitly enabled.

        Publishing who belongs to a workspace before anybody has authenticated is a
        disclosure; only a demo wants it, and ``config.validate`` refuses it in
        multi-tenant mode.
        """
        if not settings.public_user_directory:
            raise HTTPException(status_code=404, detail="Not found")
        users = await deps.require_directory().list_users(tenant_id)
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
        "those credentials do not work here" -- so this is where a user who is not a
        member of the workspace they picked finds out.
        """
        user = await deps.caller(request)
        groups = list(getattr(user, "group_memberships", None) or [])

        tenant: Dict[str, Any] = {"id": user.tenant_id, "name": user.tenant_id}
        role = role_of(user)
        memberships: List[str] = []

        if deps.directory is not None:
            row = await deps.directory.get_tenant(user.tenant_id)
            if row:
                tenant = {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "data_source": describe_data_source(row["database_url"]),
                    # So the account screen can hide the personal-key form when the
                    # workspace forbids it. The server enforces this independently --
                    # hiding a form is a courtesy, not a control.
                    "allow_byo_key": row.get("allow_byo_key", True),
                }
            member = await deps.directory.get_member(user.tenant_id, user.email or user.id)
            if member:
                role = member["role"]
                await deps.directory.touch_last_seen(user.tenant_id, member["email"])
            memberships = await deps.directory.tenants_for_email(user.email or user.id)

        return {
            "user": {
                "id": user.id,
                "email": user.email,
                "name": getattr(user, "username", None) or "",
                "role": role,
            },
            "tenant": tenant,
            "memberships": memberships,
            "is_platform_admin": is_platform_admin(user, settings),
            "is_admin": "admin" in groups,
            "control_plane": deps.directory is not None,
            "deployment_mode": settings.mode,
        }

    @app.get("/api/vanna/v2/starters")
    async def starters(request: Request) -> Dict[str, Any]:
        """Suggested questions for the caller's workspace."""
        user = await deps.caller(request)
        if deps.directory is None:
            return {"starters": []}
        return {"starters": await deps.directory.list_starters(user.tenant_id)}

    # ------------------------------------------------------------------
    # Schema explorer
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/schema")
    async def schema(request: Request, layer: str = "active") -> Dict[str, Any]:
        """The catalog for the caller's workspace.

        Reads the catalog, never the database. The catalog is what the model is
        actually shown, so a user browsing it sees the same picture the agent has --
        including where a scan found nothing, which is the usual explanation for a
        bad answer.
        """
        user = await deps.caller(request)
        runtime = await deps.runtime_for_request(user, request)
        context = await deps.tool_context(user)

        catalog = runtime.catalog
        is_semantic = type(catalog).__name__ == "SemanticSchemaCatalog"
        if layer == "physical" and is_semantic:
            catalog = getattr(catalog, "physical", None) or catalog

        # Scoped to the database this request is about. Omitting it returns every
        # table the *tenant* has across all of its databases, so the schema screen
        # reported `world` in its header and listed chinook's tables underneath.
        tables = await catalog.get_tables(context, data_source_id=runtime.data_source)
        relationships = await catalog.get_relationships(
            context, data_source_id=runtime.data_source
        )

        return {
            "dialect": runtime.dialect,
            "data_source": runtime.data_source,
            "semantic": is_semantic,
            # The backend really serving retrieval. Reported because a
            # configured-but-unavailable vector index silently degrades to keyword
            # search, and that is invisible from the outside.
            "index_backend": getattr(getattr(catalog, "index", None), "name", "none"),
            "layer": (
                "physical" if (layer == "physical" and is_semantic)
                else ("semantic" if is_semantic else "physical")
            ),
            "tables": [
                {
                    "name": t.table_name,
                    "schema": t.schema_name,
                    "description": t.description,
                    "row_count_estimate": t.row_count_estimate,
                    "last_synced_at": t.last_synced_at.isoformat() if t.last_synced_at else None,
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

    @app.post("/api/vanna/v2/schema/rescan")
    async def rescan(request: Request) -> Dict[str, Any]:
        """Re-scan the workspace's database into its catalog. Tenant admin only."""
        user = await deps.caller(request)
        require_tenant_admin(user, user.tenant_id, settings)

        from vanna.capabilities.schema_catalog import SchemaScanner

        runtime = await deps.runtime_for_request(user, request)
        # Naming the source matters: without it the scan files every table under
        # "default" and the annotation routes, which resolve the real id, cannot
        # find any of them.
        context = await deps.tool_context(user, data_source=runtime.data_source)
        try:
            # `data_source_id` as well as the context: the scanner stamps every
            # table and relationship with it, defaulting to "default", and the
            # store honours the record's own value over the context's. Without
            # it every rescan filed the whole database under "default" -- rows
            # no reader looks up -- and the catalog the agent reads never changed.
            # `Platform._prepare_tenant` has always passed it; this route did not.
            report = await SchemaScanner(runtime.runner, dialect=runtime.dialect).scan(
                context, runtime.catalog, data_source_id=runtime.data_source
            )
        except Exception as exc:
            logger.error("Rescan failed for %s: %s", user.tenant_id, exc)
            raise HTTPException(status_code=502, detail=f"Scan failed: {exc}")

        await deps.admin_audit.record(
            "knowledge.rescan",
            actor_email=user.email,
            tenant_id=user.tenant_id,
            details={"tables": report.tables_scanned},
            actor_ip=deps.client_ip(request),
        )
        return {
            "tables_scanned": report.tables_scanned,
            "columns_profiled": report.columns_profiled,
            "relationships_found": report.relationships_found,
            "relationships_inferred": report.relationships_inferred,
            "duration_ms": report.duration_ms,
            "errors": report.errors,
        }

    # ------------------------------------------------------------------
    # Semantic layer
    # ------------------------------------------------------------------

    def _manifest_of(runtime: Any) -> Any:
        registry = getattr(runtime.agent, "tool_registry", None)
        return getattr(registry, "manifest", None)

    @app.get("/api/vanna/v2/semantic/manifest")
    async def semantic_manifest(request: Request) -> Dict[str, Any]:
        """The manifest this workspace's agent is using.

        Read-only. A manifest is authored in git and compiled by
        ``vanna project build``; editing it over HTTP would put the definition of
        record somewhere nobody reviews.
        """
        user = await deps.caller(request)
        runtime = await deps.runtime_for_request(user, request)
        manifest = _manifest_of(runtime)

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
    async def semantic_compile(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """Show what a semantic statement compiles to, without running it.

        Admin-only because the output is the physical schema -- table names, join
        conditions, and any row predicate that was injected.
        """
        user = await deps.caller(request)
        require_tenant_admin(user, user.tenant_id, settings)

        statement = str(payload.get("sql") or "").strip()
        if not statement:
            raise HTTPException(status_code=400, detail="sql is required")

        runtime = await deps.runtime_for_request(user, request)
        compile_for = getattr(
            getattr(runtime.agent, "tool_registry", None), "compile_for", None
        )
        if compile_for is None:
            raise HTTPException(
                status_code=503, detail="This workspace is not using a semantic layer."
            )

        from vanna.core.errors import VannaError

        context = await deps.tool_context(user)
        try:
            compiled = compile_for(statement, user, context)
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
            "warnings": [{"code": w.code, "message": w.message} for w in compiled.warnings],
        }

    @app.post("/api/vanna/v2/admin/access/preview")
    async def access_preview(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """The SQL a *given* user's question would actually run.

        The most useful thing in the access-control feature. Without it, an operator
        confirms a rule works by noticing that somebody sees fewer rows, which is
        indistinguishable from the rule being broken in the other direction. This
        shows the predicate.
        """
        caller = await deps.caller(request)
        require_tenant_admin(caller, caller.tenant_id, settings)

        target_email = str(payload.get("email") or "").strip().lower()
        statement = str(payload.get("sql") or "").strip()
        if not target_email or not statement:
            raise HTTPException(status_code=400, detail="email and sql are required")

        directory = deps.require_directory()
        member = await directory.get_member(caller.tenant_id, target_email)
        if member is None:
            raise HTTPException(
                status_code=404,
                detail=f"{target_email} is not a member of this workspace.",
            )

        from vanna.core.errors import VannaError
        from vanna.core.tool import ToolContext
        from vanna.core.user import User

        subject = User(
            id=member["email"],
            email=member["email"],
            tenant_id=caller.tenant_id,
            group_memberships=["user"] + (["admin"] if member["role"] == "admin" else []),
            metadata={"role": member["role"]},
        )

        runtime = await deps.runtime_for(caller.tenant_id)
        compile_for = getattr(
            getattr(runtime.agent, "tool_registry", None), "compile_for", None
        )
        if compile_for is None:
            raise HTTPException(
                status_code=503, detail="This workspace is not using a semantic layer."
            )

        import uuid

        context = ToolContext(
            user=subject,
            conversation_id="access-preview",
            request_id=str(uuid.uuid4()),
            tenant_id=caller.tenant_id,
            agent_memory=deps.agent_memory,
        )

        try:
            compiled = compile_for(statement, subject, context)
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

    # ------------------------------------------------------------------
    # Usage and prompt inspection
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/usage")
    async def usage(request: Request) -> Dict[str, Any]:
        """This workspace's question count against its quota.

        Read from the same counters the limit is enforced with, so the number shown
        and the number enforced cannot disagree -- which they did when the hook was
        per-process and this endpoint counted rows.
        """
        user = await deps.caller(request)
        if deps.directory is None:
            return {"enabled": False}

        from vanna.core.billing import resolve_limits

        tenant = await deps.directory.get_tenant(user.tenant_id)
        subscription = (
            await deps.billing.get_subscription(user.tenant_id) if deps.billing else None
        )
        limits = resolve_limits(
            tenant,
            subscription,
            default_quota=settings.daily_quota,
            default_max_rows=settings.max_rows,
        )

        used = 0
        if deps.counters is not None:
            used = await deps.counters.peek(f"quota:tenant:{user.tenant_id}", 86_400)
        elif deps.generation_store is not None:
            row = await deps.directory.db.fetch_one(
                "SELECT count(*) AS used FROM vanna_app.generations "
                "WHERE tenant_id = %s AND created_at > now() - interval '1 day'",
                (user.tenant_id,),
            )
            used = int((row or {}).get("used") or 0)

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
            # the user's side is indistinguishable from a bug.
            "expired": bool(
                subscription
                and limits.plan.name == "free"
                and (subscription.get("plan") or "free") != "free"
            ),
            # "Why am I capped at 200?" answered without anyone reading code.
            "limit_source": limits.quota_source,
        }

    @app.get("/api/vanna/v2/prompt-preview")
    async def prompt_preview(request: Request, question: str = "") -> Dict[str, Any]:
        """Exactly what the model would be told for this question.

        Not a reconstruction -- this calls the same
        ``RetrievalContextEnhancer.build_context`` the agent uses, so what is shown
        is what would be sent, including which sections the token budget dropped.
        That last part is usually the interesting answer.

        Admin-only. The assembled context is the entire schema plus every business
        rule the workspace has.
        """
        user = await deps.caller(request)
        require_tenant_admin(user, user.tenant_id, settings)

        if not question.strip():
            raise HTTPException(status_code=400, detail="A question is required")

        runtime = await deps.runtime_for_request(user, request)
        enhancer = getattr(runtime.agent, "llm_context_enhancer", None)
        if enhancer is None or not hasattr(enhancer, "build_context"):
            raise HTTPException(
                status_code=503, detail="This workspace does not use the retrieval enhancer."
            )

        result = await enhancer.build_context(question, user)
        budget = getattr(enhancer.budget, "total_tokens", 0)
        if result is None:
            return {
                "question": question,
                "sections": [],
                "tokens_used": 0,
                "budget": budget,
                "note": "Nothing was retrieved for this question.",
            }

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
            # How the schema section was chosen: full or search, which tables
            # the join tree added as bridges, which a glossary term or metric
            # pulled in, and the core columns shown. "Why did it not see table
            # X" is answered here.
            "schema": (getattr(result, "metadata", None) or {}).get("schema"),
        }
