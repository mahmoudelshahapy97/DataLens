"""Who is calling, which workspace they are in, and whose LLM key pays for it.

The single most important file in the deployment, because every access decision
downstream trusts the ``User`` it returns.

Authentication order: **session cookie, then API token, then -- only if a deployment
has deliberately said so -- a header.** The header path is what the original stack
ran on, and it meant anyone who could reach the API could be anyone. It now requires
``VANNA_TRUST_HEADERS=true``, which ``config.validate`` refuses in multi-tenant mode.

**There is no fallback identity.** The original ended with ``return
"demo@example.com"`` whenever no credential was present and no account existed --
and, because ``build_app_database`` swallowed connection failures and returned
``None``, that path was reached whenever the control plane was merely *unreachable*.
A database blip therefore turned the API into an unauthenticated one. Anonymous
access is now a mode somebody chooses (``VANNA_ALLOW_ANONYMOUS``, or demo), never
something a failure falls into.

Authorisation is separate from authentication and comes from the directory, not the
request: the address must be an active member of the workspace it claims, and the
role that decides what it can do is read from the ``tenant_users`` row.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Dict, List, Optional

from .observability import bind_identity

logger = logging.getLogger("vanna.identity")

#: Providers a caller may name in ``X-LLM-Provider``. An allow-list rather than free
#: text: the value selects which client class is constructed, and "whatever the
#: header says" is not a decision to hand to a request.
BYO_PROVIDERS = ("openai", "anthropic")


def _groups_for(role: str, platform_admin: bool) -> List[str]:
    """The group memberships a caller carries, from their workspace role.

    The role is a group, not just a metadata field. That is what makes a grant
    written for ``analyst`` or ``viewer`` match anybody: grants resolve against
    ``group_memberships``, and this list used to be ``["user"]`` for everyone who
    was not an admin, so every such grant was stored happily and matched nobody.

    ``group_memberships`` is read by more than grants, and each consumer was
    checked before this changed:

    * **Write tools** are registered for ``["admin"]``, so an analyst still
      cannot reach them.
    * **Dashboard tools** are registered for ``["admin", "analyst"]``. An analyst
      gains them here -- which is what the comment beside that registration
      already claims ("Viewers may read a dashboard but not author one") and what
      ``["user"]`` never actually delivered.
    * **Quota and rate-limit exemptions** are constructed with
      ``exempt_groups=()``, so nothing is exempt either way.
    * **Row-level rules** can name a group in a semantic manifest. None shipped
      here does; a deployment with its own manifest should check before upgrading.
    * **Group-scoped instructions** naming a role now apply, which is what that
      scope means.

    The role always comes from the ``tenant_users`` row, never from the request.
    """
    groups = ["user"]
    if role and role not in groups:
        groups.append(role)
    if platform_admin and "admin" not in groups:
        groups.append("admin")
    return groups


class AuthenticationRequired(PermissionError):
    """No usable credential was presented."""


def build_user_resolver(settings: Any, directory: Any, accounts: Any = None) -> Any:
    """The ``UserResolver`` every route and the agent share.

    One instance, used by the chat routes, the portal routes and the agent itself,
    so identity and tenant scoping cannot drift between them.
    """
    from vanna.core.user import RequestContext, User, UserResolver

    class DirectoryUserResolver(UserResolver):

        # -- authentication --------------------------------------------

        async def _authenticate(self, headers: Dict[str, str], cookies: Dict[str, str]) -> tuple:
            """Establish who is calling. Returns ``(email, session_scope)``.

            Raises ``AuthenticationRequired`` when nothing valid was presented --
            never a default identity.
            """
            if accounts is not None:
                token = cookies.get(settings.session_cookie, "")
                if token:
                    row = await accounts.session_user(token)
                    if row is not None:
                        return row["email"], (row.get("session_scope") or "full")

                authorization = headers.get("authorization", "")
                if authorization.lower().startswith("bearer "):
                    row = await accounts.token_user(authorization[7:].strip())
                    if row is not None:
                        # An API token is never restricted to password changes:
                        # tokens are issued by an authenticated session, and an
                        # account under a temporary password has none to issue with.
                        return row["email"], "full"

            claimed = (headers.get("x-user-email") or "").strip().lower()

            if settings.trust_headers and claimed:
                # Explicitly enabled. The gateway in front of us is the thing being
                # trusted, and the config validator refuses this in multi-tenant.
                logger.debug("Accepting header identity %s (VANNA_TRUST_HEADERS)", claimed)
                return claimed, "full"

            if settings.allow_anonymous or (settings.is_demo and accounts is None):
                # A deployment that has opted out of authentication entirely. The
                # claimed address is a label, not a credential, and is treated as
                # one -- but it still has to look like an address so it cannot be
                # used to smuggle something into a log line or a file path.
                if claimed and "@" in claimed and len(claimed) < 200:
                    return claimed, "full"
                return "anonymous@localhost", "full"

            if accounts is None:
                raise AuthenticationRequired(
                    "This deployment has no control plane, so nobody can sign in. "
                    "Configure VANNA_APP_DATABASE_URL."
                )

            raise AuthenticationRequired(
                "Header authentication is disabled. Sign in, or use an API token."
                if claimed
                else "Sign in to continue."
            )

        # -- resolution ------------------------------------------------

        async def resolve_user(self, request_context: RequestContext) -> User:
            headers = getattr(request_context, "headers", {}) or {}
            lowered = {k.lower(): v for k, v in headers.items()}
            cookies = getattr(request_context, "cookies", {}) or {}

            # Take the caller's API key *out* of the context before it goes any
            # further. Routes build this dict with `headers=dict(request.headers)`
            # and the context is then passed around freely -- so leaving a
            # third-party secret in it means the first person to log
            # request_context.headers while debugging leaks it. `lowered` above is a
            # copy and keeps the value for the one caller that needs it.
            for name in [k for k in list(headers) if k.lower() == "x-llm-key"]:
                headers.pop(name, None)

            email, session_scope = await self._authenticate(lowered, cookies)
            tenant = (lowered.get("x-tenant-id") or settings.default_tenant).strip().lower()

            from .authz import is_platform_admin as _is_platform_admin

            # A stand-in carrying only what the check reads, because the real User
            # does not exist yet.
            probe = type("_Probe", (), {"email": email, "id": email})()
            platform_admin = _is_platform_admin(probe, settings)

            if directory is None:
                # No control plane: single-workspace behaviour.
                groups = _groups_for(
                    "admin" if platform_admin else "analyst", platform_admin
                )
                bind_identity(tenant, email)
                return User(
                    id=email,
                    email=email,
                    tenant_id=tenant,
                    group_memberships=groups,
                    metadata={
                        "role": "admin" if platform_admin else "analyst",
                        "platform_admin": platform_admin,
                        "session_scope": session_scope,
                        "byo_key": self._byo(lowered, {}),
                    },
                )

            if await directory.count_tenants() == 0:
                # Bootstrap: somebody has to create the first workspace. Reachable
                # only by an authenticated caller, so this is not the open door the
                # original version's equivalent was.
                bind_identity(tenant, email)
                return User(
                    id=email,
                    email=email,
                    tenant_id=tenant,
                    group_memberships=["user", "admin"],
                    metadata={
                        "role": "admin",
                        "platform_admin": platform_admin,
                        "session_scope": session_scope,
                        "bootstrap": True,
                        "byo_key": False,
                    },
                )

            row = await directory.get_tenant(tenant)
            if row is None or not row["is_active"]:
                if not platform_admin:
                    raise PermissionError(f"No active workspace named {tenant!r}")
                member = None
            else:
                member = await directory.get_member(tenant, email)

            if member is None and not platform_admin:
                raise PermissionError(
                    f"{email} is not a member of {tenant!r}. Ask an administrator of "
                    "that workspace for access."
                )
            if member is not None and not member["is_active"]:
                raise PermissionError(f"Access for {email} has been disabled.")

            role = member["role"] if member else "admin"
            groups = _groups_for(role, platform_admin)

            bind_identity(tenant, email)
            return User(
                id=email,
                email=email,
                username=(member or {}).get("full_name") or "",
                tenant_id=tenant,
                group_memberships=groups,
                metadata={
                    # Carried so routes can tell analyst from viewer without a
                    # second directory round trip. Read from the row, never the
                    # request.
                    "role": role,
                    "platform_admin": platform_admin,
                    "session_scope": session_scope,
                    "byo_key": self._byo(lowered, row or {}),
                },
            )

        # -- personal LLM keys -----------------------------------------

        @staticmethod
        def _byo(lowered: Dict[str, str], tenant_row: Dict[str, Any]) -> bool:
            """Is this request being answered on the caller's own LLM key?

            Decided here because this is where the workspace row is already in hand,
            and the workspace gets the final say: a tenant with ``allow_byo_key``
            off has the header *ignored*, not merely the form hidden.

            Two signals, because this method runs **twice per request**: once in the
            dispatching chat handler, and again inside ``Agent._send_message``. The
            first call sees the header; by the second it has been stripped above, so
            the header alone would report False exactly when the quota hook consults
            it -- and the caller would be charged for a question answered on their
            own key. The already-installed override is the durable signal for the
            second pass.

            Only the *fact* is carried. The key never enters ``User.metadata``, which
            is persisted into conversations and audit records, where a secret would
            outlive the request by months.
            """
            if not tenant_row.get("allow_byo_key", True):
                return False
            if (lowered.get("x-llm-key") or "").strip():
                return True
            from vanna.core.llm import current_llm_service

            return current_llm_service() is not None

    return DirectoryUserResolver()


# ----------------------------------------------------------------------
# Per-request LLM services
# ----------------------------------------------------------------------


def build_byo_llm_service(headers: Dict[str, str]) -> Optional[Any]:
    """Build an LLM service from a caller's own key, or return None.

    The key arrives per request and is never stored: no column, no cache, no log
    line. It exists for the life of the client object built here, which lives for
    the life of the request.

    Returns None on anything unexpected -- an unknown provider, a missing key, a
    client that will not construct -- so the caller silently falls back to the
    server's own service. Failing the whole question because a personal key was
    malformed would be a worse outcome than answering it normally.
    """
    key = (headers.get("x-llm-key") or "").strip()
    if not key:
        return None

    provider = (headers.get("x-llm-provider") or "openai").strip().lower()
    model = (headers.get("x-llm-model") or "").strip() or None

    if provider not in BYO_PROVIDERS:
        logger.warning("Ignoring personal key: unknown provider %r", provider)
        return None

    try:
        if provider == "anthropic":
            from vanna.integrations.anthropic import AnthropicLlmService

            return AnthropicLlmService(api_key=key, model=model)

        from vanna.integrations.openai import OpenAILlmService

        return OpenAILlmService(api_key=key, model=model)
    except Exception as exc:  # noqa: BLE001 - never fail a request over this
        # Note the *type*, never the exception text: client libraries have been
        # known to echo the key back in their error messages.
        logger.warning(
            "Ignoring personal key: %s could not be constructed (%s)",
            provider, type(exc).__name__,
        )
        return None


async def close_llm_service(service: Any) -> None:
    """Release a per-request LLM service's HTTP connections.

    Each personal-key request builds its own client and each client holds a
    connection pool. Without this they accumulate for the life of the worker until
    it runs out of file descriptors.

    Deliberately *not* a client cache keyed by the key: that would keep users' API
    keys resident in memory long after their request, to save a connection setup on
    a call that already takes seconds. Wrong trade.
    """
    client = getattr(service, "_client", None)
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):  # AsyncOpenAI.close() is a coroutine
            await result
    except Exception as exc:  # noqa: BLE001 - a failed cleanup must not fail a request
        logger.debug("Could not close per-request LLM client: %s", type(exc).__name__)
