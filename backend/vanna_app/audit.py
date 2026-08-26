"""Audit trails, for agent events and for administrative actions.

The library defines an ``AuditLogger`` ABC and ``Agent.__init__`` already accepts
one. Nothing ever passed an implementation, so every tool invocation and every
access decision went unrecorded.

Administrative actions were worse. Repointing a workspace at a different database,
promoting somebody to admin, changing a plan, resetting a password -- all existed
only as lines on stdout: not queryable, not retained, and not scoped to a tenant, so
a workspace admin could never be shown their own workspace's history. "Who changed
this, and when" is the first question after an incident and it had no answer.

Both writers are **best-effort on the request path**. An audit write that can fail a
user's request converts a logging problem into an outage. They are not best-effort
about *content*: redaction happens before the value reaches the database, so a
credential cannot be audited into permanence.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from vanna.core.audit import AuditLogger

from .db import SCHEMA
from .observability import current_request_id, current_tenant_id
from .secrets import Secret
from .tenancy import _iso

logger = logging.getLogger("vanna.audit")

#: Keys whose values never reach the audit table, whatever they contain. Matched as
#: substrings and case-insensitively, so ``db_password`` and ``X-LLM-Key`` are both
#: caught without anybody having to enumerate spellings.
_SENSITIVE = ("password", "secret", "token", "api_key", "apikey", "credential",
              "database_url", "authorization", "cookie", "llm-key", "llm_key")


def redact(value: Any, *, depth: int = 0) -> Any:
    """A copy of ``value`` with anything credential-shaped replaced.

    Recursive, depth-limited, and applied on the way *in* rather than on the way
    out: an audit row is written once and read for years, so the redaction has to
    happen before it is durable.
    """
    if depth > 6:
        return "..."
    if isinstance(value, Secret):
        return "***"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = str(key).lower()
            out[key] = "***" if any(s in name for s in _SENSITIVE) else redact(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in value][:50]
    if isinstance(value, str):
        # A bare connection string passed as a value rather than under a telling key.
        if "://" in value and "@" in value:
            return "***"
        return value[:2000]
    return value


# ----------------------------------------------------------------------
# Agent events
# ----------------------------------------------------------------------


class PostgresAuditLogger(AuditLogger):
    """Writes the library's audit events to the control plane.

    Every tool invocation, every access check, every denial. The denials are what
    anyone actually goes looking for, which is why migration 0004 gives them their
    own partial index.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    async def log_event(self, event: Any) -> None:
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)

        # Lift the columns worth indexing out of the document; the rest stays in
        # jsonb, so a new event type needs no migration.
        # The library's `AuditEvent` has no tenant field -- it is a
        # single-tenant model -- so this pop always yielded "". Every row landed
        # with an empty tenant_id, and `recent()` filters `WHERE tenant_id = %s`
        # with a real one: twenty thousand events were written and none of them
        # were readable. The access-log screen looked like a workspace nobody had
        # used.
        #
        # The request-scoped tenant is the answer: `identity.py` binds it for
        # every authenticated request, and these events are only ever produced
        # inside one.
        tenant_id = str(payload.pop("tenant_id", "") or "") or current_tenant_id()
        conversation_id = str(payload.pop("conversation_id", "") or "")
        request_id = str(payload.pop("request_id", "") or "") or current_request_id()
        tool_name = payload.pop("tool_name", None)
        access_granted = payload.pop("access_granted", None)

        try:
            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.audit_events
                        (event_id, event_type, tenant_id, user_id, user_email,
                         conversation_id, request_id, tool_name, access_granted,
                         payload, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, coalesce(%s, now()))""",
                (
                    str(payload.get("event_id") or ""),
                    str(payload.get("event_type") or "unknown"),
                    tenant_id,
                    str(payload.get("user_id") or ""),
                    str(payload.get("user_email") or ""),
                    conversation_id,
                    request_id,
                    tool_name,
                    access_granted,
                    json.dumps(redact(payload), default=str),
                    payload.get("timestamp"),
                ),
            )
        except Exception as exc:
            # Never fails the request that produced the event.
            logger.warning("Could not write audit event: %s", exc)

    async def recent(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
        denied_only: bool = False,
    ) -> List[Dict[str, Any]]:
        sql = f"""SELECT event_id, event_type, user_email, tool_name, access_granted,
                         request_id, payload, created_at
                    FROM {SCHEMA}.audit_events WHERE tenant_id = %s"""
        params: List[Any] = [tenant_id]
        if denied_only:
            sql += " AND access_granted = false"
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(min(max(limit, 1), 500))

        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
        return rows


# ----------------------------------------------------------------------
# Administrative actions
# ----------------------------------------------------------------------

#: The action vocabulary. A closed set rather than free text, so the table can be
#: filtered and alerted on without anybody having to guess how a colleague spelled
#: "role change".
ACTIONS = (
    "tenant.create", "tenant.update", "tenant.delete", "tenant.rebind",
    "tenant.purge_data", "tenant.writes_granted", "tenant.writes_revoked",
    "member.add", "member.update", "member.remove", "member.role_change",
    "starter.add", "starter.delete",
    "billing.plan", "billing.cancel", "billing.payment",
    "account.create", "account.reset", "account.disable", "account.enable",
    "auth.password_change", "auth.sessions_revoked", "auth.token_create",
    "auth.token_revoke", "auth.reset_requested", "auth.reset_redeemed",
    "knowledge.rescan", "datasource.test",
    "instruction.create", "instruction.update", "instruction.delete",
    "instruction.library_enabled", "instruction.library_removed",
    "grants.preset_applied", "grants.policy_changed", "grants.revoked",
)


class AdminAudit:
    """The administrative action log."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def record(
        self,
        action: str,
        *,
        actor_email: str,
        tenant_id: str = "",
        target: str = "",
        details: Optional[Dict[str, Any]] = None,
        actor_ip: str = "",
    ) -> None:
        """Record one administrative action. Never raises."""
        if action not in ACTIONS:
            # Not fatal -- refusing to record an action because its name is new
            # would lose exactly the event somebody added in a hurry -- but loud,
            # because the vocabulary is meant to stay closed.
            logger.warning("Recording unknown audit action %r; add it to ACTIONS.", action)

        try:
            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.admin_audit
                        (actor_email, actor_ip, action, tenant_id, target, details, request_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    (actor_email or "").lower(),
                    actor_ip,
                    action,
                    tenant_id,
                    target,
                    json.dumps(redact(details or {}), default=str),
                    current_request_id(),
                ),
            )
        except Exception as exc:
            logger.warning("Could not record admin action %s: %s", action, exc)

    async def recent(
        self,
        *,
        tenant_id: str = "",
        actor_email: str = "",
        action: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Recent actions, newest first.

        ``tenant_id`` is how a workspace admin is shown their own history and
        nothing else; a platform admin passes nothing and sees everything.
        """
        sql = f"""SELECT id, actor_email, actor_ip, action, tenant_id, target,
                         details, request_id, created_at
                    FROM {SCHEMA}.admin_audit WHERE true"""
        params: List[Any] = []
        if tenant_id:
            sql += " AND tenant_id = %s"
            params.append(tenant_id)
        if actor_email:
            sql += " AND actor_email = %s"
            params.append(actor_email.lower())
        if action:
            sql += " AND action = %s"
            params.append(action)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(min(max(limit, 1), 500))

        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            row["id"] = str(row["id"])
            row["created_at"] = _iso(row["created_at"])
        return rows

    async def purge_older_than(self, days: int) -> int:
        if days <= 0:
            return 0
        return await self.db.execute(
            f"""DELETE FROM {SCHEMA}.admin_audit
                 WHERE created_at < now() - make_interval(days => %s)""",
            (days,),
        )


class NullAdminAudit:
    """No control plane, nothing to write to. Keeps call sites unconditional."""

    async def record(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def recent(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        return []

    async def purge_older_than(self, days: int) -> int:
        return 0
