"""Feedback and knowledge-administration routes.

Two endpoints groups, serving the two halves of the improvement loop:

**Feedback** (``/api/vanna/v2/feedback``) is called by ``<vanna-chat>`` when a
user rates an answer. A positive rating on a turn that produced SQL writes a
*candidate* example. This is the only thing that turns ordinary usage into
training data, and until it exists the example store fills with entries nothing
ever confirms.

**Admin** (``/api/vanna/v2/admin/*``) backs the review console: list candidates,
promote the good ones to verified, reject the bad ones, and manage instructions.

Every route is tenant-scoped through the same ``UserResolver`` the chat routes
use, and the admin routes additionally require group membership. Note the
deliberate asymmetry: a candidate is written automatically, but **nothing
becomes verified without a human**. "The query ran" is not "the answer was
right", and an auto-promotion pipeline would launder the difference -- one
plausible-looking wrong answer becomes a few-shot example, which teaches the
model to produce more like it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from vanna.capabilities.knowledge import (
    ExampleStatus,
    is_worth_saving,
    ExampleStore,
    Instruction,
    InstructionScope,
    InstructionStore,
)
from vanna.core.generation import Feedback, GenerationStore, recent_window
from vanna.core.tool import ToolContext
from vanna.core.user import RequestContext, User, UserResolver

logger = logging.getLogger(__name__)

#: Groups permitted to review and promote knowledge. Promotion decides what the
#: model is shown as authoritative, so it is a privileged action.
DEFAULT_ADMIN_GROUPS = ("admin",)


class FeedbackPayload(BaseModel):
    """A user's verdict on one answer."""

    conversation_id: str
    request_id: str
    rating: str = Field(description="'positive' or 'negative'")
    comment: Optional[str] = None
    sql: Optional[str] = None
    question: Optional[str] = None


class InstructionPayload(BaseModel):
    """A new business rule."""

    text: str
    scope: str = "global"
    scope_ref: Optional[str] = None
    priority: int = 0


