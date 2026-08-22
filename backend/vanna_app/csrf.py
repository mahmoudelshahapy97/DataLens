"""Cross-site request forgery protection: signed double-submit cookies.

``SameSite=Lax`` on the session cookie already blocks the common case -- a
cross-site form POST does not carry it. It is not the whole story:

* ``Lax`` permits top-level ``GET`` navigation with cookies, so any state-changing
  ``GET`` would be exposed. (There are none here, and this makes sure of it by
  refusing to treat ``GET`` as safe-by-accident.)
* Browsers that predate ``SameSite`` treat it as ``None``.
* The moment anything needs ``SameSite=None`` -- an embedded ``<vanna-chat>`` on a
  customer's own domain is the obvious future request -- ``Lax`` stops helping and
  nothing else is left.

The *signed* double-submit variant, rather than the plain one. Plain double-submit
trusts that an attacker cannot write a cookie for our domain, which is false when a
subdomain is compromised or the site has ever served user content. Here the cookie
value is ``<random>.<hmac>``, keyed by ``VANNA_SECRET_KEY``, so a value the server
did not issue fails verification whoever managed to set it.

Bearer-token callers are exempt, and this is not a loophole: CSRF exists because
browsers attach cookies automatically. Nothing attaches an ``Authorization`` header
automatically, so a request carrying one cannot be forged by a third-party page.
"""

from __future__ import annotations

import hmac
import logging
import secrets as _secrets
from hashlib import sha256
from typing import Any, Dict, Iterable, Tuple

from .secrets import derive_key

logger = logging.getLogger("vanna.csrf")

COOKIE_NAME = "vanna_csrf"
HEADER_NAME = "x-csrf-token"

#: Methods that cannot change state and therefore need no token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Paths exempt from the check.
#:
#: ``login`` is exempt because there is no session to protect yet and a forged login
#: achieves nothing an attacker could not do by visiting the page themselves. It is
#: still rate-limited, which is the control that matters there.
#:
#: The OIDC callback is a top-level ``GET`` redirect from the identity provider and
#: carries its own ``state`` parameter, which is CSRF protection by another name.
EXEMPT_PATHS = (
    "/api/vanna/v2/auth/login",
    "/api/vanna/v2/auth/logout",
    "/api/vanna/v2/auth/forgot",
    "/api/vanna/v2/auth/reset",
    "/api/vanna/v2/auth/oidc/",
)


def _key(secret: str) -> bytes:
    return derive_key(secret, "csrf")


def issue(secret: str) -> str:
    """Mint a token: 128 bits of randomness plus its signature."""
    nonce = _secrets.token_urlsafe(16)
    signature = hmac.new(_key(secret), nonce.encode("ascii"), sha256).hexdigest()[:32]
    return f"{nonce}.{signature}"


def verify(secret: str, token: str) -> bool:
    """Whether this deployment issued ``token``."""
    if not token or "." not in token:
        return False
    nonce, _, signature = token.rpartition(".")
    if not nonce or not signature:
        return False
    expected = hmac.new(_key(secret), nonce.encode("ascii"), sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, signature)


def matches(secret: str, cookie_value: str, header_value: str) -> bool:
    """The double-submit check: both present, equal, and validly signed."""
    if not cookie_value or not header_value:
        return False
    if not hmac.compare_digest(cookie_value, header_value):
        return False
    return verify(secret, cookie_value)


class CsrfMiddleware:
    """Enforces the check and keeps the cookie fresh.

    Pure ASGI, like ``RequestContextMiddleware`` and for the same reason: Starlette's
    ``BaseHTTPMiddleware`` buffers streaming responses, and streaming is the product.
    """

    def __init__(
        self,
        app: Any,
        *,
        secret: str,
        secure: bool,
        enabled: bool = True,
        exempt_paths: Iterable[str] = EXEMPT_PATHS,
    ) -> None:
        self.app = app
        self.secret = secret
        self.secure = secure
        # A deployment with no secret cannot sign anything. Demo mode lands here,
        # and the config validator refuses to start multi-tenant without a key, so
        # this can only be off where it was always going to be off.
        self.enabled = enabled and bool(secret)
        self.exempt: Tuple[str, ...] = tuple(exempt_paths)
        if enabled and not secret:
            logger.warning("CSRF protection is disabled: VANNA_SECRET_KEY is not set.")

    def _exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.exempt)

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        cookies = _cookies(headers.get("cookie", ""))
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")

        if method not in SAFE_METHODS and not self._exempt(path):
            # A bearer token cannot be attached by a third-party page, so a request
            # authenticated that way is not forgeable and needs no token.
            authorization = headers.get("authorization", "")
            if not authorization.lower().startswith("bearer "):
                if not matches(
                    self.secret,
                    cookies.get(COOKIE_NAME, ""),
                    headers.get(HEADER_NAME, ""),
                ):
                    logger.info("CSRF check failed: %s %s", method, path)
                    await _reject(send)
                    return

        # Hand the browser a token if it has none, so the first state-changing call
        # after a page load succeeds without an extra round trip.
        issue_new = COOKIE_NAME not in cookies
        token = issue(self.secret) if issue_new else ""

        async def send_wrapper(message: Dict[str, Any]) -> None:
            if issue_new and message["type"] == "http.response.start":
                message.setdefault("headers", [])
                # Readable by JavaScript on purpose: the page has to echo it back
                # in a header, which is the half an attacker's page cannot do.
                cookie = (
                    f"{COOKIE_NAME}={token}; Path=/; SameSite=Lax"
                    + ("; Secure" if self.secure else "")
                )
                message["headers"].append((b"set-cookie", cookie.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _headers(scope: Dict[str, Any]) -> Dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _cookies(header: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


async def _reject(send: Any) -> None:
    body = (
        b'{"detail":{"code":"csrf_failed","message":'
        b'"This request could not be verified. Reload the page and try again."}}'
    )
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})
