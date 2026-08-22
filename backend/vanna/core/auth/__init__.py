"""Authentication primitives: password hashing and opaque tokens.

    from vanna.core.auth import hash_password, verify_password, generate_token

Storage and HTTP live in the deployment (``vanna_app/accounts.py`` and
``vanna_app/routes/auth.py``); this package holds only the cryptography, so it is
usable by any assembly and testable without a database.
"""

from .passwords import (
    generate_password,
    generate_token,
    hash_password,
    hash_token,
    tokens_match,
    verify_password,
    waste_time,
)

__all__ = [
    "hash_password",
    "verify_password",
    "waste_time",
    "generate_password",
    "generate_token",
    "hash_token",
    "tokens_match",
]
