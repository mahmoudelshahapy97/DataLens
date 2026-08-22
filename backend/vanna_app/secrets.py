"""Encryption for stored credentials, and a type that will not leak them.

Two problems, one module.

**Credentials at rest.** ``tenants.database_url`` holds a warehouse password. Anyone
with a dump of the control plane -- a backup, a read replica, a support export --
had every customer's database password in plaintext. Values are now sealed with
Fernet (AES-128-CBC + HMAC-SHA256, authenticated) under a key derived from
``VANNA_SECRET_KEY``.

**Credentials in flight through our own code.** Encryption at rest does nothing once
a value is decrypted into a Python string that then flows into a log line, an
exception message, or a JSON response. ``Secret`` makes that mistake loud: its
``__str__`` and ``__repr__`` return ``'***'``, so the only way to obtain the value is
to ask for it by name, with ``.reveal()``. Grepping for ``.reveal()`` then lists every
place a credential is genuinely used, which is a list short enough to review.

The key is *derived*, not used directly: ``VANNA_SECRET_KEY`` is a human-supplied
string of arbitrary length and shape, and Fernet requires exactly 32 bytes of
url-safe base64. Deriving with HKDF also means the same secret can safely produce
independent keys for other purposes (CSRF signing, OIDC state) without those uses
sharing key material.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any, Optional

logger = logging.getLogger("vanna.secrets")

#: Marks a value this module produced, so ``decrypt`` can tell ciphertext from a
#: plaintext value written before encryption existed and migrate it in place rather
#: than failing. Deliberately not valid in any connection URL, so it can never
#: collide with a real value.
_PREFIX = "enc:v1:"

#: HKDF context strings. Distinct per purpose so a key used for CSRF signatures
#: cannot decrypt a datasource credential.
_INFO_DATASOURCE = b"vanna.datasource.v1"
_INFO_CSRF = b"vanna.csrf.v1"
_INFO_STATE = b"vanna.oidc-state.v1"


class SecretsUnavailable(RuntimeError):
    """Encryption was needed and cannot be performed."""


def _hkdf(secret: str, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 over the configured secret.

    RFC 5869, in nine lines of ``hmac``, rather than a dependency for a key
    derivation that the standard library already has every primitive for.
    """
    if not secret:
        raise SecretsUnavailable(
            "VANNA_SECRET_KEY is not set, so credentials cannot be encrypted."
        )
    # Extract: a fixed all-zero salt is what RFC 5869 specifies when none is
    # available, and there is no per-deployment salt to store that would not itself
    # need the same protection as the key.
    prk = hmac.new(b"\x00" * 32, secret.encode("utf-8"), hashlib.sha256).digest()
    # Expand.
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def derive_key(secret: str, purpose: str = "datasource") -> bytes:
    """A 32-byte key for one purpose, derived from the deployment secret."""
    info = {
        "datasource": _INFO_DATASOURCE,
        "csrf": _INFO_CSRF,
        "state": _INFO_STATE,
    }.get(purpose)
    if info is None:
        raise ValueError(f"Unknown key purpose {purpose!r}")
    return _hkdf(secret, info)


class Cipher:
    """Seals and opens credential strings.

    Constructed once at startup and passed to whatever stores credentials. A
    deployment with no secret gets a cipher that refuses to encrypt but still reads
    plaintext -- which is what keeps ``demo`` mode working with no configuration
    while making the production path impossible to get wrong.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._fernet: Any = None
        if secret:
            self._fernet = _build_fernet(secret)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        """Seal a value. Empty stays empty; already-sealed is returned unchanged."""
        if not value:
            return value
        if value.startswith(_PREFIX):
            return value
        if self._fernet is None:
            # Demo mode. Loud, once per value, because the alternative -- a silent
            # plaintext write in a deployment that thought it was encrypting -- is
            # the failure this whole module exists to prevent.
            logger.warning(
                "Storing a credential unencrypted: VANNA_SECRET_KEY is not set."
            )
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return f"{_PREFIX}{token}"

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        """Open a value.

        A value without the marker is returned as-is: rows written before this
        module existed are plaintext, and refusing to read them would take a
        working deployment offline on upgrade. The migration re-writes them sealed;
        until it runs, they still work.
        """
        if not value or not value.startswith(_PREFIX):
            return value
        if self._fernet is None:
            raise SecretsUnavailable(
                "A stored credential is encrypted but VANNA_SECRET_KEY is not set. "
                "Restore the key that was in use when it was written."
            )
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretsUnavailable(
                "A stored credential could not be decrypted. VANNA_SECRET_KEY has "
                "most likely changed since it was written; restore the previous key "
                "or re-enter the connection details."
            ) from exc

    def is_sealed(self, value: Optional[str]) -> bool:
        return bool(value) and value.startswith(_PREFIX)


def _build_fernet(secret: str) -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is pinned in the image
        raise SecretsUnavailable(
            "The 'cryptography' package is required to encrypt stored credentials. "
            "Install it, or unset VANNA_SECRET_KEY to run without encryption."
        ) from exc
    return Fernet(base64.urlsafe_b64encode(derive_key(secret, "datasource")))


# ----------------------------------------------------------------------
# The redacting wrapper
# ----------------------------------------------------------------------


class Secret:
    """A string that does not print itself.

    ``str(secret)``, ``repr(secret)``, ``f"{secret}"`` and ``json.dumps`` via
    ``default=str`` all yield ``'***'``. Only ``.reveal()`` returns the value.

        >>> url = Secret("postgresql://vanna:hunter2@db/analytics")
        >>> f"connecting to {url}"
        'connecting to ***'
        >>> url.reveal()
        'postgresql://vanna:hunter2@db/analytics'

    Equality and hashing work on the underlying value so these can be compared and
    used as dictionary keys, but comparison is constant-time to keep a ``==`` from
    becoming a timing oracle.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Optional[str]) -> None:
        self._value = value or ""

    def reveal(self) -> str:
        """The actual value. Every call site of this is a place to review."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "***" if self._value else ""

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return hmac.compare_digest(self._value, other._value)
        if isinstance(other, str):
            return hmac.compare_digest(self._value, other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    # Pydantic and json.dumps(default=...) both reach for these.
    def __get_pydantic_core_schema__(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise TypeError(
            "A Secret must not be serialised. Call .reveal() at the point of use."
        )
