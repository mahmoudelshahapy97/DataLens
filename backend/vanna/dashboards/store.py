"""Where dashboards live."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..core.errors import ErrorCode, ErrorPhase, VannaError
from .models import Dashboard
from .verify import has_errors, verify_dashboard

logger = logging.getLogger(__name__)


class DashboardStore(ABC):
    """Tenant-scoped dashboard storage.

    Same contract as every other store here: the tenant comes from the caller's
    context and is never taken from the payload, so a dashboard cannot be
    written into a workspace the author does not belong to.
    """

    @abstractmethod
    async def list(self, tenant_id: str) -> List[Dashboard]: ...

    @abstractmethod
    async def get(self, tenant_id: str, dashboard_id: str) -> Optional[Dashboard]: ...

    @abstractmethod
    async def save(self, dashboard: Dashboard) -> Dashboard: ...

    @abstractmethod
    async def delete(self, tenant_id: str, dashboard_id: str) -> bool: ...


def validated(dashboard: Dashboard) -> Dashboard:
    """Verify before storing, and refuse on an error.

    Storing a dashboard known to be broken means the failure surfaces later, to
    a reader rather than an author, against something that looks saved and fine.
    """
    issues = verify_dashboard(dashboard)
    if has_errors(issues):
        raise VannaError(
            ErrorCode.INVALID_REQUEST,
            "; ".join(str(i) for i in issues if i.severity == "error"),
            phase=ErrorPhase.VISUALIZATION,
            hint="Fix the tiles listed and save again.",
        )
    dashboard.updated_at = datetime.now(timezone.utc)
    return dashboard


class LocalDashboardStore(DashboardStore):
    """JSON-file-backed store, for single-process deployments.

    The reference stack uses the Postgres control plane instead; this exists so
    the library is usable without one.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else None
        self._by_tenant: Dict[str, Dict[str, Dashboard]] = {}
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("Could not read dashboards from %s: %s", self.path, exc)
            return

        for entry in raw.get("dashboards", []):
            try:
                dashboard = Dashboard.model_validate(entry)
            except Exception as exc:
                logger.warning("Skipping malformed dashboard: %s", exc)
                continue
            self._by_tenant.setdefault(dashboard.tenant_id, {})[dashboard.id] = dashboard

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            "dashboards": [
                d.to_json_dict()
                for bucket in self._by_tenant.values()
                for d in bucket.values()
            ]
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temp-then-replace: an interrupted write must not truncate the file.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def list(self, tenant_id: str) -> List[Dashboard]:
        return sorted(
            self._by_tenant.get(tenant_id, {}).values(), key=lambda d: d.title.lower()
        )

    async def get(self, tenant_id: str, dashboard_id: str) -> Optional[Dashboard]:
        return self._by_tenant.get(tenant_id, {}).get(dashboard_id)

    async def save(self, dashboard: Dashboard) -> Dashboard:
        dashboard = validated(dashboard)
        self._by_tenant.setdefault(dashboard.tenant_id, {})[dashboard.id] = dashboard
        self._save()
        return dashboard

    async def delete(self, tenant_id: str, dashboard_id: str) -> bool:
        bucket = self._by_tenant.get(tenant_id, {})
        if dashboard_id not in bucket:
            return False
        del bucket[dashboard_id]
        self._save()
        return True
