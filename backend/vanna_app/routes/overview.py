"""The operator's landing screen, and the audit trail as a file.

Two read-only surfaces, both of which existed as data long before anything showed
them.

**The overview.** Every number here was already collected -- ``generations`` has
carried per-question status, feedback, model and cost since the beginning, and
``list_tenants_with_usage`` already answered "how is each workspace doing" in a
single query. What was missing was a screen that asked. The console opened on the
review queue, so "is the platform healthy" was a question you answered by reading
tabs.

**The audit trail as CSV.** ``admin_audit`` records every privileged mutation and
nothing ever read it back. An operator investigating an incident wants the rows in
a spreadsheet, not a paginated table.

Authorisation is the same shape as ``/admin/audit`` in :mod:`.admin`, and
deliberately so: a platform admin that names no workspace gets the platform, and
everybody else is resolved through ``visible_tenant`` and refused with **404**.
The 404 is not an oversight -- see :mod:`vanna_app.authz`; a 403 confirms that the
workspace exists.

Neither route records an audit event. The convention in this codebase is that
privileged *mutations* are audited; auditing reads would bury them.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import StreamingResponse

from ..audit import ACTIONS
from ..authz import is_platform_admin, require_tenant_admin, visible_tenant
from . import Deps

logger = logging.getLogger("vanna.routes.overview")

#: Columns in the CSV export, in order. ``details`` is JSON-encoded into its cell;
#: the audit writer has already redacted anything credential-shaped by key name.
CSV_COLUMNS = (
    "created_at", "actor_email", "action", "tenant_id", "target", "actor_ip",
    "details",
)


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings

    async def _scope(request: Request, tenant_id: str) -> Tuple[Any, str]:
        """Resolve the caller and the workspace they are asking about.

        Returns ``""`` for the platform-wide view, which only a platform admin
        naming no workspace can reach. Copied in shape from ``admin_audit_log``
        rather than reimplemented: these two surfaces must agree about who can see
        across workspace boundaries, and two spellings of that rule is one too
        many.
        """
        user = await deps.caller(request)
        if tenant_id or not is_platform_admin(user, settings):
            scope = visible_tenant(user, tenant_id or None, settings)
            require_tenant_admin(user, scope, settings)
            return user, scope
        return user, ""

    def _registry() -> Optional[Any]:
        return getattr(deps.platform, "datasources", None)

    @app.get("/api/vanna/v2/admin/overview")
    async def admin_overview(
        request: Request, tenant_id: str = "", days: int = 30
    ) -> Dict[str, Any]:
        """Headline numbers, a daily series, and what just happened.

        One request for the whole tab. The pieces are independent queries against
        the control plane, so they are gathered concurrently -- serially this is
        five round trips to paint one screen.
        """
        user, scope = await _scope(request, tenant_id)
        directory = deps.require_directory()
        days = min(max(days, 1), 365)
        platform_wide = scope == ""
        privileged = is_platform_admin(user, settings)

        # Cost is platform-admin only, matching /admin/tenants/{id}/spend. A
        # workspace admin gets the key omitted rather than zero: "we do not show
        # you this" and "you spent nothing" are different answers.
        async def _spend() -> Optional[Dict[str, Any]]:
            if not privileged:
                return None
            if platform_wide:
                return await directory.platform_spend(days=days)
            return await directory.spend(scope, days=days)

        async def _sources() -> Optional[List[Dict[str, Any]]]:
            registry = _registry()
            if not privileged or registry is None:
                return None
            # Platform-wide, the useful question is "is anything broken", not a
            # roll-call of every source on the platform.
            if platform_wide:
                return await registry.unhealthy_sources()
            return await registry.list_sources(scope)

        usage_task = (
            directory.list_tenants_with_usage(days=days)
            if platform_wide
            else directory.tenant_usage(scope, days=days)
        )

        usage, series, spend, sources, recent = await asyncio.gather(
            usage_task,
            directory.activity_series(scope, days=days),
            _spend(),
            _sources(),
            deps.admin_audit.recent(tenant_id=scope, limit=8),
        )

        payload: Dict[str, Any] = {
            "scope": scope,
            "window_days": days,
            "series": series,
            "recent": recent,
            # Served rather than hardcoded in the browser, so the filter cannot
            # drift from the vocabulary the writer actually accepts.
            "actions": list(ACTIONS),
        }

        if platform_wide:
            payload["kpis"] = _platform_kpis(usage)
            payload["workspaces"] = usage
        else:
            payload["kpis"] = _tenant_kpis(usage)

        if spend is not None:
            payload["kpis"]["cost_usd"] = spend["cost_usd"]
            payload["spend"] = spend
        if sources is not None:
            payload["data_sources"] = sources

        return payload

    @app.get("/api/vanna/v2/admin/audit.csv")
    async def admin_audit_csv(
        request: Request,
        tenant_id: str = "",
        action: str = "",
        actor_email: str = "",
        limit: int = 500,
    ) -> StreamingResponse:
        """The admin audit trail as a file.

        Streamed rather than assembled in memory: 500 rows of JSON details is not
        large, but the shape is right if the store's cap ever rises.
        """
        _user, scope = await _scope(request, tenant_id)
        events = await deps.admin_audit.recent(
            tenant_id=scope,
            action=action,
            actor_email=actor_email,
            limit=min(max(limit, 1), 500),
        )

        def _rows() -> Iterator[str]:
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(CSV_COLUMNS)
            yield buffer.getvalue()
            for event in events:
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerow(
                    [
                        json.dumps(event.get(column), default=str)
                        if column == "details"
                        else _cell(event.get(column))
                        for column in CSV_COLUMNS
                    ]
                )
                yield buffer.getvalue()

        name = "audit-" + (scope or "platform") + ".csv"
        return StreamingResponse(
            _rows(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="' + name + '"'},
        )


# ----------------------------------------------------------------------
# Shaping
# ----------------------------------------------------------------------


def _cell(value: Any) -> Any:
    """A missing value is an empty cell, not the string ``None``."""
    return "" if value is None else value


def _rate(succeeded: int, questions: int) -> Optional[float]:
    """Success rate, or ``None`` when nothing was asked.

    Not zero. A window with no questions has no success rate, and rendering one as
    "0%" reads as a platform that is failing every request.
    """
    return round(succeeded / questions, 4) if questions else None


def _tenant_kpis(usage: Dict[str, Any]) -> Dict[str, Any]:
    questions = int(usage.get("questions") or 0)
    succeeded = int(usage.get("succeeded") or 0)
    return {
        "workspaces": 1,
        "members": int(usage.get("members") or 0),
        "questions": questions,
        "succeeded": succeeded,
        "success_rate": _rate(succeeded, questions),
        "active_users": int(usage.get("active_users") or 0),
        "liked": int(usage.get("liked") or 0),
        "disliked": int(usage.get("disliked") or 0),
        "last_activity": usage.get("last_activity"),
    }


def _platform_kpis(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Totals across every workspace.

    ``list_tenants_with_usage`` nests the counts under ``usage`` rather than
    putting them beside ``id`` and ``name`` -- read them from the wrong level and
    every total is a confident zero, which is what a platform with no traffic
    also looks like.

    ``active_users`` is a sum of per-workspace distinct counts, so somebody who
    works in two workspaces counts twice. That is the honest reading of "active
    users per workspace, added up", and it is what the per-workspace table below
    it shows; a platform-wide distinct count would agree with no row on the page.
    """
    usages = [row.get("usage") or {} for row in rows]
    totals = {
        key: sum(int(usage.get(key) or 0) for usage in usages)
        for key in ("questions", "succeeded", "liked", "disliked", "members",
                    "active_users")
    }
    stamps: List[Any] = [
        usage["last_activity"] for usage in usages if usage.get("last_activity")
    ]
    return {
        "workspaces": len(rows),
        "active_workspaces": sum(1 for row in rows if row.get("is_active")),
        **totals,
        "success_rate": _rate(totals["succeeded"], totals["questions"]),
        "last_activity": max(stamps) if stamps else None,
    }
