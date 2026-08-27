"""The HTTP surface, one module per area.

This was a single 1,900-line file with a ten-argument registration function. The
split is by *area of the product* rather than by HTTP verb, so a change to how
billing works touches one file and a reviewer reading ``admin.py`` sees every
privileged operation in one place.

Authorisation model
-------------------

Two tiers, because "admin" means two different things in a multi-tenant system:

* **Platform admin** -- an address in ``VANNA_ADMIN_EMAILS``. Creates and deletes
  workspaces, binds datasources, sets plans, administers any workspace.
* **Tenant admin** -- ``role = 'admin'`` on a ``tenant_users`` row. Manages members,
  starters and knowledge *for their own workspace only*.

Every write route names the tier it needs, from :mod:`vanna_app.authz`. Nothing
derives permission from what the browser sent beyond the identity the resolver
produced, and **no route trusts a ``tenant_id`` in the path without checking it
against the caller** -- ``authz.visible_tenant`` is the one way that check is
spelled.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException, Request

logger = logging.getLogger("vanna.routes")


class RuntimeProvider(Protocol):
    """``Platform.runtime_for`` -- resolves a workspace to its agent and runner."""

    async def __call__(self, tenant_id: str, *, invalidate: bool = False) -> Any: ...


@dataclass
class Deps:
    """Everything the routes need, assembled once in ``wiring``.

    A container rather than ten positional arguments: adding a dependency used to
    mean editing a signature, three call sites and every test that built one.
    """

    settings: Any
    platform: Any
    user_resolver: Any
    directory: Any = None
    accounts: Any = None
    billing: Any = None
    generation_store: Any = None
    conversation_store: Any = None
    counters: Any = None
    admin_audit: Any = None
    agent_audit: Any = None
    mailer: Any = None
    login_throttle: Any = None
    oidc: Any = None
    #: ReportStore. Absent when there is no control plane (demo mode), which is
    #: why `routes/reports.py` answers 404 rather than 500 when it is None.
    reports: Any = None
    #: Lineage is derived from rows the system already keeps -- see
    #: `vanna_app/lineage.py` -- so this is a service, not a store.
    lineage: Any = None
    compliance: Any = None

    @property
    def runtime_for(self) -> RuntimeProvider:
        return self.platform.runtime_for

    async def runtime_for_request(self, user: Any, request: Request) -> Any:
        """The runtime for the database this request is about.

        A workspace can register several databases, and a caller says which with
        the ``X-Data-Source-Id`` header. It is a *preference*, exactly as
        ``X-Tenant-Id`` is: ``Platform.resolve_source`` checks it against what the
        workspace has registered, and an id that is not there is a 404 rather than
        a quiet fall back to the default.

        Every endpoint that reads per-database state needs this. Resolving the
        default instead -- which is what they all did -- meant the schema screen,
        the cube list and the prompt preview described one database while the
        chat answered from another, with no indication anything was wrong.
        """
        from ..datasources import UnknownDataSource

        requested = request.headers.get("x-data-source-id")
        try:
            return await self.platform.runtime_for(
                user.tenant_id, data_source_id=requested
            )
        except UnknownDataSource as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @property
    def agent_memory(self) -> Any:
        return self.platform.memory

    # -- guards --------------------------------------------------------

    def require_directory(self) -> Any:
        """The directory, or a 503 that says what is missing.

        503 rather than 500: "this feature needs a database" is actionable,
        "internal error" is not.
        """
        if self.directory is None:
            raise HTTPException(
                status_code=503,
                detail="No control-plane database configured. Set VANNA_APP_DATABASE_URL.",
            )
        return self.directory

    def require_accounts(self) -> Any:
        if self.accounts is None:
            raise HTTPException(
                status_code=503, detail="Authentication needs a control-plane database."
            )
        return self.accounts

    def require_billing(self) -> Any:
        if self.billing is None:
            raise HTTPException(
                status_code=503, detail="Billing needs a control-plane database."
            )
        return self.billing

    def require_conversations(self) -> Any:
        if self.conversation_store is None:
            raise HTTPException(
                status_code=503,
                detail="Conversation history needs a control-plane database.",
            )
        return self.conversation_store

    # -- identity ------------------------------------------------------

    async def caller(self, request: Request, *, full_session: bool = True) -> Any:
        """Resolve the caller, or refuse.

        ``full_session=False`` is for the two endpoints an account under a temporary
        password must still be able to reach.
        """
        from vanna.core.user import RequestContext

        from ..authz import require_full_session
        from ..identity import AuthenticationRequired

        try:
            user = await self.user_resolver.resolve_user(
                RequestContext(
                    headers=dict(request.headers),
                    cookies=dict(request.cookies),
                    metadata={},
                )
            )
        except HTTPException:
            raise
        except AuthenticationRequired as exc:
            # 401: no credential. Distinct from 403, which means "credential
            # accepted, and it is not enough".
            raise HTTPException(status_code=401, detail=str(exc))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except Exception as exc:
            logger.warning("Identity resolution failed: %s", exc)
            raise HTTPException(status_code=503, detail="Could not resolve identity")

        if full_session:
            require_full_session(user)
        return user

    async def tool_context(
        self, user: Any, *, conversation_id: str = "portal", data_source: str = ""
    ) -> Any:
        """A ``ToolContext`` for the caller, for catalog and runner calls.

        ``data_source`` travels in ``metadata`` because ``ToolContext`` has no
        field for it. The catalog store reads it there when a record does not
        name its own: a scan whose tables carry no data source was previously
        written under the literal string ``"default"``, while every reader
        resolves the workspace's real source id -- so the rows existed and
        nothing could find them. Annotating a table answered "not in this
        workspace's catalog" for a table plainly listed on the screen.
        """
        from vanna.core.tool import ToolContext

        return ToolContext(
            user=user,
            conversation_id=conversation_id,
            request_id=str(uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=self.agent_memory,
            metadata={"data_source_id": data_source} if data_source else {},
        )

    def client_ip(self, request: Request) -> str:
        from ..net import request_ip

        return request_ip(request, self.settings.trusted_proxies)


def register_all(app: Any, deps: Deps) -> None:
    """Register every route module."""
    from . import (
        admin,
        auth,
        catalog,
        config,
        dashboards,
        data,
        domains,
        governance,
        grants,
        instructions,
        memories,
        overview,
        reports,
        workspace,
        writes,
    )

    auth.register(app, deps)
    workspace.register(app, deps)
    data.register(app, deps)
    dashboards.register(app, deps)
    # After dashboards: a report is a schedule over a dashboard, and reading this
    # file top to bottom should introduce the thing before the thing that points
    # at it.
    reports.register(app, deps)
    governance.register(app, deps)
    writes.register(app, deps)
    grants.register(app, deps)
    domains.register(app, deps)
    catalog.register(app, deps)
    instructions.register(app, deps)
    admin.register(app, deps)
    memories.register(app, deps)
    overview.register(app, deps)
    config.register(app, deps)
