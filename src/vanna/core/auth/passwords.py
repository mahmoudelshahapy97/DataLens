"""Password hashing and verification, on the standard library.

``hashlib.scrypt`` rather than bcrypt or argon2. Both are better-known and neither is
worth a dependency here: scrypt is memory-hard, is in the standard library of every
Python this package supports, and the parameters below are tunable without a migration.
A package whose entire dependency list is twelve lines should not grow a thirteenth for
something CPython already ships.

The encoded form carries its own parameters::

    scrypt$16384$8$1$<salt-b64>$<hash-b64>

so ``n`` can be raised later and existing hashes keep verifying against the values they
were created with. A scheme that stores only the digest cannot be re-tuned without
invalidating every password at once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Tuple

#: scrypt cost parameters. n is the work factor; raising it costs CPU and memory per
#: verification, which is the point. 16384/8/1 is the widely-cited interactive-login
#: baseline and takes single-digit milliseconds.
_N = 16384
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32

_PREFIX = "scrypt"

#: A real hash, used to burn the same CPU when the account does not exist. Built once,
#: on first import of the module rather than on first failed login, so no request pays
#: for its construction.
_DUMMY_HASH: str = ""


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def hash_password(password: str, *, n: int = _N, r: int = _R, p: int = _P) -> str:
    """Hash a password for storage.

    Returns the encoded form described in the module docstring. A fresh random salt is
    generated per call, so the same password stored twice produces different output and
    a stolen table cannot be scanned for shared passwords.
    """
    if not password:
        raise ValueError("Password must not be empty")

    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES
    )
    return f"{_PREFIX}${n}${r}${p}${_encode(salt)}${_encode(derived)}"


def _parse(encoded: str) -> Tuple[int, int, int, bytes, bytes]:
    scheme, n, r, p, salt, digest = encoded.split("$")
    if scheme != _PREFIX:
        raise ValueError(f"Unsupported password scheme {scheme!r}")
    return int(n), int(r), int(p), _decode(salt), _decode(digest)


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash.

    Never raises on a malformed or empty hash -- it returns False. Verification is called
    on a public endpoint, and an exception there would distinguish "this account has no
    usable password" from "wrong password", which is the distinction we are paying scrypt
    to hide.
    """
    if not password or not encoded:
        return False

    try:
        n, r, p, salt, expected = _parse(encoded)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except Exception:
        return False

    return hmac.compare_digest(derived, expected)


def waste_time() -> None:
    """Do the work of a verification, and discard it.

    Called when the account does not exist. Returning early there would make a failed
    login measurably faster for an unknown address than for a known one, which turns the
    login endpoint into a user-enumeration oracle -- and the member list is exactly what
    an attacker wants before trying passwords.

    The dummy hash is built at import, not on first use. Building it lazily made the
    *first* unknown-account attempt cost a hash plus a verification -- twice the work of
    a real failure, and measurably slower -- which is the same oracle in reverse.
    """
    verify_password("not-the-password", _dummy_hash())


def _dummy_hash() -> str:
    """The stand-in hash, built once."""
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


# Prime it at import so no login request ever pays to construct it.
_dummy_hash()


def generate_password(length: int = 16) -> str:
    """A random password, for seeding an admin or issuing a reset."""
    return secrets.token_urlsafe(length)


# ----------------------------------------------------------------------
# Opaque tokens: sessions and API keys
# ----------------------------------------------------------------------


def generate_token() -> str:
    """A session or API token. 32 bytes of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """The value stored for a token.

    Plain SHA-256, deliberately: a token is 256 bits of randomness rather than something
    a human chose, so there is no dictionary to attack and nothing for a slow KDF to buy.
    What matters is that the *stored* value cannot be replayed -- SQL Chat stores session
    tokens verbatim and looks them up by equality, so a database dump there hands over
    every live session.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash or "")
