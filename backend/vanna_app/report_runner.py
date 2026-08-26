"""Executing a queued report run, and the loop that keeps the queue moving.

## The rule this module exists to keep

**Tiles execute as the caller.** Two people opening the same dashboard see
different numbers, because the row and column rules that apply to their questions
apply there too; an export can therefore never become a way around a grant. That
property is stated at the top of ``routes/dashboards.py`` and it is what the whole
dashboards design rests on.

A scheduled run has no caller. So one is *named* -- ``report_schedules.run_as`` --
and this module resolves that address into a real ``User`` through the same
membership check the HTTP resolver performs, then renders through the same
``render_dashboard(registry, user, ...)`` call ``GET /dashboards/{id}/data`` uses.
There is deliberately no second rendering path: a report that rendered through its
own code would be a report whose numbers nobody could argue from.

Three consequences follow, and each is enforced below rather than documented and
hoped for:

* **Membership is rechecked at execution time.** Removing somebody from a
  workspace stops their reports on the next tick. Trusting the address that was
  valid when the schedule was created means a departed employee keeps receiving
  data for as long as the schedule lives.
* **A run fails closed.** If the identity cannot be resolved, the run is marked
  failed and nothing is delivered. It does not fall back to the creator, to a
  platform admin, or to an unrestricted identity.
* **Recipients are constrained.** See ``report_delivery``: a schedule that could
  mail arbitrary addresses is a grant bypass with a mail server attached.

## Why the loop is shaped this way

Two phases, because they have opposite concurrency requirements.

``materialise`` is check-then-act -- find due schedules, queue a run, advance the
clock -- and must happen once per tick across the whole deployment. It runs under
``locks.KEY_REPORTS``.

``drain`` claims queued runs with ``FOR UPDATE SKIP LOCKED`` and executes them.
Every worker does this concurrently; that is the point. A row is handed to exactly
one transaction, so four workers share the load and none of them sends the same
report twice.

The precedent for an in-process loop is ``_housekeeping`` in ``wiring.py``, whose
comment notes that every replica running it is harmless because the work is
idempotent DELETEs. Report delivery is emphatically *not* idempotent, which is why
this one needs the lock and the claim and that one does not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import locks
from .identity import _groups_for
from .report_store import RUN_RETENTION_DAYS, ReportStore

logger = logging.getLogger("vanna.reports")

#: How often a worker looks for work. Cron's resolution is a minute, so checking
#: faster cannot make a report earlier -- it only adds queries. Checking slower
#: makes every report late by up to the interval.
TICK_SECONDS = 30

#: A run that has been 'running' for longer than this is assumed dead -- its worker
#: was killed mid-execution -- and is failed so the history stops claiming it is
#: still going. Generous, because a genuinely slow warehouse query is not a fault.
STUCK_AFTER_SECONDS = 3600


def worker_name() -> str:
    """Which process claimed a run.

    Not used for correctness -- SKIP LOCKED handles that -- but a run wedged in
    'running' is otherwise untraceable to a container.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


