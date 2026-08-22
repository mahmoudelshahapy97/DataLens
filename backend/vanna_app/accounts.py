"""Accounts, sessions, API tokens and password resets.

Separate from ``tenancy`` because it answers a different question. That module knows
*who belongs to which workspace and with what role*; this one knows *whether the
person making this request is who they say they are*.

Three credential stores, deliberately separate:

* **sessions** -- browsers. Short-lived, cleared on logout, and now *scoped*: a
  session issued to an account with a temporary password can reach the
  password-change endpoint and nothing else.
* **api_tokens** -- the CLI and ``vanna mcp``, which cannot hold a cookie. Revoking
  a laptop must not sign out a scheduled job, and the two want very different
  lifetimes.
* **password_resets** -- single-use, one hour, hashed like the others.

All three store the **SHA-256 of the token, never the token**, so a dump of these
tables yields nothing replayable.

Four defects fixed here, each small and each with real consequences:

1. ``must_change`` was advisory. The API returned a flag and issued a full 72-hour
   session; only the browser honoured it. A temporary password was a permanent
   credential for anything speaking HTTP directly.
2. Changing a password did not end other sessions. Resetting the password of a
   compromised account left the attacker signed in for up to three days.
3. ``reset_account`` called ``create()`` with no name, and the upsert's
   ``full_name = EXCLUDED.full_name`` overwrote the real name with ``''``. Every
   password reset silently erased the user's name.
4. There was no recovery path at all: a forgotten password needed a platform admin.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .db import SCHEMA
from .tenancy import _iso

logger = logging.getLogger("vanna.accounts")

#: A session that may only be used to set a new password.
SCOPE_PASSWORD_CHANGE = "password_change_only"
SCOPE_FULL = "full"

#: How long a reset link lives. Long enough to arrive and be clicked, short enough
#: that a mailbox compromised next week is not a way in.
RESET_TTL_MINUTES = 60


class Accounts:
    """Credential storage and verification."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def count(self) -> int:
        return int(await self.db.fetch_value(f"SELECT count(*) FROM {SCHEMA}.users", default=0))

    async def get(self, email: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.users WHERE email = %s", ((email or "").strip().lower(),)
        )

    async def list_all(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT email, full_name, is_active, must_change, auth_provider,
                       created_at, last_login_at, password_changed_at
                  FROM {SCHEMA}.users ORDER BY email"""
        )
        for row in rows:
            for key in ("created_at", "last_login_at", "password_changed_at"):
                row[key] = _iso(row[key])
        return rows

    async def create(
        self,
        email: str,
        password: str,
        *,
        full_name: str = "",
        must_change: bool = False,
        auth_provider: str = "password",
        external_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create an account, or update the one that exists.

        ``full_name`` is only overwritten when a non-empty one is supplied. The
        previous version always overwrote, so the admin password-reset path -- which
        passes no name -- erased it.
        """
        from vanna.core.auth import hash_password

        email = (email or "").strip().lower()
        if "@" not in email:
            raise ValueError("A valid email address is required")

        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.users
                    (email, password_hash, full_name, must_change, auth_provider,
                     external_id, password_changed_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (email) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        full_name     = CASE
                                            WHEN EXCLUDED.full_name = ''
                                            THEN {SCHEMA}.users.full_name
                                            ELSE EXCLUDED.full_name
                                        END,
                        must_change   = EXCLUDED.must_change,
                        auth_provider = EXCLUDED.auth_provider,
                        external_id   = coalesce(EXCLUDED.external_id, {SCHEMA}.users.external_id),
                        password_changed_at = now(),
                        is_active     = true""",
            (
                email,
                hash_password(password) if password else "",
                full_name,
                must_change,
                auth_provider,
                external_id,
            ),
        )
        return await self.get(email)  # type: ignore[return-value]

    async def upsert_external(
        self, email: str, *, full_name: str = "", provider: str, external_id: str
    ) -> Dict[str, Any]:
        """Record an identity that authenticates elsewhere (OIDC).

        ``password_hash`` stays empty, and ``verify_password`` returns False for an
        empty hash, so an external account can never be signed into with a password.
        """
        email = (email or "").strip().lower()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.users
                    (email, password_hash, full_name, auth_provider, external_id)
                VALUES (%s, '', %s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET full_name   = CASE
                                          WHEN EXCLUDED.full_name = ''
                                          THEN {SCHEMA}.users.full_name
                                          ELSE EXCLUDED.full_name
                                      END,
                        external_id = EXCLUDED.external_id,
                        is_active   = true""",
            (email, full_name, provider, external_id),
        )
        return await self.get(email)  # type: ignore[return-value]

    async def set_password(
        self, email: str, password: str, *, keep_token: str = "", revoke_tokens: bool = True
    ) -> bool:
        """Set a new password and invalidate what the old one could reach.

        Every other session ends, and API tokens are revoked unless the caller opts
        out. Leaving them alive means resetting the password of a compromised
        account does not actually lock anyone out -- which is the only reason
        anybody resets a password in a hurry.
        """
        from vanna.core.auth import hash_password, hash_token

        email = (email or "").strip().lower()
        changed = bool(
            await self.db.execute(
                f"""UPDATE {SCHEMA}.users
                       SET password_hash = %s,
                           must_change = false,
                           password_changed_at = now()
                     WHERE email = %s""",
                (hash_password(password), email),
            )
        )
        if not changed:
            return False

        keep = hash_token(keep_token) if keep_token else ""
        ended = await self.db.execute(
            f"DELETE FROM {SCHEMA}.sessions WHERE email = %s AND token_hash <> %s",
            (email, keep),
        )
        revoked = 0
        if revoke_tokens:
            revoked = await self.db.execute(
                f"DELETE FROM {SCHEMA}.api_tokens WHERE email = %s", (email,)
            )
        # Any outstanding reset links are now meaningless, and leaving them usable
        # would let an old email re-take the account.
        await self.db.execute(
            f"UPDATE {SCHEMA}.password_resets SET used_at = now() "
            f"WHERE email = %s AND used_at IS NULL",
            (email,),
        )
        logger.info(
            "Password changed for %s (ended %d session(s), revoked %d token(s))",
            email, ended, revoked,
        )
        return True

    async def set_temporary_password(self, email: str, password: str) -> bool:
        """Issue a password that must be replaced on first use.

        Unlike ``create``, this touches nothing but the credential -- the account's
        name, provider and membership are none of a password reset's business.
        """
        from vanna.core.auth import hash_password

        email = (email or "").strip().lower()
        changed = bool(
            await self.db.execute(
                f"""UPDATE {SCHEMA}.users
                       SET password_hash = %s, must_change = true,
                           password_changed_at = now()
                     WHERE email = %s""",
                (hash_password(password), email),
            )
        )
        if changed:
            # A temporary password is issued because the old one is not trusted.
            await self.db.execute(f"DELETE FROM {SCHEMA}.sessions WHERE email = %s", (email,))
            await self.db.execute(f"DELETE FROM {SCHEMA}.api_tokens WHERE email = %s", (email,))
        return changed

    async def set_active(self, email: str, active: bool) -> bool:
        """Enable or disable an account.

        Disabling ends every live session and revokes every token. Leaving them
        would mean the account keeps working until each expires, which is not what
        anyone means by "disabled".
        """
        email = (email or "").strip().lower()
        changed = bool(
            await self.db.execute(
                f"UPDATE {SCHEMA}.users SET is_active = %s WHERE email = %s", (active, email)
            )
        )
        if changed and not active:
            await self.db.execute(f"DELETE FROM {SCHEMA}.sessions WHERE email = %s", (email,))
            await self.db.execute(f"DELETE FROM {SCHEMA}.api_tokens WHERE email = %s", (email,))
        return changed

    async def verify(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """Check a password. Returns the account, or None.

        When the account does not exist it still burns a verification. Returning
        early would make a failed login measurably faster for an unknown address
        than for a known one, which turns this into a user-enumeration oracle.
        """
        from vanna.core.auth import verify_password, waste_time

        user = await self.get(email)
        if user is None or not user.get("is_active"):
            waste_time()
            return None

        # An account that authenticates elsewhere has no password to check. Burn the
        # time anyway so "this address uses SSO" is not observable from timing.
        if (user.get("auth_provider") or "password") != "password":
            waste_time()
            return None

        if not verify_password(password, user.get("password_hash") or ""):
            return None
        return user

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(
        self,
        email: str,
        *,
        ttl_hours: int = 72,
        user_agent: str = "",
        ip: str = "",
        scope: str = SCOPE_FULL,
    ) -> str:
        """Issue a session and return the token. Only its hash is stored."""
        from vanna.core.auth import generate_token, hash_token

        token = generate_token()
        email = (email or "").strip().lower()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.sessions
                    (token_hash, email, expires_at, user_agent, ip, scope)
                VALUES (%s, %s, now() + make_interval(hours => %s), %s, %s, %s)""",
            (hash_token(token), email, ttl_hours, user_agent[:300], ip, scope),
        )
        await self.db.execute(
            f"UPDATE {SCHEMA}.users SET last_login_at = now() WHERE email = %s", (email,)
        )
        return token

    async def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        """The account behind a session token, or None.

        Expiry and the account's active flag are both conditions of the query, so
        disabling an account cannot leave a valid-looking session working. The
        session's ``scope`` rides along so the caller can enforce it.
        """
        from vanna.core.auth import hash_token

        row = await self.db.fetch_one(
            f"""SELECT u.*, s.scope AS session_scope
                  FROM {SCHEMA}.sessions s
                  JOIN {SCHEMA}.users u ON u.email = s.email
                 WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active""",
            (hash_token(token),),
        )
        return row

    async def delete_session(self, token: str) -> bool:
        from vanna.core.auth import hash_token

        return bool(
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.sessions WHERE token_hash = %s", (hash_token(token),)
            )
        )

    async def purge_expired(self) -> int:
        """Housekeeping for everything that expires."""
        sessions = await self.db.execute(
            f"DELETE FROM {SCHEMA}.sessions WHERE expires_at <= now()"
        )
        await self.db.execute(
            f"DELETE FROM {SCHEMA}.password_resets "
            f"WHERE expires_at <= now() - interval '7 days'"
        )
        await self.db.execute(
            f"DELETE FROM {SCHEMA}.api_tokens "
            f"WHERE expires_at IS NOT NULL AND expires_at <= now()"
        )
        return sessions

    async def list_sessions(self, email: str, *, current_token: str = "") -> List[Dict[str, Any]]:
        """This account's live sessions, newest first.

        The token hash is compared here rather than returned: the caller needs to
        know which row is the browser it is talking to, and the only safe way to
        answer that is to hash the cookie we were given and match it.
        """
        from vanna.core.auth import hash_token

        rows = await self.db.fetch_all(
            f"""SELECT token_hash, user_agent, ip, created_at, expires_at, scope
                  FROM {SCHEMA}.sessions
                 WHERE email = %s AND expires_at > now()
                 ORDER BY created_at DESC""",
            ((email or "").strip().lower(),),
        )

        current_hash = hash_token(current_token) if current_token else ""
        return [
            {
                # Eight hex characters of a SHA-256 identify a row among an
                # account's handful of sessions and are useless for anything else.
                "id": row["token_hash"][:8],
                "user_agent": row["user_agent"] or "",
                "ip": row["ip"] or "",
                "created_at": _iso(row["created_at"]),
                "expires_at": _iso(row["expires_at"]),
                "scope": row["scope"],
                "is_current": row["token_hash"] == current_hash,
            }
            for row in rows
        ]

    async def delete_session(
        self, email: str, session_id: str, *, keep_token: str = ""
    ) -> int:
        """End one named session. Returns 1 if it went, 0 if there was nothing to end.

        ``session_id`` is the eight-character prefix ``list_sessions`` hands out, so
        the match is on the prefix -- the full hash is never given to a client, by
        design. Always scoped by ``email``: the prefix identifies a row among *this*
        account's handful of sessions and is not an authorisation to touch anyone
        else's, however it was obtained.

        Refuses the caller's own session. Killing it would be a sign-out, and one
        that leaves the page believing it is still signed in; ``/auth/logout`` is
        that operation and does the rest of the work.
        """
        from vanna.core.auth import hash_token

        clean = (session_id or "").strip().lower()
        if len(clean) != 8 or not all(c in "0123456789abcdef" for c in clean):
            return 0
        keep = hash_token(keep_token) if keep_token else ""
        return await self.db.execute(
            f"""DELETE FROM {SCHEMA}.sessions
                 WHERE email = %s AND left(token_hash, 8) = %s AND token_hash <> %s""",
            ((email or "").strip().lower(), clean, keep),
        )

    async def delete_other_sessions(self, email: str, *, keep_token: str = "") -> int:
        """Sign out everywhere else. Returns how many were ended."""
        from vanna.core.auth import hash_token

        keep = hash_token(keep_token) if keep_token else ""
        return await self.db.execute(
            f"DELETE FROM {SCHEMA}.sessions WHERE email = %s AND token_hash <> %s",
            ((email or "").strip().lower(), keep),
        )

    # ------------------------------------------------------------------
    # API tokens
    # ------------------------------------------------------------------

    async def create_token(
        self, email: str, *, name: str = "", ttl_days: Optional[int] = None
    ) -> str:
        """Issue an API token. Returned once; no endpoint can show it again."""
        from vanna.core.auth import generate_token, hash_token

        token = generate_token()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.api_tokens (token_hash, email, name, expires_at)
                VALUES (%s, %s, %s,
                        CASE WHEN %s::int IS NULL THEN NULL
                             ELSE now() + make_interval(days => %s::int) END)""",
            (hash_token(token), (email or "").strip().lower(), name, ttl_days, ttl_days),
        )
        return token

    async def token_user(self, token: str) -> Optional[Dict[str, Any]]:
        from vanna.core.auth import hash_token

        digest = hash_token(token)
        user = await self.db.fetch_one(
            f"""SELECT u.* FROM {SCHEMA}.api_tokens t
                  JOIN {SCHEMA}.users u ON u.email = t.email
                 WHERE t.token_hash = %s
                   AND (t.expires_at IS NULL OR t.expires_at > now())
                   AND u.is_active""",
            (digest,),
        )
        if user is not None:
            try:
                await self.db.execute(
                    f"UPDATE {SCHEMA}.api_tokens SET last_used_at = now() WHERE token_hash = %s",
                    (digest,),
                )
            except Exception as exc:  # pragma: no cover - bookkeeping only
                logger.debug("Could not stamp token use: %s", exc)
        return user

    async def list_tokens(self, email: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT token_hash, name, created_at, expires_at, last_used_at
                  FROM {SCHEMA}.api_tokens WHERE email = %s ORDER BY created_at DESC""",
            ((email or "").strip().lower(),),
        )
        for row in rows:
            # A handle for revocation that is neither the token nor its full hash.
            row["id"] = row.pop("token_hash")[:12]
            for key in ("created_at", "expires_at", "last_used_at"):
                row[key] = _iso(row[key])
        return rows

    async def revoke_token(self, email: str, token_id: str) -> bool:
        return bool(
            await self.db.execute(
                f"""DELETE FROM {SCHEMA}.api_tokens
                     WHERE email = %s AND left(token_hash, 12) = %s""",
                ((email or "").strip().lower(), token_id),
            )
        )

    # ------------------------------------------------------------------
    # Password reset
    # ------------------------------------------------------------------

    async def create_reset(self, email: str, *, ip: str = "") -> Optional[str]:
        """Issue a reset token, or None when the address cannot receive one.

        Returns None for an unknown, disabled, or externally-authenticated account.
        The caller must answer identically in every case -- the point of a reset
        endpoint that reveals nothing is defeated by a caller that branches on it.
        """
        from vanna.core.auth import generate_token, hash_token

        user = await self.get(email)
        if user is None or not user.get("is_active"):
            return None
        if (user.get("auth_provider") or "password") != "password":
            return None

        token = generate_token()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.password_resets
                    (token_hash, email, expires_at, requested_ip)
                VALUES (%s, %s, now() + make_interval(mins => %s), %s)""",
            (hash_token(token), user["email"], RESET_TTL_MINUTES, ip),
        )
        return token

    async def redeem_reset(self, token: str, new_password: str) -> Optional[str]:
        """Consume a reset token and set the password. Returns the email, or None.

        The update is conditional on ``used_at IS NULL`` and the row is claimed in
        the same statement, so two requests racing with the same link cannot both
        succeed.
        """
        from vanna.core.auth import hash_token

        row = await self.db.fetch_one(
            f"""UPDATE {SCHEMA}.password_resets
                   SET used_at = now()
                 WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
                 RETURNING email""",
            (hash_token(token),),
        )
        if not row:
            return None

        email = row["email"]
        # Ends every session and revokes every token: the person resetting is
        # assumed to be locking somebody else out.
        await self.set_password(email, new_password)
        logger.warning("Password reset redeemed for %s", email)
        return email


async def seed_first_admin(
    accounts: Accounts, *, admin_emails: List[str], password: str = ""
) -> Optional[str]:
    """Give the first platform admin a password, on an empty install.

    Returns the password when one was generated, so the caller can log it once.
    Only ever runs when there are no accounts at all, so a deliberately deleted
    admin stays deleted.
    """
    if await accounts.count() > 0:
        return None

    email = (admin_emails or ["demo@example.com"])[0]
    generated = ""
    if not password:
        from vanna.core.auth import generate_password

        password = generate_password()
        generated = password

    await accounts.create(
        email, password, full_name="Administrator", must_change=bool(generated)
    )
    logger.info("Seeded the first account: %s", email)
    return generated
