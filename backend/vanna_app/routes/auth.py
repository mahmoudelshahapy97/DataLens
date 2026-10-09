"""Sign in, sign out, password management, sessions, API tokens, SSO.

These are the only endpoints reachable *without* an identity, and it is worth being
able to see all of them on one screen.

Properties that matter more than the rest:

* **A failed login never says why.** Same message and comparable timing whether the
  account is unknown, disabled, external, or the password is wrong. Anything else is
  a user-enumeration oracle on the one endpoint that must be public.
* **Failed attempts are throttled** per address and per client IP, in the control
  plane, so the limit is the limit regardless of worker count.
* **A temporary password grants a restricted session.** ``must_change`` used to be a
  flag in the login response that only the browser honoured; the session issued here
  now carries a scope the server enforces on every other route.
* **Forgot-password reveals nothing.** The same 200 and the same body whether or not
  the address exists.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..accounts import RESET_TTL_MINUTES, SCOPE_FULL, SCOPE_PASSWORD_CHANGE
from ..authz import require_platform_admin
from ..observability import get_metrics
from . import Deps

logger = logging.getLogger("vanna.auth")

#: The single message every failed login gets. Deliberately uninformative.
_REJECTED = "Those credentials are not valid."

#: The single response the forgot-password endpoint always gives.
_RESET_SENT = {
    "sent": True,
    "message": "If that address has an account, a reset link is on its way.",
}

#: Shortest acceptable password. Length beats composition rules -- which mostly
#: produce Password1! -- and 12 is the current floor in most published guidance.
MIN_PASSWORD_LENGTH = 12


class LoginPayload(BaseModel):
    email: str
    password: str
    tenant: Optional[str] = None


class PasswordPayload(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class ForgotPayload(BaseModel):
    email: str


class ResetPayload(BaseModel):
    token: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class TokenPayload(BaseModel):
    name: str = ""
    ttl_days: Optional[int] = None


class AccountPayload(BaseModel):
    email: str
    full_name: str = ""


def _weak(password: str, email: str) -> Optional[str]:
    """Reject the passwords that are guessed first.

    Not a composition policy. Three checks that catch the passwords which actually
    appear in breach dumps, and nothing that pushes people toward writing it down.
    """
    lowered = password.lower()
    local = (email or "").split("@")[0].lower()
    if local and len(local) > 2 and local in lowered:
        return "The password must not contain your email address."
    if lowered in {
        "password", "passw0rd", "letmein", "welcome", "changeme", "correcthorse",
        "administrator", "vannavanna", "qwertyuiop", "123456789012",
    }:
        return "That password is too common."
    if len(set(password)) < 5:
        return "The password must use at least five different characters."
    return None


def register(app: Any, deps: Deps) -> None:
    settings = deps.settings
    metrics = get_metrics()

    def _set_session_cookie(response: Response, token: str, *, ttl_hours: int) -> None:
        response.set_cookie(
            settings.session_cookie,
            token,
            max_age=ttl_hours * 3600,
            httponly=True,   # unreadable from JavaScript
            samesite="lax",  # survives top-level navigation, not cross-site POSTs
            secure=settings.secure_cookies,
            path="/",
        )

    # ------------------------------------------------------------------
    # Sign in / out
    # ------------------------------------------------------------------

    @app.post("/api/vanna/v2/auth/login")
    async def login(payload: LoginPayload, request: Request, response: Response) -> Dict[str, Any]:
        """Exchange an email and password for a session cookie."""
        store = deps.require_accounts()
        throttle = deps.login_throttle

        email = payload.email.strip().lower()
        ip = deps.client_ip(request)

        if throttle is not None and await throttle.blocked(email, ip):
            metrics.login_failures.labels("throttled").inc()
            logger.warning("Login throttled email=%s ip=%s", email, ip)
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Wait a few minutes and try again.",
            )

        user = await store.verify(email, payload.password)
        if user is None:
            if throttle is not None:
                await throttle.record_failure(email, ip)
            metrics.login_failures.labels("rejected").inc()
            logger.info("Failed login email=%s ip=%s", email, ip)
            # Same message for unknown, disabled, external and wrong-password.
            raise HTTPException(status_code=401, detail=_REJECTED)

        if throttle is not None:
            await throttle.clear(email)

        # A temporary password buys a session that can do exactly one thing. The
        # previous behaviour handed out a full 72-hour session and trusted the
        # browser to act on a flag.
        must_change = bool(user.get("must_change"))
        scope = SCOPE_PASSWORD_CHANGE if must_change else SCOPE_FULL
        ttl_hours = 1 if must_change else settings.session_ttl_hours

        token = await store.create_session(
            email,
            ttl_hours=ttl_hours,
            user_agent=request.headers.get("user-agent", ""),
            ip=ip,
            scope=scope,
        )
        _set_session_cookie(response, token, ttl_hours=ttl_hours)

        memberships = (
            await deps.directory.tenants_for_email(email) if deps.directory is not None else []
        )
        logger.info("Login email=%s ip=%s workspaces=%d", email, ip, len(memberships))

        return {
            "email": email,
            "full_name": user.get("full_name") or "",
            "must_change_password": must_change,
            "memberships": memberships,
        }

    @app.post("/api/vanna/v2/auth/logout")
    async def logout(request: Request, response: Response) -> Dict[str, Any]:
        """End this session. Idempotent."""
        token = request.cookies.get(settings.session_cookie, "")
        if token and deps.accounts is not None:
            # Not `delete_session`, which is the account screen's "end that other
            # session" and takes (email, session_id). The two shared a name, so
            # this call raised TypeError and the row was never deleted -- the
            # cookie was cleared in the browser and the session stayed valid on
            # the server, which is the opposite of what signing out means.
            await deps.accounts.end_session_by_token(token)
        response.delete_cookie(settings.session_cookie, path="/")
        return {"signed_out": True}

    @app.get("/api/vanna/v2/auth/methods")
    async def methods() -> Dict[str, Any]:
        """What this deployment accepts. Drives the sign-in screen."""
        return {
            "password": "password" in settings.auth_methods,
            "oidc": settings.oidc_enabled,
            "oidc_label": (deps.oidc.label if deps.oidc else "") or "Single sign-on",
            "can_reset": settings.smtp_enabled and "password" in settings.auth_methods,
        }

    # ------------------------------------------------------------------
    # Passwords
    # ------------------------------------------------------------------

    @app.post("/api/vanna/v2/auth/password")
    async def change_password(payload: PasswordPayload, request: Request) -> Dict[str, Any]:
        """Change your own password.

        Reachable with a restricted session -- it is the one thing such a session
        exists to do. Requires the current password even though the caller is
        already authenticated: a borrowed laptop should not be a way to lock the
        owner out of their own account.
        """
        store = deps.require_accounts()
        user = await deps.caller(request, full_session=False)

        if await store.verify(user.email, payload.current_password) is None:
            raise HTTPException(status_code=401, detail=_REJECTED)
        if payload.new_password == payload.current_password:
            raise HTTPException(status_code=400, detail="The new password must be different.")
        weak = _weak(payload.new_password, user.email)
        if weak:
            raise HTTPException(status_code=400, detail=weak)

        # Keeps this session, ends every other one, and revokes API tokens.
        await store.set_password(
            user.email,
            payload.new_password,
            keep_token=request.cookies.get(settings.session_cookie, ""),
        )
        await _promote_session(request)

        if deps.mailer is not None:
            from ..mail import password_changed

            await deps.mailer.send(
                password_changed(to=user.email, base_url=settings.public_base_url)
            )
        await deps.admin_audit.record(
            "auth.password_change",
            actor_email=user.email,
            target=user.email,
            actor_ip=deps.client_ip(request),
        )
        logger.info("Password changed for %s", user.email)
        return {"changed": True}

    async def _promote_session(request: Request) -> None:
        """Lift a password-change-only session to a full one.

        Called after a successful change so the browser continues straight into the
        app rather than being asked to sign in again with the password it just set.
        """
        from vanna.core.auth import hash_token

        from ..db import SCHEMA

        token = request.cookies.get(settings.session_cookie, "")
        if not token or deps.accounts is None:
            return
        await deps.accounts.db.execute(
            f"""UPDATE {SCHEMA}.sessions
                   SET scope = 'full',
                       expires_at = now() + make_interval(hours => %s)
                 WHERE token_hash = %s""",
            (settings.session_ttl_hours, hash_token(token)),
        )

    @app.post("/api/vanna/v2/auth/forgot")
    async def forgot(payload: ForgotPayload, request: Request) -> Dict[str, Any]:
        """Start a password reset.

        Always answers identically. Whether the address exists, is disabled, or
        authenticates through an identity provider is not something a public
        endpoint gets to tell you.
        """
        if "password" not in settings.auth_methods:
            raise HTTPException(
                status_code=404, detail="Password sign-in is not enabled here."
            )
        store = deps.require_accounts()
        email = payload.email.strip().lower()
        ip = deps.client_ip(request)

        # Throttled on the same budget as failed logins, so this cannot be used to
        # mail-bomb an address or to probe for accounts by watching timing.
        if deps.login_throttle is not None and await deps.login_throttle.blocked(email, ip):
            return _RESET_SENT
        if deps.login_throttle is not None:
            await deps.login_throttle.record_failure(email, ip)

        token = await store.create_reset(email, ip=ip)
        if token and deps.mailer is not None:
            from ..mail import password_reset

            await deps.mailer.send(
                password_reset(
                    to=email,
                    token=token,
                    base_url=settings.public_base_url,
                    ttl_minutes=RESET_TTL_MINUTES,
                )
            )
            await deps.admin_audit.record(
                "auth.reset_requested", actor_email=email, target=email, actor_ip=ip
            )
        logger.info("Password reset requested for %s (issued=%s)", email, bool(token))
        return _RESET_SENT

    @app.post("/api/vanna/v2/auth/reset")
    async def reset(payload: ResetPayload, request: Request) -> Dict[str, Any]:
        """Finish a password reset.

        The token is single-use and claimed in the same statement that redeems it,
        so two requests racing with the same link cannot both succeed.
        """
        store = deps.require_accounts()
        weak = _weak(payload.new_password, "")
        if weak:
            raise HTTPException(status_code=400, detail=weak)

        email = await store.redeem_reset(payload.token, payload.new_password)
        if email is None:
            raise HTTPException(
                status_code=400,
                detail="That reset link is not valid, has expired, or has already "
                       "been used. Request a new one.",
            )
        await deps.admin_audit.record(
            "auth.reset_redeemed",
            actor_email=email,
            target=email,
            actor_ip=deps.client_ip(request),
        )
        return {"reset": True}

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/auth/sessions")
    async def list_sessions(request: Request) -> Dict[str, Any]:
        """Where this account is currently signed in."""
        store = deps.require_accounts()
        user = await deps.caller(request)
        return {
            "sessions": await store.list_sessions(
                user.email, current_token=request.cookies.get(settings.session_cookie, "")
            )
        }

    @app.delete("/api/vanna/v2/auth/sessions")
    async def sign_out_everywhere(request: Request) -> Dict[str, Any]:
        """End every session except this one.

        The self-service answer to a laptop left somewhere. Deliberately
        all-or-nothing rather than per-row: picking a single session to kill
        requires identifying it by something the browser holds, and the only such
        thing is the token itself.
        """
        store = deps.require_accounts()
        user = await deps.caller(request)
        ended = await store.delete_other_sessions(
            user.email, keep_token=request.cookies.get(settings.session_cookie, "")
        )
        await deps.admin_audit.record(
            "auth.sessions_revoked",
            actor_email=user.email,
            target=user.email,
            details={"ended": ended},
            actor_ip=deps.client_ip(request),
        )
        logger.warning("%s ended %d other session(s)", user.email, ended)
        return {"ended": ended}

    @app.delete("/api/vanna/v2/auth/sessions/{session_id}")
    async def sign_out_one(session_id: str, request: Request) -> Dict[str, Any]:
        """End one session.

        "Sign out everywhere else" was the only option, which is the wrong shape
        for the common case: one unfamiliar device in the list, and no reason to
        also sign out the three that are fine. The row is identified by the short
        hash prefix ``GET /auth/sessions`` already returns, and the delete is scoped
        to the caller's own account.
        """
        store = deps.require_accounts()
        user = await deps.caller(request)
        ended = await store.delete_session(
            user.email,
            session_id,
            keep_token=request.cookies.get(settings.session_cookie, ""),
        )
        if not ended:
            raise HTTPException(status_code=404, detail="No such session")
        await deps.admin_audit.record(
            "auth.session_revoked",
            actor_email=user.email,
            target=user.email,
            details={"session": session_id},
            actor_ip=deps.client_ip(request),
        )
        logger.warning("%s ended session %s", user.email, session_id)
        return {"ended": ended}

    # ------------------------------------------------------------------
    # API tokens
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/auth/tokens")
    async def list_tokens(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        return {"tokens": await deps.require_accounts().list_tokens(user.email)}

    @app.post("/api/vanna/v2/auth/tokens")
    async def create_token(payload: TokenPayload, request: Request) -> Dict[str, Any]:
        """Issue an API token for the CLI or an MCP client.

        The token is in this response and nowhere else -- only its hash is stored,
        so there is no endpoint that can show it again.
        """
        user = await deps.caller(request)
        token = await deps.require_accounts().create_token(
            user.email, name=payload.name, ttl_days=payload.ttl_days
        )
        await deps.admin_audit.record(
            "auth.token_create",
            actor_email=user.email,
            target=payload.name or "unnamed",
            actor_ip=deps.client_ip(request),
        )
        logger.info("API token issued for %s (%s)", user.email, payload.name or "unnamed")
        return {"token": token, "note": "Copy this now. It cannot be shown again."}

    @app.delete("/api/vanna/v2/auth/tokens/{token_id}")
    async def revoke_token(token_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        if not await deps.require_accounts().revoke_token(user.email, token_id):
            raise HTTPException(status_code=404, detail="Not found")
        await deps.admin_audit.record(
            "auth.token_revoke",
            actor_email=user.email,
            target=token_id,
            actor_ip=deps.client_ip(request),
        )
        return {"revoked": True}

    # ------------------------------------------------------------------
    # Single sign-on
    # ------------------------------------------------------------------

    if settings.oidc_enabled and deps.oidc is not None:

        @app.get("/api/vanna/v2/auth/oidc/login")
        async def oidc_login(request: Request) -> Any:
            """Begin the authorization-code flow."""
            from fastapi.responses import RedirectResponse

            url, state_cookie = await deps.oidc.begin(
                redirect_uri=f"{settings.public_base_url}/api/vanna/v2/auth/oidc/callback"
            )
            response = RedirectResponse(url, status_code=302)
            response.set_cookie(
                deps.oidc.STATE_COOKIE,
                state_cookie,
                max_age=600,
                httponly=True,
                samesite="lax",
                secure=settings.secure_cookies,
                path="/api/vanna/v2/auth/oidc",
            )
            return response

        @app.get("/api/vanna/v2/auth/oidc/callback")
        async def oidc_callback(request: Request) -> Any:
            """Complete the flow and issue our own session.

            The identity provider says who somebody is. It does not say what they
            may see: membership still comes from ``tenant_users``, so an SSO
            identity with no membership can sign in and reach nothing.
            """
            from fastapi.responses import RedirectResponse

            from ..oidc import OidcError

            store = deps.require_accounts()
            try:
                claims = await deps.oidc.complete(
                    code=request.query_params.get("code", ""),
                    state=request.query_params.get("state", ""),
                    state_cookie=request.cookies.get(deps.oidc.STATE_COOKIE, ""),
                    redirect_uri=f"{settings.public_base_url}/api/vanna/v2/auth/oidc/callback",
                )
            except OidcError as exc:
                logger.warning("OIDC sign-in failed: %s", exc)
                return RedirectResponse(f"{settings.public_base_url}/?sso_error=1", status_code=302)

            email = (claims.get("email") or "").strip().lower()
            if not email:
                logger.warning("OIDC token carried no email claim; refusing.")
                return RedirectResponse(f"{settings.public_base_url}/?sso_error=1", status_code=302)

            await store.upsert_external(
                email,
                full_name=claims.get("name") or "",
                provider="oidc",
                external_id=str(claims.get("sub") or email),
            )
            await deps.oidc.apply_mapping(deps.directory, email, claims)

            token = await store.create_session(
                email,
                ttl_hours=settings.session_ttl_hours,
                user_agent=request.headers.get("user-agent", ""),
                ip=deps.client_ip(request),
                scope=SCOPE_FULL,
            )
            logger.info("OIDC sign-in for %s", email)

            response = RedirectResponse(f"{settings.public_base_url}/", status_code=302)
            _set_session_cookie(response, token, ttl_hours=settings.session_ttl_hours)
            response.delete_cookie(deps.oidc.STATE_COOKIE, path="/api/vanna/v2/auth/oidc")
            return response

    # ------------------------------------------------------------------
    # Account administration (platform admin)
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/admin/accounts")
    async def list_accounts(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        return {"accounts": await deps.require_accounts().list_all()}

    @app.post("/api/vanna/v2/admin/accounts")
    async def create_account(payload: AccountPayload, request: Request) -> Dict[str, Any]:
        """Create an account with a temporary password.

        The password is generated here and mailed if mail is configured. Letting an
        admin choose somebody else's password means the admin knows it, and the
        account's owner has no way to tell whether they still do -- ``must_change``
        forces it to be replaced, and now the *server* enforces that.
        """
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        store = deps.require_accounts()

        from vanna.core.auth import generate_password

        email = payload.email.strip().lower()
        if "@" not in email:
            raise HTTPException(status_code=400, detail="A valid email is required")
        if await store.get(email):
            raise HTTPException(status_code=409, detail="That account already exists")

        password = generate_password()
        await store.create(email, password, full_name=payload.full_name, must_change=True)
        await deps.admin_audit.record(
            "account.create",
            actor_email=user.email,
            target=email,
            actor_ip=deps.client_ip(request),
        )

        mailed = False
        if deps.mailer is not None and settings.smtp_host:
            from ..mail import account_invitation

            mailed = await deps.mailer.send(
                account_invitation(
                    to=email,
                    temporary_password=password,
                    base_url=settings.public_base_url,
                    workspace=user.tenant_id,
                    invited_by=user.email,
                )
            )
        logger.info("Account created: %s (by %s, mailed=%s)", email, user.email, mailed)
        return {
            "email": email,
            # Returned only when it could not be delivered, so a configured
            # deployment never puts a live credential in an HTTP response.
            "temporary_password": "" if mailed else password,
            "mailed": mailed,
            "note": (
                "An invitation has been sent."
                if mailed
                else "Give this to them over a channel you trust. It is not stored "
                     "and cannot be shown again."
            ),
        }

    @app.post("/api/vanna/v2/admin/accounts/{email}/reset")
    async def reset_account(email: str, request: Request) -> Dict[str, Any]:
        """Issue a temporary password, ending every session and token."""
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        store = deps.require_accounts()

        from vanna.core.auth import generate_password

        target = email.strip().lower()
        if not await store.get(target):
            raise HTTPException(status_code=404, detail="Not found")

        password = generate_password()
        # `set_temporary_password`, not `create`: the latter's upsert overwrote
        # `full_name` with the empty string this route does not supply, so every
        # password reset silently erased the account's name.
        await store.set_temporary_password(target, password)
        await deps.admin_audit.record(
            "account.reset",
            actor_email=user.email,
            target=target,
            actor_ip=deps.client_ip(request),
        )

        mailed = False
        if deps.mailer is not None and settings.smtp_host:
            from ..mail import account_invitation

            mailed = await deps.mailer.send(
                account_invitation(
                    to=target,
                    temporary_password=password,
                    base_url=settings.public_base_url,
                    workspace=user.tenant_id,
                    invited_by=user.email,
                )
            )
        logger.warning("Password reset for %s (by %s)", target, user.email)
        return {
            "email": target,
            "temporary_password": "" if mailed else password,
            "mailed": mailed,
        }

    @app.patch("/api/vanna/v2/admin/accounts/{email}")
    async def set_account_active(
        email: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        require_platform_admin(user, settings)
        store = deps.require_accounts()

        target = email.strip().lower()
        active = bool(payload.get("is_active", True))

        # Disabling your own account signs you out with no way back in unless
        # another platform admin exists. Refuse rather than field the ticket.
        if not active and target == (user.email or "").lower():
            raise HTTPException(status_code=400, detail="You cannot disable your own account.")

        if not await store.set_active(target, active):
            raise HTTPException(status_code=404, detail="Not found")
        await deps.admin_audit.record(
            "account.enable" if active else "account.disable",
            actor_email=user.email,
            target=target,
            actor_ip=deps.client_ip(request),
        )
        logger.warning(
            "Account %s %s (by %s)", target, "enabled" if active else "disabled", user.email
        )
        return {"email": target, "is_active": active}
