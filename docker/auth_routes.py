"""Sign in, sign out, change password, and manage API tokens.

Separate from ``portal_routes`` because these are the only endpoints reachable *without*
an identity, and it is worth being able to see all of them on one screen.

Two properties matter more than the rest:

* **The response to a failed login never says why.** Same message and comparable timing
  whether the account is unknown, disabled, or the password is wrong. Anything else is a
  user-enumeration oracle on the one endpoint that must be public.
* **Failed attempts are rate-limited** per address and per client IP. Without that, a
  login route is an offline password cracker with a network hop.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import Any, Deque, Dict, Optional
from collections import deque

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field

logger = logging.getLogger("vanna.auth")

#: Failed attempts allowed per key within the window, before refusing outright.
MAX_ATTEMPTS = int(os.getenv("VANNA_LOGIN_MAX_ATTEMPTS", "8"))
WINDOW_SECONDS = int(os.getenv("VANNA_LOGIN_WINDOW_SECONDS", "300"))

#: The single message every failed login gets. Deliberately uninformative.
_REJECTED = "Those credentials are not valid."


class LoginPayload(BaseModel):
    email: str
    password: str
    tenant: Optional[str] = None


class PasswordPayload(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


class TokenPayload(BaseModel):
    name: str = ""
    ttl_days: Optional[int] = None


class _Attempts:
    """In-memory failed-attempt counter.

    Per process, like the existing quota hook -- which means with several workers the
    effective limit is the limit times the worker count. That is a real weakness and the
    honest fix is a shared counter; it is still far better than none, and it is stated
    here rather than left for someone to discover.
    """

    def __init__(self) -> None:
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > WINDOW_SECONDS:
            hits.popleft()
        return len(hits) >= MAX_ATTEMPTS

    def record(self, key: str) -> None:
        self._hits[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)


def register_auth_routes(
    app: Any,
    *,
    accounts: Any,
    directory: Any,
    session_cookie: str,
    session_ttl_hours: int,
    secure_cookies: bool,
    user_resolver: Any,
    platform_admin_emails: Optional[set] = None,
) -> None:
    """Register the authentication endpoints."""

    attempts = _Attempts()
    platform_admin_emails = platform_admin_emails or set()

    def _require_accounts():
        if accounts is None:
            raise HTTPException(
                status_code=503,
                detail="Authentication needs a control-plane database.",
            )
        return accounts

    def _client_ip(request: Request) -> str:
        # X-Forwarded-For only when a proxy set it; uvicorn runs with --proxy-headers.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else ""

    @app.post("/api/vanna/v2/auth/login")
    async def login(payload: LoginPayload, request: Request, response: Response) -> Dict[str, Any]:
        """Exchange an email and password for a session cookie."""
        store = _require_accounts()

        email = payload.email.strip().lower()
        ip = _client_ip(request)

        # Both keys, so one attacker cannot exhaust one account's budget to lock out its
        # owner, and cannot spread attempts across accounts to evade the per-IP limit.
        if attempts.blocked(f"email:{email}") or attempts.blocked(f"ip:{ip}"):
            logger.warning("Login rate limit hit email=%s ip=%s", email, ip)
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Wait a few minutes and try again.",
            )

        user = await store.verify(email, payload.password)
        if user is None:
            attempts.record(f"email:{email}")
            attempts.record(f"ip:{ip}")
            logger.info("Failed login email=%s ip=%s", email, ip)
            # Same message for unknown, disabled and wrong-password.
            raise HTTPException(status_code=401, detail=_REJECTED)

        attempts.clear(f"email:{email}")

        token = await store.create_session(
            email,
            ttl_hours=session_ttl_hours,
            user_agent=request.headers.get("user-agent", ""),
            ip=ip,
        )

        response.set_cookie(
            session_cookie,
            token,
            max_age=session_ttl_hours * 3600,
            httponly=True,       # unreadable from JavaScript
            samesite="lax",      # survives top-level navigation, not cross-site POSTs
            secure=secure_cookies,
            path="/",
        )

        memberships = (
            await directory.tenants_for_email(email) if directory is not None else []
        )
        logger.info("Login email=%s ip=%s workspaces=%s", email, ip, len(memberships))

        return {
            "email": email,
            "full_name": user.get("full_name") or "",
            "must_change_password": bool(user.get("must_change")),
            "memberships": memberships,
        }

    @app.post("/api/vanna/v2/auth/logout")
    async def logout(request: Request, response: Response) -> Dict[str, Any]:
        """End this session. Idempotent."""
        token = request.cookies.get(session_cookie, "")
        if token and accounts is not None:
            await accounts.delete_session(token)
        response.delete_cookie(session_cookie, path="/")
        return {"signed_out": True}

    @app.post("/api/vanna/v2/auth/password")
    async def change_password(
        payload: PasswordPayload, request: Request
    ) -> Dict[str, Any]:
        """Change your own password.

        Requires the current one even though the caller is already authenticated: a
        borrowed laptop should not be a way to lock the owner out of their own account.
        """
        store = _require_accounts()
        user = await _caller(request)

        if await store.verify(user.email, payload.current_password) is None:
            raise HTTPException(status_code=401, detail=_REJECTED)

        if payload.new_password == payload.current_password:
            raise HTTPException(
                status_code=400, detail="The new password must be different."
            )

        await store.set_password(user.email, payload.new_password)
        logger.info("Password changed for %s", user.email)
        return {"changed": True}

    # ------------------------------------------------------------------
    # API tokens
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/auth/tokens")
    async def list_tokens(request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        return {"tokens": await _require_accounts().list_tokens(user.email)}

    @app.post("/api/vanna/v2/auth/tokens")
    async def create_token(payload: TokenPayload, request: Request) -> Dict[str, Any]:
        """Issue an API token for the CLI or an MCP client.

        The token is in this response and nowhere else -- only its hash is stored, so
        there is no endpoint that can show it again.
        """
        user = await _caller(request)
        token = await _require_accounts().create_token(
            user.email, name=payload.name, ttl_days=payload.ttl_days
        )
        logger.info("API token issued for %s (%s)", user.email, payload.name or "unnamed")
        return {
            "token": token,
            "note": "Copy this now. It cannot be shown again.",
        }

    @app.delete("/api/vanna/v2/auth/tokens/{token_id}")
    async def revoke_token(token_id: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        if not await _require_accounts().revoke_token(user.email, token_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"revoked": True}

    # ------------------------------------------------------------------
    # Account administration (platform admin)
    # ------------------------------------------------------------------

    def _require_platform_admin(user: Any) -> None:
        email = (getattr(user, "email", "") or "").lower()
        if platform_admin_emails and email not in platform_admin_emails:
            raise HTTPException(status_code=404, detail="Not found")

    @app.get("/api/vanna/v2/admin/accounts")
    async def list_accounts(request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_platform_admin(user)
        return {"accounts": await _require_accounts().list_all()}

    @app.post("/api/vanna/v2/admin/accounts")
    async def create_account(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
        """Create an account with a temporary password.

        The password is generated here and returned once. Letting an admin choose
        someone else's password means the admin knows it, and the account's owner
        has no way to tell whether they still do -- `must_change` forces it to be
        replaced on first use.
        """
        user = await _caller(request)
        _require_platform_admin(user)
        store = _require_accounts()

        from vanna.core.auth import generate_password

        email = str(payload.get("email") or "").strip().lower()
        if "@" not in email:
            raise HTTPException(status_code=400, detail="A valid email is required")
        if await store.get(email):
            raise HTTPException(status_code=409, detail="That account already exists")

        password = generate_password()
        await store.create(
            email,
            password,
            full_name=str(payload.get("full_name") or ""),
            must_change=True,
        )
        logger.info("Account created: %s (by %s)", email, user.email)
        return {
            "email": email,
            "temporary_password": password,
            "note": "Give this to them over a channel you trust. It is not stored and cannot be shown again.",
        }

    @app.post("/api/vanna/v2/admin/accounts/{email}/reset")
    async def reset_account(email: str, request: Request) -> Dict[str, Any]:
        user = await _caller(request)
        _require_platform_admin(user)
        store = _require_accounts()

        from vanna.core.auth import generate_password

        target = email.strip().lower()
        if not await store.get(target):
            raise HTTPException(status_code=404, detail="Not found")

        password = generate_password()
        await store.create(target, password, must_change=True)
        logger.warning("Password reset for %s (by %s)", target, user.email)
        return {"email": target, "temporary_password": password}

    @app.patch("/api/vanna/v2/admin/accounts/{email}")
    async def set_account_active(
        email: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        user = await _caller(request)
        _require_platform_admin(user)
        store = _require_accounts()

        target = email.strip().lower()
        active = bool(payload.get("is_active", True))

        # Disabling your own account signs you out with no way back in unless
        # another platform admin exists. Refuse rather than field the ticket.
        if not active and target == (user.email or "").lower():
            raise HTTPException(
                status_code=400, detail="You cannot disable your own account."
            )

        if not await store.set_active(target, active):
            raise HTTPException(status_code=404, detail="Not found")
        logger.warning(
            "Account %s %s (by %s)", target, "enabled" if active else "disabled", user.email
        )
        return {"email": target, "is_active": active}

    # ------------------------------------------------------------------

    async def _caller(request: Request):
        """Resolve the caller through the same resolver every other route uses."""
        from vanna.core.user import RequestContext

        try:
            return await user_resolver.resolve_user(
                RequestContext(
                    headers=dict(request.headers),
                    cookies=dict(request.cookies),
                    metadata={},
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
