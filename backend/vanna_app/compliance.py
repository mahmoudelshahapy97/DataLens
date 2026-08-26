"""Right to be forgotten: the request lifecycle and what execution does.

The interesting question is not "how do we delete rows" -- it is *which* rows,
and what happens to the ones that must not be deleted.

## Delete, redact, keep

Three categories, and the boundary between the first two is where every
implementation of this either earns trust or loses it.

**Deleted outright.** Things that exist only because this person used the system:
their conversations, their saved queries, the dashboards they own, their sessions
and API tokens, their notifications, their membership rows and their account.

**Redacted in place.** ``generations`` -- the record of what was asked. The
question text and the SQL go; the row, its timestamp, its cost and its token
counts stay, with the user id anonymised. That table is deliberately *not*
foreign-keyed to ``tenants`` precisely so that erasing a tenant cannot erase the
record of what happened under it, and a person is the same case. The personal
data is the question text, and that is what is removed.

**Kept.** ``admin_audit``. The record that administrators did things -- including
this deletion -- with the actor's address. It is the compliance artifact: it is
what is produced when somebody asks whether the request was honoured. A system
that deletes the proof of deletion cannot answer the question the deletion was
for.

That third category is a real tension rather than a dodge, and it is worth being
plain about it: the subject's address remains in the audit trail as the *subject*
of an administrative action. Under GDPR that retention is the legal-obligation
basis rather than consent, which is why it is stated here rather than quietly
done.

## Two people

A request is created by one platform admin and executed by another. Enforced in
the route, in this module, and as a CHECK constraint on the table -- three
because the API, a migration and a psql session are three different ways to
reach the same row.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .db import SCHEMA

logger = logging.getLogger("vanna.compliance")


class ComplianceError(Exception):
    """A request that will not be created or executed as asked."""


def _new_id() -> str:
    return uuid4().hex[:16]


def anonymised_address(email: str) -> str:
    """The placeholder an erased account is renamed to.

    Deterministic per request rather than per address: two deletions of the same
    address (a person who returned and left again) must not collide on a unique
    index, and a random suffix makes each row independently unlinkable.
    """
    return f"deleted-{uuid4().hex[:12]}@anonymised.invalid"


class Compliance:
    """Deletion requests, and the erasure itself."""

    def __init__(self, db: Any, directory: Any, admin_audit: Any = None) -> None:
        self.db = db
        self.directory = directory
        self.admin_audit = admin_audit

    # ------------------------------------------------------------------
    # The request lifecycle
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        subject_email: str,
        requested_by: str,
        tenant_id: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        subject = (subject_email or "").strip().lower()
        if "@" not in subject:
            raise ComplianceError("A deletion request needs the subject's email address.")

        # Requesting your own erasure is a legitimate thing to want and an
        # illegitimate thing to *self-approve*. Allowed here; the two-person rule
        # at execution is what stops it completing unilaterally.
        request_id = _new_id()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.deletion_requests
                    (id, subject_email, tenant_id, requested_by, notes)
                VALUES (%s, %s, %s, %s, %s)""",
            (request_id, subject, tenant_id or "", (requested_by or "").lower(), notes[:2000]),
        )
        await self._audit("compliance.request", requested_by, tenant_id, request_id, {
            "subject": subject, "scope": tenant_id or "all workspaces",
        })
        created = await self.get(request_id)
        assert created is not None
        return created

    async def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.deletion_requests WHERE id = %s", (request_id,)
        )
        return _request_json(row) if row else None

    async def list(self, *, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if status:
            rows = await self.db.fetch_all(
                f"""SELECT * FROM {SCHEMA}.deletion_requests
                     WHERE status = %s ORDER BY created_at DESC LIMIT %s""",
                (status, limit),
            )
        else:
            rows = await self.db.fetch_all(
                f"SELECT * FROM {SCHEMA}.deletion_requests ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
        return [_request_json(row) for row in rows or []]

    async def cancel(self, request_id: str, actor: str) -> Optional[Dict[str, Any]]:
        await self.db.execute(
            f"""UPDATE {SCHEMA}.deletion_requests
                   SET status = 'cancelled'
                 WHERE id = %s AND status = 'pending'""",
            (request_id,),
        )
        await self._audit("compliance.cancel", actor, "", request_id, {})
        return await self.get(request_id)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, request_id: str, executed_by: str) -> Dict[str, Any]:
        """Carry out an approved request. Irreversible.

        The two-person rule is checked here rather than only in the route, because
        this method is also what a future CLI or a migration script would call and
        neither goes through the route.
        """
        request = await self.get(request_id)
        if request is None:
            raise ComplianceError("No such request.")
        if request["status"] != "pending":
            raise ComplianceError(
                f"That request is {request['status']}, not pending."
            )

        actor = (executed_by or "").lower()
        if actor == request["requested_by"]:
            raise ComplianceError(
                "A deletion must be executed by a different administrator than the "
                "one who requested it."
            )

        subject = request["subject_email"]
        scope = request["tenant_id"]

        await self.db.execute(
            f"UPDATE {SCHEMA}.deletion_requests SET status = 'executing' WHERE id = %s",
            (request_id,),
        )

        try:
            outcome = await self._erase(subject, scope)
        except Exception as exc:  # noqa: BLE001
            logger.error("Deletion request %s failed: %s", request_id, exc, exc_info=True)
            await self.db.execute(
                f"""UPDATE {SCHEMA}.deletion_requests
                       SET status = 'failed', error = %s, executed_by = %s,
                           executed_at = now()
                     WHERE id = %s""",
                (f"{type(exc).__name__}: {exc}"[:2000], actor, request_id),
            )
            raise

        await self.db.execute(
            f"""UPDATE {SCHEMA}.deletion_requests
                   SET status = 'completed', executed_by = %s, executed_at = now(),
                       outcome = %s
                 WHERE id = %s""",
            (actor, json.dumps(outcome, ensure_ascii=False), request_id),
        )

        # Recorded *after* the erasure, and this row is not itself erasable. It is
        # the artifact produced when somebody asks whether the request was
        # honoured; see the module docstring on why that is deliberate.
        await self._audit("compliance.execute", executed_by, scope, request_id, {
            "subject": subject,
            "scope": scope or "all workspaces",
            "outcome": outcome,
        })

        result = await self.get(request_id)
        assert result is not None
        return result

    async def _erase(self, subject: str, tenant_id: str) -> Dict[str, Any]:
        """Delete what is theirs, redact what must survive.

        Every statement is scoped by ``tenant_id`` when the request names one.
        A request about one workspace that erased the person's account everywhere
        would be its own incident -- a contractor leaving one client does not
        stop existing.
        """
        scoped = bool(tenant_id)
        deleted: Dict[str, int] = {}
        redacted: Dict[str, int] = {}

        def where(extra: str = "") -> str:
            return (" AND tenant_id = %s" if scoped else "") + extra

        def args(*values: Any) -> tuple:
            return tuple(values) + ((tenant_id,) if scoped else ())

        # --- deleted outright ----------------------------------------
        deleted["conversations"] = await self.db.execute(
            f"DELETE FROM {SCHEMA}.conversations WHERE user_id = %s" + where(),
            args(subject),
        )
        deleted["saved_queries"] = await self.db.execute(
            f"DELETE FROM {SCHEMA}.saved_queries WHERE created_by = %s" + where(),
            args(subject),
        )
        deleted["dashboards"] = await self.db.execute(
            f"DELETE FROM {SCHEMA}.dashboards WHERE created_by = %s" + where(),
            args(subject),
        )

        # Best-effort for tables a deployment may not have yet: reports arrived in
        # migration 0015 and an older control plane will not have them. A missing
        # table must not abort an erasure that has already deleted six others.
        for table, column in (
            ("report_schedules", "run_as"),
            ("notifications", "user_email"),
        ):
            try:
                deleted[table] = await self.db.execute(
                    f"DELETE FROM {SCHEMA}.{table} WHERE {column} = %s" + where(),
                    args(subject),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipped %s during erasure: %s", table, type(exc).__name__)

        # --- redacted in place ---------------------------------------
        #
        # The question text and the SQL are the personal data; the row's existence,
        # timing and cost are the audit trail. See the module docstring.
        placeholder = anonymised_address(subject)
        redacted["generations"] = await self.db.execute(
            f"""UPDATE {SCHEMA}.generations
                   SET question = '', sql = '', user_id = %s,
                       feedback_comment = ''
                 WHERE user_id = %s""" + where(),
            args(placeholder, subject),
        )

        # --- membership and account ----------------------------------
        if scoped:
            deleted["memberships"] = await self.db.execute(
                f"DELETE FROM {SCHEMA}.tenant_users WHERE email = %s AND tenant_id = %s",
                (subject, tenant_id),
            )
            # The account itself is left alone: they are still a member elsewhere,
            # or may be. Erasing it would log them out of workspaces this request
            # said nothing about.
            deleted["account"] = 0
        else:
            deleted["memberships"] = await self.db.execute(
                f"DELETE FROM {SCHEMA}.tenant_users WHERE email = %s", (subject,)
            )
            deleted["sessions"] = await self.db.execute(
                f"DELETE FROM {SCHEMA}.sessions WHERE email = %s", (subject,)
            )
            deleted["api_tokens"] = await self.db.execute(
                f"DELETE FROM {SCHEMA}.api_tokens WHERE email = %s", (subject,)
            )
            # Anonymised rather than deleted: rows elsewhere reference the address,
            # and a dangling reference reads as corruption where a tombstone reads
            # as what actually happened.
            deleted["account"] = await self.db.execute(
                f"""UPDATE {SCHEMA}.users
                       SET email = %s, full_name = '[erased]', password_hash = '',
                           is_active = false
                     WHERE email = %s""",
                (placeholder, subject),
            )

        return {
            "subject": subject,
            "scope": tenant_id or "all workspaces",
            "deleted": {k: int(v or 0) for k, v in deleted.items()},
            "redacted": {k: int(v or 0) for k, v in redacted.items()},
            "kept": {
                "admin_audit": (
                    "Administrative actions, including this one, are retained as "
                    "the record that the request was honoured."
                ),
            },
            "at": datetime.now(timezone.utc).isoformat(),
        }

    async def _audit(
        self, action: str, actor: str, tenant_id: str, target: str, details: Dict[str, Any]
    ) -> None:
        if self.admin_audit is None:
            return
        try:
            await self.admin_audit.record(
                "tenant.update",
                actor_email=actor,
                tenant_id=tenant_id or "",
                target=f"deletion:{target}",
                details={"kind": action, **details},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record %s: %s", action, type(exc).__name__)


def _request_json(row: Dict[str, Any]) -> Dict[str, Any]:
    outcome = row.get("outcome")
    if isinstance(outcome, str):
        try:
            outcome = json.loads(outcome)
        except (TypeError, ValueError):
            outcome = {}
    return {
        "id": row["id"],
        "subject_email": row["subject_email"],
        "tenant_id": row.get("tenant_id") or "",
        "status": row["status"],
        "requested_by": row.get("requested_by") or "",
        "executed_by": row.get("executed_by"),
        "notes": row.get("notes") or "",
        "outcome": outcome or {},
        "error": row.get("error") or "",
        "created_at": row.get("created_at"),
        "executed_at": row.get("executed_at"),
    }