def register_admin_routes(
    app: Any,
    *,
    user_resolver: UserResolver,
    example_store: Optional[ExampleStore] = None,
    instruction_store: Optional[InstructionStore] = None,
    agent_memory: Any = None,
    generation_store: Optional[GenerationStore] = None,
    admin_groups: tuple = DEFAULT_ADMIN_GROUPS,
    dialect: Optional[str] = None,
) -> None:
    """Register feedback and admin routes.

    Args:
        app: FastAPI application.
        user_resolver: Resolves identity and tenant from the request. The same
            resolver as the chat routes, so scoping is consistent.
        example_store: Where rated answers land and reviewers promote from.
        instruction_store: Business-rule storage.
        agent_memory: Needed only to construct a ``ToolContext``.
        generation_store: Records the rating against the turn that earned
            it, so quality can be measured rather than guessed at.
        admin_groups: Groups allowed to reach the admin routes.
        dialect: SQL dialect, for validating examples on write.
    """
    # NOTE: `Request` and `HTTPException` are imported at module scope, not
    # here. This module uses `from __future__ import annotations`, so every
    # annotation is a string that FastAPI resolves at request-binding time via
    # `get_type_hints` against the *module* globals. A function-local import is
    # invisible there, so FastAPI cannot tell `request: Request` is the ASGI
    # request and silently binds it as a required query parameter -- every
    # route then 422s. Routes register fine either way; the failure only
    # appears on a real request.

    async def _context(request: Request) -> tuple:
        """Resolve the caller and build a tenant-scoped ToolContext."""
        import uuid

        req_ctx = RequestContext(
            headers=dict(request.headers),
            cookies=dict(request.cookies),
            metadata={},
        )
        user = await user_resolver.resolve_user(req_ctx)
        ctx = ToolContext(
            user=user,
            conversation_id="admin",
            request_id=str(uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=agent_memory,
        )
        return user, ctx

    def _require_admin(user: User) -> None:
        if not admin_groups:
            return
        if not (set(user.group_memberships) & set(admin_groups)):
            # 404, not 403: a 403 confirms the endpoint exists and that the
            # caller simply lacks the role, which is a small information leak
            # about the deployment's shape.
            raise HTTPException(status_code=404, detail="Not found")

    # ------------------------------------------------------------------
    # Feedback -- called by the chat widget, no admin rights needed
    # ------------------------------------------------------------------

    @app.post("/api/vanna/v2/feedback")
    async def submit_feedback(
        payload: FeedbackPayload, request: Request
    ) -> Dict[str, Any]:
        """Record a rating, and capture a candidate example on a good one."""
        user, ctx = await _context(request)

        if payload.rating not in ("positive", "negative"):
            raise HTTPException(
                status_code=400, detail="rating must be 'positive' or 'negative'"
            )

        logger.info(
            "feedback rating=%s tenant=%s user=%s conversation=%s request=%s",
            payload.rating,
            user.tenant_id,
            user.id,
            payload.conversation_id,
            payload.request_id,
        )

        # Attach the rating to the recorded generation first. This is what
        # makes success rate, failure clustering, and retrieval
        # attribution answerable later -- capturing an example without it
        # gives you training data and no way to tell if it is working.
        if generation_store is not None:
            try:
                await generation_store.set_feedback(
                    ctx,
                    payload.request_id,
                    Feedback(payload.rating),
                    payload.comment,
                )
            except Exception as e:
                logger.error("Could not record feedback: %s", e, exc_info=True)

        # Filter before capturing. A large share of positively-rated turns
        # are people looking around (`SELECT * FROM orders LIMIT 10`) or saying
        # "thanks". Storing those crowds genuinely useful examples out of a
        # fixed-size retrieval window.
        captured = False
        if (
            payload.rating == "positive"
            and example_store is not None
            and is_worth_saving(payload.question, payload.sql, dialect=dialect)
        ):
            try:
                await example_store.add(
                    ctx,
                    payload.question,
                    payload.sql,
                    # CANDIDATE, never VERIFIED. One user's thumbs-up is a
                    # signal, not a review -- they can easily be pleased by a
                    # confident wrong answer.
                    status=ExampleStatus.CANDIDATE,
                    tags=["from-feedback"],
                    dialect=dialect,
                )
                captured = True
            except ValueError as e:
                # Invalid SQL cannot become an example. Not the user's problem.
                logger.warning("Could not capture example from feedback: %s", e)
            except Exception as e:
                logger.error("Failed storing feedback example: %s", e, exc_info=True)

        return {"ok": True, "captured_example": captured}

    # ------------------------------------------------------------------
    # Admin -- review queue and knowledge management
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/examples")
    async def list_examples(
        request: Request, status: Optional[str] = None
    ) -> Dict[str, Any]:
        user, ctx = await _context(request)
        _require_admin(user)
        if example_store is None:
            return {"examples": [], "store_configured": False}

        wanted = None
        if status:
            try:
                wanted = ExampleStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"bad status: {status}")

        examples = await example_store.list_all(ctx, status=wanted)
        return {
            "examples": [e.model_dump(mode="json") for e in examples],
            "store_configured": True,
        }

    @app.post("/api/vanna/v2/admin/examples/{example_id}/status")
    async def set_example_status(
        example_id: str, request: Request
    ) -> Dict[str, Any]:
        """Promote to verified, or reject.

        Rejected examples are kept rather than deleted, so the same bad pattern
        is not silently re-captured the next time someone rates it positively.
        """
        user, ctx = await _context(request)
        _require_admin(user)
        if example_store is None:
            raise HTTPException(status_code=503, detail="No example store configured")

        body = await request.json()
        try:
            status = ExampleStatus(str(body.get("status", "")))
        except ValueError:
            raise HTTPException(status_code=400, detail="bad status")

        updated = await example_store.set_status(
            ctx, example_id, status, actor=user.id
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Example not found")
        logger.info(
            "example %s -> %s by %s (tenant=%s)",
            example_id,
            status.value,
            user.id,
            user.tenant_id,
        )
        return {"ok": True, "id": example_id, "status": status.value}

    @app.delete("/api/vanna/v2/admin/examples/{example_id}")
    async def delete_example(example_id: str, request: Request) -> Dict[str, Any]:
        user, ctx = await _context(request)
        _require_admin(user)
        if example_store is None:
            raise HTTPException(status_code=503, detail="No example store configured")
        if not await example_store.delete(ctx, example_id):
            raise HTTPException(status_code=404, detail="Example not found")
        return {"ok": True}

    @app.post("/api/vanna/v2/admin/examples")
    async def create_example(request: Request) -> Dict[str, Any]:
        """Author an example directly, already verified.

        A human writing it by hand *is* the review, so this path skips the
        candidate stage.
        """
        user, ctx = await _context(request)
        _require_admin(user)
        if example_store is None:
            raise HTTPException(status_code=503, detail="No example store configured")

        body = await request.json()
        question, sql = body.get("question"), body.get("sql")
        if not question or not sql:
            raise HTTPException(status_code=400, detail="question and sql required")

        try:
            example = await example_store.add(
                ctx,
                str(question),
                str(sql),
                status=ExampleStatus.VERIFIED,
                tags=list(body.get("tags") or []),
                dialect=dialect,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "example": example.model_dump(mode="json")}

    @app.get("/api/vanna/v2/admin/instructions")
    async def list_instructions(request: Request) -> Dict[str, Any]:
        user, ctx = await _context(request)
        _require_admin(user)
        if instruction_store is None:
            return {"instructions": [], "store_configured": False}
        rules = await instruction_store.list_all(ctx)
        return {
            "instructions": [i.model_dump(mode="json") for i in rules],
            "store_configured": True,
        }

    @app.post("/api/vanna/v2/admin/instructions")
    async def create_instruction(
        payload: InstructionPayload, request: Request
    ) -> Dict[str, Any]:
        user, ctx = await _context(request)
        _require_admin(user)
        if instruction_store is None:
            raise HTTPException(
                status_code=503, detail="No instruction store configured"
            )
        try:
            scope = InstructionScope(payload.scope)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad scope: {payload.scope}")

        try:
            created = await instruction_store.add(
                ctx,
                Instruction(
                    text=payload.text,
                    scope=scope,
                    scope_ref=payload.scope_ref,
                    priority=payload.priority,
                ),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "instruction": created.model_dump(mode="json")}

    @app.post("/api/vanna/v2/admin/instructions/{instruction_id}/enabled")
    async def toggle_instruction(
        instruction_id: str, request: Request
    ) -> Dict[str, Any]:
        user, ctx = await _context(request)
        _require_admin(user)
        if instruction_store is None:
            raise HTTPException(
                status_code=503, detail="No instruction store configured"
            )
        body = await request.json()
        enabled = bool(body.get("enabled", True))
        if not await instruction_store.set_enabled(ctx, instruction_id, enabled):
            raise HTTPException(status_code=404, detail="Instruction not found")
        return {"ok": True, "enabled": enabled}

    @app.delete("/api/vanna/v2/admin/instructions/{instruction_id}")
    async def delete_instruction(
        instruction_id: str, request: Request
    ) -> Dict[str, Any]:
        user, ctx = await _context(request)
        _require_admin(user)
        if instruction_store is None:
            raise HTTPException(
                status_code=503, detail="No instruction store configured"
            )
        if not await instruction_store.delete(ctx, instruction_id):
            raise HTTPException(status_code=404, detail="Instruction not found")
        return {"ok": True}

    @app.get("/api/vanna/v2/admin/stats")
    async def generation_stats(request: Request, hours: int = 24) -> Dict[str, Any]:
        """Quality metrics over a recent window."""
        user, ctx = await _context(request)
        _require_admin(user)
        if generation_store is None:
            return {"store_configured": False}
        stats = await generation_store.stats(ctx, since=recent_window(hours))
        return {
            "store_configured": True,
            "window_hours": hours,
            "stats": stats.model_dump(mode="json"),
            "summary": stats.summary(),
        }

    @app.get("/api/vanna/v2/admin/generations")
    async def list_generations(
        request: Request, limit: int = 50, hours: int = 168
    ) -> Dict[str, Any]:
        """Recent generations, for the review queue and for debugging."""
        user, ctx = await _context(request)
        _require_admin(user)
        if generation_store is None:
            return {"generations": [], "store_configured": False}
        records = await generation_store.list_recent(
            ctx, limit=limit, since=recent_window(hours)
        )
        return {
            "generations": [g.model_dump(mode="json") for g in records],
            "store_configured": True,
        }

    logger.info("Registered feedback and admin routes")
