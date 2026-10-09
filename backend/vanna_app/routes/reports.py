"""Reports: schedule, run, download, and the notification bell.

A report is a dashboard that declares parameters, plus a schedule and a delivery
list. There is no report document -- see the header of
``database/migrations/0015_reports.sql`` for why.

The authorisation rules here are narrower than they look, and all three come from
the same fact: **a report executes as a named member, so creating one decides
whose permissions a set of numbers is produced with.**

* A viewer may not create or edit a schedule (``forbid_viewer``), for the same
  reason a viewer may not author a dashboard.
* Only a workspace admin may set ``run_as`` to *somebody else*. An analyst may
  schedule a report that runs as themselves and nobody else, because a schedule
  running as a colleague is a request for that colleague's permissions.
* ``run_as`` must be a current member at write time -- and again at execution
  time, which ``report_runner`` rechecks. Both, because the membership can change
  in between and only one of the two checks is present when it does.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from ..authz import forbid_viewer
from ..cron import CronError, validate as validate_cron
from ..report_delivery import DeliveryRefused, check_webhook_url
from . import Deps

logger = logging.getLogger("vanna.routes.reports")

_CHANNEL_KINDS = ("email", "webhook", "inapp")
_FORMATS = ("html", "csv")


def register(app: Any, deps: Deps) -> None:

    def _store() -> Any:
        store = getattr(deps, "reports", None)
        if store is None:
            # The control plane is optional in demo mode, and reports need it.
            # 404 rather than 500: the feature is genuinely not here.
            raise HTTPException(status_code=404, detail="Not found")
        return store

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def _check_run_as(user: Any, requested: Optional[str]) -> str:
        """Resolve and authorise the identity a schedule will execute as."""
        caller = (user.email or user.id or "").lower()
        target = (requested or caller).strip().lower()

        if target != caller:
            # Scheduling as a colleague means asking for their permissions. Only a
            # workspace admin may do that, and it is recorded.
            if (user.metadata or {}).get("role") != "admin" and not (
                user.metadata or {}
            ).get("platform_admin"):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Only a workspace admin can schedule a report that runs as "
                        "somebody else. Leave it blank to run as yourself."
                    ),
                )

        member = await deps.require_directory().get_member(user.tenant_id, target)
        if member is None or not member["is_active"]:
            raise HTTPException(
                status_code=400,
                detail=f"{target} is not an active member of this workspace.",
            )
        return target

    def _check_channels(payload: Any, *, allowed_hosts: Any) -> List[Dict[str, Any]]:
        """Normalise and refuse the delivery list.

        Webhook destinations are checked *here* as well as at delivery time. The
        write-time check is what gives the person typing the URL an error message;
        the delivery-time one is what holds when DNS changes underneath it.
        """
        if not isinstance(payload, list):
            raise HTTPException(status_code=400, detail="channels must be a list.")

        channels: List[Dict[str, Any]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail="Each channel must be an object.")
            kind = str(entry.get("kind") or "").strip()
            target = str(entry.get("target") or "").strip()

            if kind not in _CHANNEL_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown channel {kind!r}. Use one of: {', '.join(_CHANNEL_KINDS)}.",
                )
            if kind == "webhook":
                try:
                    target = check_webhook_url(target, allowed_hosts=allowed_hosts)
                except DeliveryRefused as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
            if kind == "email" and not target:
                raise HTTPException(status_code=400, detail="An email channel needs recipients.")

            channels.append({"kind": kind, "target": target})
        return channels

    async def _check_dashboard(user: Any, dashboard_id: str) -> Any:
        """The dashboard, or 404. Also the tenant scoping for this whole module."""
        row = await deps.require_directory().get_dashboard(user.tenant_id, dashboard_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        from vanna.dashboards import Dashboard

        return Dashboard.model_validate(row["document"])

    def _check_parameters(dashboard: Any, values: Any) -> Dict[str, Any]:
        """Bound values must name parameters the dashboard actually declares.

        Checked at write time so a typo is an error message rather than a report
        that renders at defaults forever without saying so. `params.resolve`
        refuses an undeclared name at render time too -- this is the friendly half
        of that, not a replacement for it.
        """
        if values in (None, ""):
            return {}
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="parameters must be an object.")

        declared = {p.name for p in dashboard.parameters}
        unknown = sorted(set(values) - declared)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This dashboard does not declare {', '.join(unknown)}. "
                    + (f"It declares: {', '.join(sorted(declared))}." if declared
                       else "It declares no parameters.")
                ),
            )
        return dict(values)

    def _check_cron(expression: Any, timezone_name: Any) -> None:
        try:
            validate_cron(str(expression or ""), str(timezone_name or "UTC"))
        except CronError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/reports")
    async def list_reports(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        schedules = await _store().list_schedules(user.tenant_id)

        # The title is what a person recognises; the id is not. Resolved here
        # rather than joined in SQL because a dashboard is a jsonb document and
        # digging the title out in the query is worse than one extra read.
        titles = {
            row["id"]: (row["document"] or {}).get("title") or row["title"]
            for row in await deps.require_directory().list_dashboards(user.tenant_id)
        }
        from ..cron import parse as parse_cron

        for schedule in schedules:
            schedule["dashboard_title"] = titles.get(schedule["dashboard_id"], "")
            try:
                schedule["schedule_label"] = parse_cron(schedule["cron"]).describe()
            except CronError:
                # A stored expression that no longer parses still has to render in
                # the list -- that is where somebody would notice it is broken.
                schedule["schedule_label"] = schedule["cron"]
        return {"reports": schedules}

    @app.post("/api/vanna/v2/reports")
    async def create_report(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        forbid_viewer(user, "schedule reports")

        dashboard = await _check_dashboard(user, str(payload.get("dashboard_id") or ""))
        _check_cron(payload.get("cron"), payload.get("timezone"))

        fmt = str(payload.get("format") or "html")
        if fmt not in _FORMATS:
            raise HTTPException(status_code=400, detail=f"format must be one of: {', '.join(_FORMATS)}")

        run_as = await _check_run_as(user, payload.get("run_as"))
        channels = _check_channels(
            payload.get("channels") or [],
            allowed_hosts=getattr(deps.settings, "webhook_allowed_hosts", ()),
        )
        parameters = _check_parameters(dashboard, payload.get("parameters"))

        schedule = await _store().create_schedule(
            user.tenant_id,
            dashboard_id=dashboard.id,
            name=str(payload.get("name") or dashboard.title or "Report")[:200],
            cron=str(payload["cron"]),
            timezone_name=str(payload.get("timezone") or "UTC"),
            run_as=run_as,
            channels=channels,
            parameters=parameters,
            fmt=fmt,
            created_by=user.email or user.id,
            is_active=bool(payload.get("is_active", True)),
        )

        await _audit(user, "create", schedule)
        return {"report": schedule}

    @app.patch("/api/vanna/v2/reports/{schedule_id}")
    async def update_report(
        schedule_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        forbid_viewer(user, "edit report schedules")

        store = _store()
        current = await store.get_schedule(user.tenant_id, schedule_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Not found")

        changes: Dict[str, Any] = {}

        if "dashboard_id" in payload:
            changes["dashboard_id"] = (
                await _check_dashboard(user, str(payload["dashboard_id"]))
            ).id
        if "name" in payload:
            changes["name"] = str(payload["name"])[:200]
        if "cron" in payload or "timezone" in payload:
            cron = payload.get("cron", current["cron"])
            zone = payload.get("timezone", current["timezone"])
            _check_cron(cron, zone)
            changes["cron"] = str(cron)
            changes["timezone"] = str(zone)
        if "format" in payload:
            if payload["format"] not in _FORMATS:
                raise HTTPException(status_code=400, detail="Unknown format.")
            changes["format"] = payload["format"]
        if "run_as" in payload:
            changes["run_as"] = await _check_run_as(user, payload["run_as"])
        if "channels" in payload:
            changes["channels"] = _check_channels(
                payload["channels"],
                allowed_hosts=getattr(deps.settings, "webhook_allowed_hosts", ()),
            )
        if "parameters" in payload:
            dashboard = await _check_dashboard(
                user, changes.get("dashboard_id", current["dashboard_id"])
            )
            changes["parameters"] = _check_parameters(dashboard, payload["parameters"])
        if "is_active" in payload:
            changes["is_active"] = bool(payload["is_active"])

        updated = await store.update_schedule(user.tenant_id, schedule_id, changes)
        if updated is None:
            raise HTTPException(status_code=404, detail="Not found")

        await _audit(user, "update", updated)
        return {"report": updated}

    @app.delete("/api/vanna/v2/reports/{schedule_id}")
    async def delete_report(schedule_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        forbid_viewer(user, "delete report schedules")

        schedule = await _store().get_schedule(user.tenant_id, schedule_id)
        if schedule is None or not await _store().delete_schedule(user.tenant_id, schedule_id):
            raise HTTPException(status_code=404, detail="Not found")

        await _audit(user, "delete", schedule)
        return {"deleted": True}

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    @app.post("/api/vanna/v2/reports/{schedule_id}/run")
    async def run_now(schedule_id: str, request: Request) -> Dict[str, Any]:
        """Queue a run immediately.

        Queued rather than executed inline. A dashboard with twelve tiles against
        a slow warehouse takes longer than any sensible HTTP timeout, and a
        request that dies half way through would leave the run with no record of
        having happened.

        It still runs as the schedule's ``run_as``, not as the person pressing the
        button -- otherwise "Run now" would be a way to see somebody else's view
        of the data, which is precisely what the identity rules exist to prevent.
        """
        user = await deps.caller(request)
        forbid_viewer(user, "run reports")

        store = _store()
        schedule = await store.get_schedule(user.tenant_id, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="Not found")

        run = await store.enqueue_now(
            user.tenant_id,
            dashboard_id=schedule["dashboard_id"],
            run_as=schedule["run_as"],
            parameters=schedule["parameters"],
            channels=schedule["channels"],
            fmt=schedule["format"],
            requested_by=user.email or user.id,
            schedule_id=schedule["id"],
        )
        return {"run": run}

    @app.get("/api/vanna/v2/reports/{schedule_id}/runs")
    async def list_runs(schedule_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        store = _store()
        if await store.get_schedule(user.tenant_id, schedule_id) is None:
            raise HTTPException(status_code=404, detail="Not found")
        return {"runs": await store.list_runs(user.tenant_id, schedule_id=schedule_id)}

    @app.get("/api/vanna/v2/reports/runs/{run_id}/artifact")
    async def download_artifact(run_id: str, request: Request) -> Any:
        """The stored file for one run.

        Note what is *not* checked here: whether the caller could have produced
        these numbers themselves. They could not, necessarily -- the artifact was
        rendered as ``run_as``, who may see more than this caller does.

        That is the deliberate trade a report makes, and it is the same one a
        mailed attachment makes: whoever the schedule delivers to receives the
        ``run_as`` view. Scoping the download to workspace membership matches the
        email channel; scoping it more tightly would mean a recipient could read
        the report in their inbox but not in the app.
        """
        from fastapi.responses import Response

        user = await deps.caller(request)
        artifact = await _store().artifact(user.tenant_id, run_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Not found")

        media = "text/html; charset=utf-8" if artifact["format"] == "html" else "application/zip"
        return Response(
            content=artifact["bytes"],
            media_type=media,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact["filename"]}"',
                # Never cached by anything in between: this is one person's view
                # of the data and a shared cache would hand it to the next reader.
                "Cache-Control": "no-store, private",
            },
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/notifications")
    async def list_notifications(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        return await _store().list_notifications(user.tenant_id, user.email or user.id)

    @app.post("/api/vanna/v2/notifications/{notification_id}/read")
    async def mark_read(notification_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        ok = await _store().mark_read(user.tenant_id, user.email or user.id, notification_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Not found")
        return {"read": True}

    @app.post("/api/vanna/v2/notifications/read-all")
    async def mark_all_read(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        return {
            "read": await _store().mark_all_read(user.tenant_id, user.email or user.id)
        }

    # ------------------------------------------------------------------
    # Metadata for the schedule editor
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/reports/meta")
    async def report_meta(request: Request) -> Dict[str, Any]:
        """What the schedule form needs to offer.

        Served rather than hard-coded in the frontend so a preset the backend
        would refuse cannot be presented, and so the timezone list has one owner.
        """
        await deps.caller(request)
        from ..cron import PRESETS, known_timezones

        return {
            "presets": [{"cron": cron, "label": label} for cron, label in PRESETS],
            "timezones": known_timezones(),
            "formats": list(_FORMATS),
            "channels": list(_CHANNEL_KINDS),
            "allow_external_recipients": bool(
                getattr(deps.settings, "report_allow_external_recipients", False)
            ),
        }

    async def _audit(user: Any, action: str, schedule: Dict[str, Any]) -> None:
        """Record a schedule change.

        Worth auditing in its own right: a schedule decides whose permissions
        produce a set of numbers and where those numbers go, so changing one is a
        change to who can see what.
        """
        if deps.admin_audit is None:
            return
        try:
            await deps.admin_audit.record(
                "tenant.update",
                actor_email=user.email or user.id,
                tenant_id=user.tenant_id,
                target=f"report:{schedule['id']}",
                details={
                    "kind": f"report.{action}",
                    "name": schedule.get("name"),
                    "run_as": schedule.get("run_as"),
                    "cron": schedule.get("cron"),
                    "channels": [c.get("kind") for c in schedule.get("channels") or []],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record report %s: %s", action, type(exc).__name__)
