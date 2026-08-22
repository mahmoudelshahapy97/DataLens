"""Reading, approving and declining a proposed change.

Deliberately **not** admin-gated as a whole. Whether a change may happen at all is
decided by an administrator, through table grants, long before anyone gets here.
This endpoint asks the person who requested a specific change to confirm that
specific statement. Requiring an administrator for that step would make the
confirmation meaningless -- they did not ask the question and cannot know whether
the statement matches what was meant.

The exception is the review queue. A workspace configured for second-person
approval routes destructive changes to a *different* administrator, and those two
endpoints are gated accordingly.

Two conventions carried over from the rest of this package: a caller who may not
see something gets 404 rather than 403 -- an id that answers differently for two
people is an id that can be used to probe what other people are doing -- and a
refusal carries its code, so the console can branch on the reason without parsing
prose.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from fastapi import HTTPException, Request
from pydantic import BaseModel

from ..authz import forbid_viewer, is_tenant_admin
from . import Deps

logger = logging.getLogger("vanna.routes.writes")


class DecisionPayload(BaseModel):
    approved: bool


def register(app: Any, deps: Deps) -> None:

    async def _service(request: Request) -> tuple:
        """The caller, and their workspace's write service.

        A workspace without writes enabled has no service, and says so as a 404:
        the endpoint genuinely does not exist for it.
        """
        user = await deps.caller(request)
        runtime = await deps.runtime_for(user)
        service = getattr(runtime, "write_service", None)
        if service is None:
            raise HTTPException(
                status_code=404, detail="Changes are not enabled for this workspace."
            )
        return user, service

    def _context(user: Any) -> Any:
        from vanna.core.tool import ToolContext

        return ToolContext(
            user=user,
            conversation_id="write-approval",
            request_id=str(uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=deps.agent_memory,
        )

    def _render(pending: Any) -> Dict[str, Any]:
        """What the console is allowed to see.

        Note what is absent: `plan`. It holds the literal values, which are the
        tenant's data -- the card needs the statement's shape and its effect,
        not its contents.
        """
        return {
            "id": pending.id,
            "status": pending.status.value,
            "operation": pending.operation,
            "tables": pending.tables,
            "expected_row_count": pending.expected_row_count,
            "is_destructive": pending.is_destructive,
            "statement_preview": pending.statement_preview,
            "parameter_summary": pending.parameter_summary,
            "requested_by": pending.requested_by,
            "requested_by_email": pending.requested_by_email,
            "expires_at": pending.expires_at.isoformat(),
            "created_at": pending.created_at.isoformat(),
            "description": pending.describe(),
        }

    @app.get("/api/vanna/v2/writes/{pending_id}")
    async def read_pending(pending_id: str, request: Request) -> Dict[str, Any]:
        user, service = await _service(request)
        pending = await service.approvals.get(_context(user), pending_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="No such change.")
        return _render(pending)

    @app.get("/api/vanna/v2/writes")
    async def list_pending(request: Request, limit: int = 50) -> Dict[str, Any]:
        """The second-person review queue.

        Admin-gated, because this is the one part of the flow that genuinely is
        an administrator's job.
        """
        user, service = await _service(request)
        if not is_tenant_admin(user, user.tenant_id, deps.settings):
            # Not 403: a non-admin should not learn that a queue exists.
            raise HTTPException(status_code=404, detail="No such resource.")
        queue = await service.pending_for_review(_context(user), limit=limit)
        return {"pending": [_render(p) for p in queue]}

    @app.post("/api/vanna/v2/writes/{pending_id}/decision")
    async def decide(
        pending_id: str, payload: DecisionPayload, request: Request
    ) -> Dict[str, Any]:
        """Approve or decline, and -- if approved -- run it.

        Approval and execution are one request on purpose. Splitting them would
        create a window in which a change is approved but not yet re-authorized,
        and something has to close that window; doing both here means the
        re-check happens against the world as it is at the moment of execution,
        which is the only moment that matters.
        """
        from vanna.core.write.errors import WriteRefusal

        user, service = await _service(request)
        forbid_viewer(user, "approve changes")
        context = _context(user)

        try:
            decided, result = await service.decide_and_execute(
                context,
                pending_id,
                approve=payload.approved,
                decided_by=user.id,
                decider_is_admin=is_tenant_admin(user, user.tenant_id, deps.settings),
            )
        except WriteRefusal as refusal:
            logger.info(
                "Change %s refused for %s: %s", pending_id, user.tenant_id, refusal.code
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": refusal.code.value,
                    "message": refusal.message,
                    "repairable": refusal.repairable,
                },
            )

        return {
            "id": decided.id,
            "status": decided.status.value,
            "rows_affected": result.rows_affected if result else 0,
            "tables": decided.tables,
        }
