"""Business rules: the workspace's own, and its choices about the platform's.

The library ships instruction endpoints already. They are not used here, and
``wiring`` passes them no store, for two reasons that are easy to miss:

* their guard is ``"admin" in group_memberships``, which **refuses a platform
  admin** administering a customer's workspace -- ``authz.is_tenant_admin``
  passes them, that check does not;
* they take no tenant in the path, so there is nothing to authorize the request
  against. The workspace is whatever the caller's own header resolved to.

These take the tenant in the path, like every other administrative route here,
and go through ``require_tenant_admin``.

The refusal codes are worth stating. A platform rule that a workspace tries to
edit or delete answers **409**, not 404 and not 500: the rule exists, it applies,
and it is being enforced -- which is a different thing from "no such rule", and a
different thing again from a bug.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from vanna.capabilities.knowledge import (
    Instruction,
    InstructionOrigin,
    InstructionScope,
    LockedInstructionError,
)

from ..authz import forbid_viewer, require_tenant_admin
from . import Deps

logger = logging.getLogger("vanna.routes.instructions")


class InstructionCreate(BaseModel):
    """What a caller may set when writing a rule.

    Note what is absent: ``origin``, ``locked`` and ``source_pack``. Provenance is
    decided here, never accepted from the request -- otherwise a workspace could
    post itself a rule marked as belonging to the platform, and then nobody could
    delete it.
    """

    text: str = Field(min_length=1, max_length=4000)
    scope: str = "global"
    scope_ref: Optional[str] = Field(default=None, max_length=512)
    priority: int = Field(default=0, ge=-1000, le=1000)


class InstructionPatch(BaseModel):
    """A partial edit. Every field optional, so a client that does not know
    about a field cannot revert it."""

    text: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    scope: Optional[str] = None
    scope_ref: Optional[str] = Field(default=None, max_length=512)
    priority: Optional[int] = Field(default=None, ge=-1000, le=1000)
    enabled: Optional[bool] = None


class EnabledPayload(BaseModel):
    enabled: bool


def _scope(raw: Optional[str]) -> Optional[InstructionScope]:
    if raw is None:
        return None
    try:
        return InstructionScope(raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope {raw!r}. Expected one of: "
            + ", ".join(s.value for s in InstructionScope),
        )


def _locked_as_conflict(exc: LockedInstructionError) -> HTTPException:
    reason = exc.reason
    message = (
        "This rule applies to every workspace and cannot be switched off here."
        if reason == "not_disableable"
        else "This rule belongs to the platform and cannot be changed here."
    )
    return HTTPException(
        status_code=409,
        detail={"code": f"instruction_{reason}", "id": exc.instruction_id,
                "message": message},
    )


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings
    base = "/api/vanna/v2/admin/tenants/{tenant_id}"

    async def _admin(request: Request, tenant_id: str) -> Any:
        user = await deps.caller(request)
        require_tenant_admin(user, tenant_id, settings)
        return user

    def _store() -> Any:
        return deps.platform.instructions

    def _library() -> Any:
        return deps.platform.instruction_library

    def _context(user: Any, tenant_id: str) -> Any:
        """Scoped to the workspace being administered, not the caller's own.

        Same trap the grants routes document: a platform admin managing
        workspace B would otherwise read and write workspace A's rules, with
        every permission check passing.
        """
        from vanna.core.tool import ToolContext

        return ToolContext(
            user=user,
            conversation_id="admin-instructions",
            request_id="admin-instructions",
            tenant_id=tenant_id,
            agent_memory=deps.agent_memory,
        )

    async def _audit(user: Any, tenant_id: str, action: str, details: Dict) -> None:
        if deps.admin_audit is None:
            return
        await deps.admin_audit.record(
            action,
            actor_email=getattr(user, "email", "") or "",
            tenant_id=tenant_id,
            target="instructions",
            details=details,
        )

    def _render(rule: Instruction, disableable: set) -> Dict[str, Any]:
        return {
            "id": rule.id,
            "text": rule.text,
            "scope": rule.scope.value,
            "scope_ref": rule.scope_ref,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "origin": rule.origin.value,
            "locked": rule.origin == InstructionOrigin.PLATFORM,
            # Computed, never stored on the rule: whether a platform rule may be
            # switched off is a property of the shipped YAML, and keeping one
            # source of truth means the two cannot disagree.
            "disableable": rule.id in disableable,
            "source_pack": rule.source_pack,
            "created_by": rule.created_by,
            "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
        }

    # -- rules ---------------------------------------------------------

    @app.get(base + "/instructions")
    async def list_instructions(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        # Rules used to live in markdown files, and are brought across once. That
        # normally happens on a workspace's first use, which needs a warehouse
        # connection -- too late for this screen, where an administrator opening
        # it after an upgrade would otherwise see none of their own rules and
        # conclude they had been lost. This costs two queries and no connection.
        await deps.platform.ensure_instructions_imported(tenant_id)

        store = _store()
        disableable = _library().disableable_ids()
        rules = await store.list_all(_context(user, tenant_id))
        return {
            "instructions": [_render(r, disableable) for r in rules],
            "store_configured": True,
        }

    @app.post(base + "/instructions")
    async def create_instruction(
        tenant_id: str, payload: InstructionCreate, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        forbid_viewer(user, "add a business rule")

        scope = _scope(payload.scope) or InstructionScope.GLOBAL
        if scope != InstructionScope.GLOBAL and not payload.scope_ref:
            raise HTTPException(
                status_code=400,
                detail=f"A '{scope.value}' rule needs a scope reference naming the "
                "data source, table, or group it applies to.",
            )

        try:
            created = await _store().add(
                _context(user, tenant_id),
                Instruction(
                    text=payload.text,
                    scope=scope,
                    scope_ref=payload.scope_ref or None,
                    priority=payload.priority,
                    created_by=getattr(user, "email", None),
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        await _audit(user, tenant_id, "instruction.create", {"id": created.id})
        return {"ok": True, "instruction": _render(created, _library().disableable_ids())}

    @app.put(base + "/instructions/{instruction_id}")
    async def update_instruction(
        tenant_id: str,
        instruction_id: str,
        payload: InstructionPatch,
        request: Request,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        forbid_viewer(user, "edit a business rule")

        try:
            updated = await _store().update(
                _context(user, tenant_id),
                instruction_id,
                text=payload.text,
                scope=_scope(payload.scope),
                scope_ref=payload.scope_ref,
                priority=payload.priority,
                enabled=payload.enabled,
            )
        except LockedInstructionError as exc:
            raise _locked_as_conflict(exc)
        except NotImplementedError:
            raise HTTPException(
                status_code=501,
                detail="This deployment's instruction store cannot edit rules in "
                "place. Delete the rule and add it again.",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if updated is None:
            raise HTTPException(status_code=404, detail="No such rule.")

        await _audit(user, tenant_id, "instruction.update", {"id": instruction_id})
        return {"ok": True, "instruction": _render(updated, _library().disableable_ids())}

    @app.post(base + "/instructions/{instruction_id}/enabled")
    async def set_instruction_enabled(
        tenant_id: str,
        instruction_id: str,
        payload: EnabledPayload,
        request: Request,
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        forbid_viewer(user, "change a business rule")

        try:
            changed = await _store().set_enabled(
                _context(user, tenant_id), instruction_id, payload.enabled
            )
        except LockedInstructionError as exc:
            raise _locked_as_conflict(exc)

        if not changed:
            raise HTTPException(status_code=404, detail="No such rule.")

        await _audit(
            user,
            tenant_id,
            "instruction.update",
            {"id": instruction_id, "enabled": payload.enabled},
        )
        return {"ok": True, "enabled": payload.enabled}

    @app.delete(base + "/instructions/{instruction_id}")
    async def delete_instruction(
        tenant_id: str, instruction_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        forbid_viewer(user, "delete a business rule")

        try:
            removed = await _store().delete(_context(user, tenant_id), instruction_id)
        except LockedInstructionError as exc:
            raise _locked_as_conflict(exc)

        if not removed:
            raise HTTPException(status_code=404, detail="No such rule.")

        await _audit(user, tenant_id, "instruction.delete", {"id": instruction_id})
        return {"ok": True}

    # -- starter packs -------------------------------------------------

    def _tenant_store() -> Any:
        """The workspace's own store, beneath the baseline layer.

        Pack bookkeeping is not part of the ``InstructionStore`` contract, so it
        is reached on the concrete store rather than through the layer.
        """
        return deps.platform.tenant_instructions

    @app.get(base + "/instruction-packs")
    async def list_packs(tenant_id: str, request: Request) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        store = _tenant_store()

        enabled: set = set()
        if hasattr(store, "enabled_packs"):
            enabled = await store.enabled_packs(_context(user, tenant_id))

        packs: List[Dict[str, Any]] = []
        for pack in _library().packs.values():
            packs.append({
                "id": pack.id,
                "name": pack.name,
                "description": pack.description,
                "instruction_count": len(pack.instructions),
                "enabled": pack.id in enabled,
                "preview": [r.text for r in pack.instructions[:3]],
            })
        packs.sort(key=lambda p: p["name"].lower())
        return {"packs": packs}

    @app.post(base + "/instruction-packs/{pack_id}/enable")
    async def enable_pack(
        tenant_id: str, pack_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        forbid_viewer(user, "add a starter pack")

        pack = _library().pack(pack_id)
        if pack is None:
            raise HTTPException(status_code=404, detail="No such starter pack.")

        store = _tenant_store()
        if not hasattr(store, "copy_pack"):
            raise HTTPException(
                status_code=503,
                detail="Starter packs need the control-plane database.",
            )

        added, skipped = await store.copy_pack(_context(user, tenant_id), pack)
        await _audit(
            user,
            tenant_id,
            "instruction.library_enabled",
            {"pack": pack_id, "added": added, "skipped": skipped},
        )
        return {"ok": True, "added": added, "skipped": skipped}

    @app.delete(base + "/instruction-packs/{pack_id}")
    async def remove_pack(
        tenant_id: str, pack_id: str, request: Request
    ) -> Dict[str, Any]:
        user = await _admin(request, tenant_id)
        forbid_viewer(user, "remove a starter pack")

        store = _tenant_store()
        if not hasattr(store, "remove_pack"):
            raise HTTPException(
                status_code=503,
                detail="Starter packs need the control-plane database.",
            )

        removed, kept = await store.remove_pack(_context(user, tenant_id), pack_id)
        await _audit(
            user,
            tenant_id,
            "instruction.library_removed",
            {"pack": pack_id, "removed": removed, "kept": kept},
        )
        # `kept` is not a rounding error: a rule somebody edited is theirs now,
        # and removing the pack it arrived in must not delete their work.
        return {"ok": True, "removed": removed, "kept": kept}
