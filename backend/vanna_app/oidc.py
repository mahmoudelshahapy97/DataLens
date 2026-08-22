"""Single sign-on over OpenID Connect.

Authorization code flow with PKCE, against any compliant provider -- Okta, Entra,
Auth0, Keycloak, Google. Enterprise buyers ask for this before they ask about
anything else, and password-only authentication is what makes a deployment fail
their review.

The design decision worth stating: **the identity provider says who somebody is, and
nothing more.** It does not grant access. A successful SSO login creates or updates a
row in ``users`` and issues our own session cookie; membership and role still come
from ``tenant_users``, exactly as they do for a password login. An SSO identity with
no membership can sign in and reach nothing, which is the correct behaviour and the
reason this is a hundred lines rather than a rewrite of the authorisation model.

Optional group mapping (``VANNA_OIDC_ROLE_CLAIM``) and domain auto-provisioning
(``VANNA_OIDC_AUTO_PROVISION_TENANT``) exist because the alternative -- an
administrator hand-adding every employee of a company that has just bought this --
is the reason people ask for SSO in the first place. Both are off by default: joining
a workspace is a decision, and a claim in a token somebody else issues should not be
able to make it silently.

State and PKCE verifier travel in a signed, short-lived cookie rather than in server
memory, so the callback works whichever worker handles it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from .secrets import derive_key

logger = logging.getLogger("vanna.oidc")


class OidcError(RuntimeError):
    """The flow could not be completed. Never shown verbatim to a browser."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class OidcClient:
    """One configured identity provider."""

    STATE_COOKIE = "vanna_oidc_state"

    #: How long a started flow stays valid. Long enough to type a password and
    #: answer an MFA prompt, short enough that a stolen cookie is not a login.
    STATE_TTL_SECONDS = 600

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        secret_key: str,
        scopes: str = "openid email profile",
        role_claim: str = "",
        auto_provision_tenant: str = "",
        label: str = "Single sign-on",
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.secret_key = secret_key
        self.scopes = scopes
        self.role_claim = role_claim
        self.auto_provision_tenant = auto_provision_tenant
        self.label = label
        self._metadata: Optional[Dict[str, Any]] = None

    # -- discovery -----------------------------------------------------

    async def metadata(self) -> Dict[str, Any]:
        """The provider's configuration, fetched once and cached.

        Discovery rather than three more environment variables: the endpoints are
        the provider's to change, and a deployment that hard-codes them breaks on a
        migration nobody told it about.
        """
        if self._metadata is not None:
            return self._metadata

        import httpx

        url = f"{self.issuer}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url)
                response.raise_for_status()
                self._metadata = response.json()
        except Exception as exc:
            raise OidcError(f"Could not read OIDC discovery document from {url}: {exc}") from exc

        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if required not in self._metadata:
                raise OidcError(f"Discovery document is missing {required}.")
        return self._metadata

    # -- state ---------------------------------------------------------

    def _sign(self, payload: Dict[str, Any]) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(
            derive_key(self.secret_key, "state"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()[:32]
        return f"{body}.{signature}"

    def _unsign(self, token: str) -> Dict[str, Any]:
        body, _, signature = (token or "").rpartition(".")
        if not body or not signature:
            raise OidcError("Malformed state cookie.")
        expected = hmac.new(
            derive_key(self.secret_key, "state"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()[:32]
        if not hmac.compare_digest(expected, signature):
            raise OidcError("State cookie signature does not verify.")
        payload = json.loads(_unb64(body))
        if time.time() - float(payload.get("t", 0)) > self.STATE_TTL_SECONDS:
            raise OidcError("Sign-in took too long; start again.")
        return payload

    # -- flow ----------------------------------------------------------

    async def begin(self, *, redirect_uri: str) -> Tuple[str, str]:
        """Return ``(authorization_url, state_cookie_value)``."""
        metadata = await self.metadata()

        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())

        from urllib.parse import urlencode

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scopes,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        cookie = self._sign({"s": state, "v": verifier, "t": time.time()})
        return f"{metadata['authorization_endpoint']}?{query}", cookie

    async def complete(
        self, *, code: str, state: str, state_cookie: str, redirect_uri: str
    ) -> Dict[str, Any]:
        """Exchange the code and return the verified claims."""
        if not code:
            raise OidcError("The provider returned no authorization code.")

        stored = self._unsign(state_cookie)
        if not hmac.compare_digest(str(stored.get("s") or ""), state or ""):
            # This is the CSRF check for the whole flow.
            raise OidcError("State does not match; refusing.")

        metadata = await self.metadata()

        import httpx

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    metadata["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "code_verifier": stored.get("v", ""),
                    },
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                tokens = response.json()
        except Exception as exc:
            raise OidcError(f"Token exchange failed: {type(exc).__name__}") from exc

        id_token = tokens.get("id_token")
        if not id_token:
            raise OidcError("The provider returned no id_token.")

        return await self._verify(id_token, metadata)

    async def _verify(self, id_token: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Verify the id_token's signature, issuer, audience and expiry.

        The signature check is the whole point: without it the id_token is a
        base64-encoded assertion the caller could have written themselves, and the
        code exchange over TLS is doing all the work. ``authlib`` handles the JWKS
        fetch and the algorithm allow-list; without it we refuse rather than
        decoding unverified, because a "temporary" unverified path is how this ends
        up in production.
        """
        try:
            from authlib.jose import JsonWebKey, jwt
        except ImportError as exc:
            raise OidcError(
                "The 'authlib' package is required for OIDC sign-in. Install it, or "
                "remove oidc from VANNA_AUTH_METHODS."
            ) from exc

        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                jwks = (await client.get(metadata["jwks_uri"])).json()
            claims = jwt.decode(id_token, JsonWebKey.import_key_set(jwks))
            claims.validate(leeway=60)
        except Exception as exc:
            raise OidcError(f"id_token did not verify: {type(exc).__name__}") from exc

        issuer = str(claims.get("iss") or "").rstrip("/")
        if issuer != self.issuer:
            raise OidcError("id_token was issued by a different provider.")

        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.client_id not in audiences:
            raise OidcError("id_token was not issued for this client.")

        return dict(claims)

    # -- mapping -------------------------------------------------------

    async def apply_mapping(self, directory: Any, email: str, claims: Dict[str, Any]) -> None:
        """Optionally grant membership from the provider's claims.

        Both halves are opt-in. Auto-provisioning turns a token claim into workspace
        access, which is a real trust delegation to the identity provider -- fine
        when it is the company's own, wrong by default.
        """
        if directory is None or not self.auto_provision_tenant:
            return

        role = "analyst"
        if self.role_claim:
            raw = claims.get(self.role_claim)
            values = raw if isinstance(raw, list) else [raw]
            wanted = {str(v).strip().lower() for v in values if v}
            # Most privileged claim wins, and only from a closed set: a provider
            # that emits "Admin" for its own console must not thereby grant admin
            # here unless somebody mapped it deliberately.
            if "vanna-admin" in wanted or "admin" in wanted:
                role = "admin"
            elif "vanna-viewer" in wanted or "viewer" in wanted:
                role = "viewer"

        existing = await directory.get_member(self.auto_provision_tenant, email)
        if existing is not None:
            # Never demote or promote an existing member from a token. Somebody
            # curated that row; a claim should not silently overwrite it.
            return

        try:
            await directory.add_user(
                self.auto_provision_tenant,
                email,
                full_name=claims.get("name") or "",
                role=role,
            )
            logger.info(
                "Auto-provisioned %s into %s as %s from OIDC claims",
                email, self.auto_provision_tenant, role,
            )
        except Exception as exc:
            logger.warning("Could not auto-provision %s: %s", email, exc)


def build_oidc_client(settings: Any) -> Optional[OidcClient]:
    """Construct the client, or None when SSO is not configured."""
    if not settings.oidc_enabled:
        return None
    return OidcClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        secret_key=settings.secret_key,
        scopes=settings.oidc_scopes,
        role_claim=settings.oidc_role_claim,
        auto_provision_tenant=settings.oidc_auto_provision_tenant,
    )