class ReportRunner:
    """Renders and delivers one run; drives the queue."""

    def __init__(
        self,
        *,
        store: ReportStore,
        directory: Any,
        platform: Any,
        deliver: Any,
        settings: Any,
        generation_store: Any = None,
        admin_audit: Any = None,
        agent_memory: Any = None,
    ) -> None:
        self.store = store
        self.directory = directory
        self.platform = platform
        self.deliver = deliver
        self.settings = settings
        self.generation_store = generation_store
        self.admin_audit = admin_audit
        self.agent_memory = agent_memory

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    async def user_for(self, tenant_id: str, email: str) -> Any:
        """The ``User`` a run executes as.

        Mirrors ``DirectoryUserResolver.resolve_user`` for the part that decides
        access: the membership row is read from ``tenant_users``, the role comes
        from that row, and ``_groups_for`` -- imported rather than reimplemented --
        turns it into the group memberships grants resolve against.

        Raises ``PermissionError`` if the address is not an active member. The
        caller marks the run failed; there is no fallback identity by design.
        """
        from vanna.core.user import User

        tenant = await self.directory.get_tenant(tenant_id)
        if tenant is None or not tenant["is_active"]:
            raise PermissionError(f"Workspace {tenant_id!r} is not active")

        member = await self.directory.get_member(tenant_id, email)
        if member is None:
            raise PermissionError(
                f"{email} is no longer a member of {tenant_id!r}; the schedule will "
                "not run until it names a current member."
            )
        if not member["is_active"]:
            raise PermissionError(f"Access for {email} has been disabled.")

        role = member["role"]
        # `platform_admin=False`, always. A platform admin's elevated groups are a
        # property of *who is asking*, and nobody is asking here -- granting them
        # to a background job would let a schedule read past the grants that apply
        # to the same person in a browser.
        groups = _groups_for(role, False)

        return User(
            id=email,
            email=email,
            username=member.get("full_name") or "",
            tenant_id=tenant_id,
            group_memberships=groups,
            metadata={
                "role": role,
                "platform_admin": False,
                "session_scope": "report",
                "byo_key": False,
            },
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(self, user: Any, dashboard_id: str, params: Dict[str, Any]) -> Tuple[Any, List[Any]]:
        """Load, verify and execute a dashboard as ``user``.

        The same sequence as ``routes/dashboards.py::_render``. Verification runs
        on the way out as well as on the way in because a document stored by an
        older build may not satisfy today's checks -- and a report is read by
        somebody who was not there when it was authored.
        """
        from vanna.dashboards import Dashboard, has_errors, render_dashboard, verify_dashboard

        row = await self.directory.get_dashboard(user.tenant_id, dashboard_id)
        if row is None:
            raise LookupError(f"Dashboard {dashboard_id} no longer exists")

        dashboard = Dashboard.model_validate(row["document"])

        issues = verify_dashboard(dashboard)
        if has_errors(issues):
            problems = "; ".join(str(i) for i in issues if i.severity == "error")
            raise ValueError(f"Dashboard will not render: {problems}")

        saved_sql = {
            item["id"]: item["sql"]
            for item in await self.directory.list_saved(user.tenant_id)
        }
        runtime = await self.platform.runtime_for(user.tenant_id)
        results = await render_dashboard(
            dashboard,
            registry=runtime.agent.tool_registry,
            user=user,
            agent_memory=self.agent_memory,
            saved_query_sql=saved_sql,
            # Values are matched against the dashboard's declared parameters and
            # rendered by type. `params.resolve` refuses a name the document does
            # not declare, which is what keeps a stored parameter off the
            # injection path even though it was written months ago.
            params=params,
        )
        return dashboard, results

    # ------------------------------------------------------------------
    # One run
    # ------------------------------------------------------------------

    async def execute(self, run: Dict[str, Any]) -> None:
        """Render, build the artifact, deliver, record. Never raises."""
        run_id = run["id"]
        tenant_id = run["tenant_id"]

        await self.store.mark_running(run_id)

        try:
            user = await self.user_for(tenant_id, run["run_as"])
            dashboard, results = await self.render(
                user, run["dashboard_id"], run.get("parameters") or {}
            )

            tenant = await self.directory.get_tenant(tenant_id)
            artifact, filename = await self._artifact(
                dashboard, results, user=user, tenant=tenant, fmt=run.get("format") or "html"
            )

            rows = sum(r.row_count for r in results)
            failed_tiles = [r for r in results if r.error]

            await self.deliver(
                run=run,
                dashboard=dashboard,
                results=results,
                user=user,
                tenant=tenant,
                artifact=artifact,
                filename=filename,
            )

            await self.store.finish(
                run_id,
                ok=True,
                tile_count=len(results),
                row_count=rows,
                # A run with a broken tile still succeeded *as a run* -- the other
                # eleven panels are readable and were delivered. Recording the
                # failure in `error` rather than failing the run is what keeps the
                # history honest about both facts at once.
                error=(
                    f"{len(failed_tiles)} tile(s) failed: "
                    + "; ".join(f"{r.tile_id}: {r.error}" for r in failed_tiles[:5])
                    if failed_tiles else ""
                ),
                artifact=artifact,
                artifact_filename=filename,
            )
            await self._record(run, dashboard, results, user)
            logger.info(
                "Report run %s delivered (%d tiles, %d rows) as %s",
                run_id, len(results), rows, run["run_as"],
            )

        except Exception as exc:  # noqa: BLE001 - a run must never kill the loop
            logger.warning("Report run %s failed: %s: %s", run_id, type(exc).__name__, exc)
            await self.store.finish(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _artifact(
        self, dashboard: Any, results: List[Any], *, user: Any, tenant: Any, fmt: str
    ) -> Tuple[bytes, str]:
        """The bytes that get delivered.

        ``html`` reuses ``dashboards/export.py`` unchanged -- the same
        self-contained file the Export button produces, rendered with this
        person's permissions, openable with no login and no network.
        """
        from vanna.dashboards import export_filename, export_html

        from .tenancy import describe_data_source

        if fmt == "csv":
            return _csv_zip(dashboard, results), _csv_filename(dashboard)

        html = export_html(
            dashboard,
            results,
            exported_by=user.email or user.id,
            workspace=(tenant or {}).get("name") or user.tenant_id,
            data_source=describe_data_source((tenant or {}).get("database_url")),
        )
        return html.encode("utf-8"), export_filename(dashboard)

    async def _record(self, run: Dict[str, Any], dashboard: Any, results: List[Any], user: Any) -> None:
        """Log the run where an investigation would look for it.

        Data leaving the system is exactly the event somebody asks about later, so
        a report is recorded the same way a dashboard export is
        (``routes/dashboards.py::_record_export``): the generation store gets what
        ran, the admin audit gets that it happened and who it went to.

        Never fails the run -- a delivered report that could not be logged is a
        logging problem, and failing it here would re-deliver on the next tick.
        """
        try:
            if self.generation_store is not None:
                from vanna.core.generation import GenerationStatus, SqlGeneration

                failed = [r for r in results if r.error]
                await self.generation_store.record(
                    _ToolContext(user),
                    SqlGeneration(
                        tenant_id=user.tenant_id,
                        user_id=user.email or user.id,
                        question=f"[report] {dashboard.title or 'report'}",
                        sql="\n".join(
                            f"-- tile {r.tile_id}: {r.row_count} row(s)"
                            + (f" -- FAILED: {r.error}" if r.error else "")
                            for r in results
                        )[:20_000],
                        status=GenerationStatus.VALID if not failed else GenerationStatus.INVALID,
                        row_count=sum(r.row_count for r in results),
                        metadata={
                            "kind": "report_run",
                            "run_id": run["id"],
                            "schedule_id": run.get("schedule_id"),
                            "dashboard_id": dashboard.id,
                            "tiles": len(results),
                            "failed_tiles": len(failed),
                        },
                    ),
                )

            if self.admin_audit is not None:
                await self.admin_audit.record(
                    "tenant.update",
                    actor_email=run["run_as"],
                    tenant_id=user.tenant_id,
                    target=f"report:{run.get('schedule_id') or run['id']}",
                    details={
                        "kind": "report_run",
                        "dashboard_id": dashboard.id,
                        "tiles": len(results),
                        "rows": sum(r.row_count for r in results),
                        # What left the building, and to where.
                        "channels": [c.get("kind") for c in run.get("channels") or []],
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record report run: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    async def materialise(self, db: Any) -> int:
        """Queue everything that has come due. Serialised across workers."""
        # The lock covers the materialising pass and nothing else. Holding it
        # through `drain` would serialise execution too, which is the opposite of
        # what four workers are for.
        async with _held(db, locks.KEY_REPORTS):
            queued = await self.store.materialise_due()
        if queued:
            logger.info("Queued %d report run(s)", queued)
        return queued

    async def drain(self) -> int:
        """Execute whatever this worker can claim."""
        claimed = await self.store.claim(worker_name())
        for run in claimed:
            await self.execute(run)
        return len(claimed)

    async def loop(self, db: Any) -> None:
        """Materialise, drain, sleep. Forever."""
        while True:
            try:
                await self.materialise(db)
                await self.drain()
                await self._release_stuck()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # One bad tick must not end the scheduler for the process's
                # lifetime -- that failure is silent and lasts until a restart.
                logger.warning("Report tick failed: %s: %s", type(exc).__name__, exc)

            try:
                await asyncio.sleep(TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _release_stuck(self) -> None:
        """Fail runs whose worker died mid-execution.

        Without this a killed container leaves rows in 'running' forever, and the
        history reports a report as still going months later.
        """
        from .db import SCHEMA

        await self.store.db.execute(
            f"""UPDATE {SCHEMA}.report_runs
                   SET status = 'failed',
                       finished_at = now(),
                       error = 'Worker stopped before the run finished'
                 WHERE status IN ('claimed', 'running')
                   AND claimed_at < now() - make_interval(secs => %s)""",
            (STUCK_AFTER_SECONDS,),
        )

    async def purge(self) -> int:
        return await self.store.purge_runs_older_than(RUN_RETENTION_DAYS)


class _held:
    """``async with`` over the synchronous advisory lock helper."""

    def __init__(self, db: Any, key: int) -> None:
        self.db = db
        self.key = key
        self._ctx: Optional[Any] = None

    async def __aenter__(self) -> bool:
        # `advisory_lock` is a synchronous context manager over a blocking
        # connection; entering it on the event loop would stall every request this
        # worker is serving while another worker holds it.
        self._ctx = locks.advisory_lock(self.db, self.key)
        await asyncio.to_thread(self._ctx.__enter__)
        return True

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._ctx is not None:
            await asyncio.to_thread(self._ctx.__exit__, *exc_info)
            self._ctx = None


class _ToolContext:
    """The minimum the generation store reads.

    A real ``ToolContext`` is built from a request; there is no request here.
    Rather than fabricate one and risk it drifting from the real shape, this
    carries only what ``record`` touches.
    """

    def __init__(self, user: Any) -> None:
        self.user = user
        self.tenant_id = user.tenant_id
        self.conversation_id = "report"
        self.request_id = ""


# ----------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------


def _csv_filename(dashboard: Any) -> str:
    safe = "".join(c if c.isalnum() or c in "-_ " else "-" for c in (dashboard.title or "report"))
    return f"{safe.strip().replace(' ', '-').lower() or 'report'}.zip"


def _csv_zip(dashboard: Any, results: List[Any]) -> bytes:
    """One CSV per tile, zipped.

    A zip rather than a single sheet because a dashboard's tiles have different
    columns; stacking them into one file produces a grid that is not a table.
    Text tiles have no rows and are skipped rather than written empty.
    """
    import csv
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, result in enumerate(results, start=1):
            tile = dashboard.tile(result.tile_id)
            if tile is not None and tile.kind == "text":
                continue

            label = (tile.title if tile else "") or result.tile_id
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:60]

            text = io.StringIO(newline="")
            writer = csv.writer(text)
            if result.error:
                writer.writerow(["error"])
                writer.writerow([result.error])
            else:
                writer.writerow(result.columns)
                writer.writerows(result.rows)

            # utf-8-sig: Excel opens a plain UTF-8 CSV as the local codepage and
            # mangles every non-ASCII label, which for an Arabic workspace is
            # every label.
            archive.writestr(f"{index:02d}-{safe or 'tile'}.csv",
                             text.getvalue().encode("utf-8-sig"))

        archive.writestr(
            "README.txt",
            (
                f"{dashboard.title}\n"
                f"Exported {datetime.now(timezone.utc).isoformat()}\n\n"
                "One file per tile. These rows are what the person this report "
                "runs as is permitted to see; another member may be permitted to "
                "see more, or less.\n"
            ).encode("utf-8"),
        )

    return buffer.getvalue()
