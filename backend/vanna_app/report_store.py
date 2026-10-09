"""Report schedules, runs and notifications, over the control plane.

Raw SQL against ``vanna_app``, like every other store here. Two methods carry the
weight and both are about *exactly once*:

``materialise_due``  turns due schedules into queued runs. Called only under
                     ``locks.KEY_REPORTS``, so one worker does it per tick.
``claim``            takes queued runs off the front with ``FOR UPDATE SKIP
                     LOCKED``, which every worker may do concurrently.

The split is the whole concurrency design. Materialising is check-then-act and
must not race -- two workers finding the same schedule due would queue it twice
and two identical emails would go out. Claiming is inherently safe: SKIP LOCKED
means a row is handed to exactly one transaction, and the workers that miss it
simply take the next one.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .cron import CronError, next_occurrence
from .db import SCHEMA

logger = logging.getLogger("vanna.reports.store")

#: How many runs one worker takes per tick. Small on purpose: a run holds a
#: warehouse connection and sends mail, so a worker that claimed fifty would hold
#: them all while the other three sat idle.
CLAIM_BATCH = 4

#: Runs older than this are pruned by housekeeping. The artifact is the bulk of a
#: row and the reason not to keep them forever; the fact that a run *happened*
#: outlives it in `generations` and `admin_audit`, which is what an investigation
#: reads.
RUN_RETENTION_DAYS = 90


def _new_id() -> str:
    return uuid4().hex[:16]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class ReportStore:
    """Reads and writes ``report_schedules``, ``report_runs`` and ``notifications``."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------

    async def list_schedules(self, tenant_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT * FROM {SCHEMA}.report_schedules
                 WHERE tenant_id = %s
                 ORDER BY name""",
            (tenant_id,),
        )
        return [_schedule_json(row) for row in rows or []]

    async def get_schedule(self, tenant_id: str, schedule_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.report_schedules WHERE tenant_id = %s AND id = %s",
            (tenant_id, schedule_id),
        )
        return _schedule_json(row) if row else None

    async def create_schedule(
        self,
        tenant_id: str,
        *,
        dashboard_id: str,
        name: str,
        cron: str,
        timezone_name: str,
        run_as: str,
        channels: List[Dict[str, Any]],
        parameters: Dict[str, Any],
        fmt: str,
        created_by: str,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        schedule_id = _new_id()
        # Computed here rather than left NULL: a schedule with no next_run_at is
        # invisible to the materialiser, so it would sit inert until something
        # else happened to update it.
        next_run = next_occurrence(cron, datetime.now(timezone.utc), tz=timezone_name)

        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.report_schedules
                    (id, tenant_id, dashboard_id, name, parameters, cron, timezone,
                     run_as, channels, format, is_active, next_run_at, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                schedule_id, tenant_id, dashboard_id, name, _json(parameters), cron,
                timezone_name, run_as, _json(channels), fmt, is_active,
                next_run if is_active else None, created_by,
            ),
        )
        created = await self.get_schedule(tenant_id, schedule_id)
        assert created is not None
        return created

    async def update_schedule(
        self, tenant_id: str, schedule_id: str, changes: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Patch a schedule.

        ``next_run_at`` is recomputed whenever the cron, the zone or the active
        flag changes -- and *only* then. Recomputing on every patch would let
        renaming a schedule silently postpone it to the next occurrence, skipping
        a run that was minutes away.
        """
        current = await self.get_schedule(tenant_id, schedule_id)
        if current is None:
            return None

        allowed = (
            "name", "cron", "timezone", "run_as", "channels",
            "parameters", "format", "is_active", "dashboard_id",
        )
        sets: List[str] = []
        params: List[Any] = []
        for key in allowed:
            if key not in changes:
                continue
            value = changes[key]
            sets.append(f"{key} = %s")
            params.append(_json(value) if key in ("channels", "parameters") else value)

        if not sets:
            return current

        merged = {**current, **changes}
        if any(k in changes for k in ("cron", "timezone", "is_active")):
            if merged.get("is_active"):
                sets.append("next_run_at = %s")
                params.append(
                    next_occurrence(
                        merged["cron"], datetime.now(timezone.utc),
                        tz=merged.get("timezone") or "UTC",
                    )
                )
            else:
                # Paused: clear the due time rather than leave a stale one. A
                # resumed schedule computes a fresh one, so a pause of any length
                # cannot produce a burst of overdue runs on resume.
                sets.append("next_run_at = NULL")

        sets.append("updated_at = now()")
        params.extend([tenant_id, schedule_id])

        await self.db.execute(
            f"UPDATE {SCHEMA}.report_schedules SET {', '.join(sets)} "
            "WHERE tenant_id = %s AND id = %s",
            tuple(params),
        )
        return await self.get_schedule(tenant_id, schedule_id)

    async def delete_schedule(self, tenant_id: str, schedule_id: str) -> bool:
        deleted = await self.db.execute(
            f"DELETE FROM {SCHEMA}.report_schedules WHERE tenant_id = %s AND id = %s",
            (tenant_id, schedule_id),
        )
        return bool(deleted)

    # ------------------------------------------------------------------
    # The queue
    # ------------------------------------------------------------------

    async def materialise_due(self, *, now: Optional[datetime] = None) -> int:
        """Queue one run for every schedule that has come due.

        **Call only while holding ``locks.KEY_REPORTS``.** This is check-then-act
        across two statements; run it concurrently and two workers both see the
        same schedule as due, both insert a run, and the report goes out twice.

        A schedule whose cron no longer parses is deactivated rather than skipped.
        Skipping means retrying a permanently broken expression every tick,
        forever, in a background loop nobody is reading.
        """
        moment = now or datetime.now(timezone.utc)

        rows = await self.db.fetch_all(
            f"""SELECT * FROM {SCHEMA}.report_schedules
                 WHERE is_active AND next_run_at IS NOT NULL AND next_run_at <= %s
                 ORDER BY next_run_at
                 LIMIT 200""",
            (moment,),
        )

        queued = 0
        for row in rows or []:
            schedule = _schedule_json(row)
            try:
                upcoming = next_occurrence(
                    schedule["cron"], moment, tz=schedule.get("timezone") or "UTC"
                )
            except CronError as exc:
                logger.error(
                    "Deactivating report schedule %s: %s", schedule["id"], exc
                )
                await self.db.execute(
                    f"UPDATE {SCHEMA}.report_schedules "
                    "SET is_active = false, next_run_at = NULL, updated_at = now() "
                    "WHERE id = %s",
                    (schedule["id"],),
                )
                continue

            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.report_runs
                        (id, tenant_id, schedule_id, dashboard_id, run_as,
                         parameters, channels, format, status, run_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)""",
                (
                    _new_id(), schedule["tenant_id"], schedule["id"],
                    schedule["dashboard_id"], schedule["run_as"],
                    _json(schedule["parameters"]), _json(schedule["channels"]),
                    schedule["format"],
                    # The time it *became due*, not now: a materialiser that ran
                    # late must not make the run look late too, and the history
                    # should say when it was supposed to go.
                    schedule["next_run_at"],
                ),
            )
            # Advanced in the same pass, so a crash between the insert and this
            # update is the only way to double-queue -- and the next tick would
            # find next_run_at still in the past and queue one more, which is
            # visible in the history rather than silent.
            await self.db.execute(
                f"UPDATE {SCHEMA}.report_schedules "
                "SET next_run_at = %s, updated_at = now() WHERE id = %s",
                (upcoming, schedule["id"]),
            )
            queued += 1

        return queued

    async def claim(self, worker: str, *, limit: int = CLAIM_BATCH) -> List[Dict[str, Any]]:
        """Take up to ``limit`` queued runs, exclusively.

        ``FOR UPDATE SKIP LOCKED`` is what makes this safe without a lock: a row
        another transaction is already holding is passed over rather than waited
        on, so four workers drain one queue without blocking each other and
        without any of them taking a row twice.
        """
        rows = await self.db.fetch_all(
            f"""UPDATE {SCHEMA}.report_runs
                   SET status = 'claimed', claimed_by = %s, claimed_at = now()
                 WHERE id IN (
                       SELECT id FROM {SCHEMA}.report_runs
                        WHERE status = 'pending' AND run_at <= now()
                        ORDER BY run_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                 )
             RETURNING *""",
            (worker, limit),
        )
        return [_run_json(row) for row in rows or []]

    async def enqueue_now(
        self,
        tenant_id: str,
        *,
        dashboard_id: str,
        run_as: str,
        parameters: Dict[str, Any],
        channels: List[Dict[str, Any]],
        fmt: str,
        requested_by: str,
        schedule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue a run immediately, for a Run now."""
        run_id = _new_id()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.report_runs
                    (id, tenant_id, schedule_id, dashboard_id, run_as, parameters,
                     channels, format, status, run_at, requested_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', now(), %s)""",
            (
                run_id, tenant_id, schedule_id, dashboard_id, run_as,
                _json(parameters), _json(channels), fmt, requested_by,
            ),
        )
        run = await self.get_run(tenant_id, run_id)
        assert run is not None
        return run

    async def mark_running(self, run_id: str) -> None:
        await self.db.execute(
            f"UPDATE {SCHEMA}.report_runs SET status = 'running', started_at = now() "
            "WHERE id = %s",
            (run_id,),
        )

    async def finish(
        self,
        run_id: str,
        *,
        ok: bool,
        tile_count: int = 0,
        row_count: int = 0,
        error: str = "",
        artifact: Optional[bytes] = None,
        artifact_filename: str = "",
    ) -> None:
        await self.db.execute(
            f"""UPDATE {SCHEMA}.report_runs
                   SET status = %s,
                       finished_at = now(),
                       tile_count = %s,
                       row_count = %s,
                       error = %s,
                       artifact = %s,
                       artifact_filename = %s,
                       artifact_bytes = %s
                 WHERE id = %s""",
            (
                "succeeded" if ok else "failed",
                tile_count, row_count,
                # Empty string rather than NULL for "no error": the column is read
                # into a UI that shows a failure panel when it is truthy, and two
                # spellings of absence is one too many.
                (error or "")[:4000],
                artifact, artifact_filename,
                len(artifact) if artifact else 0,
                run_id,
            ),
        )
        if ok:
            # On success, whatever was produced. Not gated on the artifact: a
            # webhook-only schedule stores no bytes, and leaving its last_run_at
            # empty would read in the list as "never ran".
            await self.db.execute(
                f"""UPDATE {SCHEMA}.report_schedules SET last_run_at = now()
                     WHERE id = (SELECT schedule_id
                                   FROM {SCHEMA}.report_runs
                                  WHERE id = %s)""",
                (run_id,),
            )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def list_runs(
        self, tenant_id: str, *, schedule_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        if schedule_id:
            rows = await self.db.fetch_all(
                f"""SELECT * FROM {SCHEMA}.report_runs
                     WHERE tenant_id = %s AND schedule_id = %s
                     ORDER BY created_at DESC LIMIT %s""",
                (tenant_id, schedule_id, limit),
            )
        else:
            rows = await self.db.fetch_all(
                f"""SELECT * FROM {SCHEMA}.report_runs
                     WHERE tenant_id = %s
                     ORDER BY created_at DESC LIMIT %s""",
                (tenant_id, limit),
            )
        return [_run_json(row) for row in rows or []]

    async def get_run(self, tenant_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.report_runs WHERE tenant_id = %s AND id = %s",
            (tenant_id, run_id),
        )
        return _run_json(row) if row else None

    async def artifact(self, tenant_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """The stored bytes. Fetched separately so listing history is cheap."""
        row = await self.db.fetch_one(
            f"""SELECT artifact, artifact_filename, format
                  FROM {SCHEMA}.report_runs
                 WHERE tenant_id = %s AND id = %s AND artifact IS NOT NULL""",
            (tenant_id, run_id),
        )
        if not row:
            return None
        payload = row["artifact"]
        return {
            # psycopg2 hands back a memoryview for bytea; FastAPI's Response wants
            # bytes and a memoryview reaches it as an empty body.
            "bytes": bytes(payload) if payload is not None else b"",
            "filename": row["artifact_filename"] or "report",
            "format": row["format"],
        }

    async def purge_runs_older_than(self, days: int) -> int:
        if days <= 0:
            return 0
        return await self.db.execute(
            f"DELETE FROM {SCHEMA}.report_runs "
            "WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def notify(
        self,
        tenant_id: str,
        user_email: str,
        *,
        title: str,
        body: str = "",
        link: str = "",
        kind: str = "report_run",
    ) -> None:
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.notifications
                    (id, tenant_id, user_email, kind, title, body, link)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (_new_id(), tenant_id, user_email.lower(), kind, title, body, link),
        )

    async def list_notifications(
        self, tenant_id: str, user_email: str, *, limit: int = 30
    ) -> Dict[str, Any]:
        rows = await self.db.fetch_all(
            f"""SELECT id, kind, title, body, link, read_at, created_at
                  FROM {SCHEMA}.notifications
                 WHERE tenant_id = %s AND user_email = %s
                 ORDER BY created_at DESC LIMIT %s""",
            (tenant_id, user_email.lower(), limit),
        )
        unread = await self.db.fetch_value(
            f"""SELECT count(*) FROM {SCHEMA}.notifications
                 WHERE tenant_id = %s AND user_email = %s AND read_at IS NULL""",
            (tenant_id, user_email.lower()),
            default=0,
        )
        return {
            "notifications": [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "title": r["title"],
                    "body": r["body"],
                    "link": r["link"],
                    "read": r["read_at"] is not None,
                    "created_at": _iso(r["created_at"]),
                }
                for r in rows or []
            ],
            "unread": int(unread or 0),
        }

    async def mark_read(self, tenant_id: str, user_email: str, notification_id: str) -> bool:
        # Scoped by address as well as tenant: a member must not be able to clear
        # somebody else's bell by guessing an id.
        updated = await self.db.execute(
            f"""UPDATE {SCHEMA}.notifications SET read_at = now()
                 WHERE tenant_id = %s AND user_email = %s AND id = %s
                   AND read_at IS NULL""",
            (tenant_id, user_email.lower(), notification_id),
        )
        return bool(updated)

    async def mark_all_read(self, tenant_id: str, user_email: str) -> int:
        return await self.db.execute(
            f"""UPDATE {SCHEMA}.notifications SET read_at = now()
                 WHERE tenant_id = %s AND user_email = %s AND read_at IS NULL""",
            (tenant_id, user_email.lower()),
        )


# ----------------------------------------------------------------------
# Row shaping
# ----------------------------------------------------------------------


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _loads(value: Any, fallback: Any) -> Any:
    """jsonb comes back parsed; a text column does not. Tolerate both."""
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _schedule_json(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "dashboard_id": row["dashboard_id"],
        "name": row["name"],
        "parameters": _loads(row.get("parameters"), {}),
        "cron": row["cron"],
        "timezone": row.get("timezone") or "UTC",
        "run_as": row["run_as"],
        "channels": _loads(row.get("channels"), []),
        "format": row.get("format") or "html",
        "is_active": bool(row.get("is_active")),
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _run_json(row: Dict[str, Any]) -> Dict[str, Any]:
    """A run, without its artifact.

    The bytes are deliberately absent: a history list of fifty runs would
    otherwise carry fifty megabytes of inlined HTML. `artifact()` fetches them one
    at a time, for the download that asked.
    """
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "schedule_id": row.get("schedule_id"),
        "dashboard_id": row["dashboard_id"],
        "run_as": row["run_as"],
        "parameters": _loads(row.get("parameters"), {}),
        "channels": _loads(row.get("channels"), []),
        "format": row.get("format") or "html",
        "status": row["status"],
        "run_at": row.get("run_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "tile_count": int(row.get("tile_count") or 0),
        "row_count": int(row.get("row_count") or 0),
        "error": row.get("error") or "",
        "artifact_filename": row.get("artifact_filename") or "",
        "artifact_bytes": int(row.get("artifact_bytes") or 0),
        "requested_by": row.get("requested_by") or "",
        "created_at": row.get("created_at"),
    }
