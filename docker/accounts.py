"""Accounts, sessions and API tokens.

Split from ``tenancy.py`` because it answers a different question. That module knows
*who belongs to which workspace and with what role*; this one knows *whether the person
making this request is who they say they are*. Keeping them apart is also why the schema
does not duplicate anything: ``tenant_users`` stays the membership table and ``users``
holds only credentials, so one account can belong to several workspaces with a different
role in each.

Three token stores, deliberately separate:

* **sessions** -- browsers. Short-lived, cleared on logout.
* **api_tokens** -- the CLI and ``vanna mcp``, which cannot hold a cookie. Revoking a
  laptop must not sign out a scheduled job, and the two want very different lifetimes.

Both store the **SHA-256 of the token, never the token**. A dump of these tables yields
nothing replayable -- unlike SQL Chat, whose session lookup is keyed on the raw value
(``prisma.session.findUnique({where: {sessionToken: token}})``) and hands over every live
session with the database.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tenancy import SCHEMA, AppDatabase, _iso

logger = logging.getLogger("vanna.accounts")


class Accounts:
    """Credential storage and verification."""

    def __init__(self, db: AppDatabase) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def count(self) -> int:
        row = await self.db.fetch_one(f"SELECT count(*) AS n FROM {SCHEMA}.users")
        return int(row["n"]) if row else 0

    async def get(self, email: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.users WHERE email = %s", (email.strip().lower(),)
        )

    async def list_all(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT email, full_name, is_active, must_change, created_at, last_login_at
                  FROM {SCHEMA}.users ORDER BY email"""
        )
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
            row["last_login_at"] = _iso(row["last_login_at"])
        return rows

    async def create(
        self,
        email: str,
        password: str,
        *,
        full_name: str = "",
        must_change: bool = False,
    ) -> Dict[str, Any]:
        from vanna.core.auth import hash_password

        email = email.strip().lower()
        if "@" not in email:
            raise ValueError("A valid email address is required")

        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.users (email, password_hash, full_name, must_change)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        full_name     = EXCLUDED.full_name,
                        must_change   = EXCLUDED.must_change,
                        is_active     = true""",
            (email, hash_password(password), full_name, must_change),
        )
        return await self.get(email)  # type: ignore[return-value]

    async def set_password(self, email: str, password: str) -> bool:
        from vanna.core.auth import hash_password

        return bool(
            await self.db.execute(
                f"""UPDATE {SCHEMA}.users
                       SET password_hash = %s, must_change = false
                     WHERE email = %s""",
                (hash_password(password), email.strip().lower()),
            )
        )

    async def set_active(self, email: str, active: bool) -> bool:
        """Enable or disable an account.

        Disabling ends every live session and revokes every token. Leaving them would
        mean the account keeps working until each expires, which is not what anyone
        means by "disabled".
        """
        email = email.strip().lower()
        changed = bool(
            await self.db.execute(
                f"UPDATE {SCHEMA}.users SET is_active = %s WHERE email = %s",
                (active, email),
            )
        )
        if changed and not active:
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.sessions WHERE email = %s", (email,)
            )
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.api_tokens WHERE email = %s", (email,)
            )
        return changed

    async def verify(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """Check a password. Returns the account, or None.

        When the account does not exist it still burns a verification. Returning early
        would make a failed login measurably faster for an unknown address than for a
        known one, which turns this into a user-enumeration oracle -- and the member
        list is exactly what someone wants before trying passwords.
        """
        from vanna.core.auth import verify_password, waste_time

        user = await self.get(email)
        if user is None or not user.get("is_active"):
            waste_time()
            return None

        if not verify_password(password, user.get("password_hash") or ""):
            return None
        return user

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(
        self, email: str, *, ttl_hours: int = 72, user_agent: str = "", ip: str = ""
    ) -> str:
        """Issue a session and return the token. Only its hash is stored."""
        from vanna.core.auth import generate_token, hash_token

        token = generate_token()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.sessions
                    (token_hash, email, expires_at, user_agent, ip)
                VALUES (%s, %s, now() + make_interval(hours => %s), %s, %s)""",
            (hash_token(token), email.strip().lower(), ttl_hours, user_agent[:300], ip),
        )
        await self.db.execute(
            f"UPDATE {SCHEMA}.users SET last_login_at = now() WHERE email = %s",
            (email.strip().lower(),),
        )
        return token

    async def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        """The account behind a session token, or None.

        Expiry and the account's active flag are both conditions of the query, so
        disabling an account cannot leave a valid-looking session working.
        """
        from vanna.core.auth import hash_token

        return await self.db.fetch_one(
            f"""SELECT u.* FROM {SCHEMA}.sessions s
                  JOIN {SCHEMA}.users u ON u.email = s.email
                 WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active""",
            (hash_token(token),),
        )

    async def delete_session(self, token: str) -> bool:
        from vanna.core.auth import hash_token

        return bool(
            await self.db.execute(
                f"DELETE FROM {SCHEMA}.sessions WHERE token_hash = %s",
                (hash_token(token),),
            )
        )

    async def purge_expired(self) -> int:
        return await self.db.execute(
            f"DELETE FROM {SCHEMA}.sessions WHERE expires_at <= now()"
        )

    async def list_sessions(
        self, email: str, *, current_token: str = ""
    ) -> List[Dict[str, Any]]:
        """This account's live sessions, newest first.

        The token hash is compared here rather than returned: the caller needs to know
        which row is the browser it is talking to, and the only safe way to answer that
        is to hash the cookie we were given and match it. Nothing derived from a token
        ever goes back out -- the row identifies itself by user agent and age.
        """
        from vanna.core.auth import hash_token

        rows = await self.db.fetch_all(
            f"""SELECT token_hash, user_agent, ip, created_at, expires_at
                  FROM {SCHEMA}.sessions
                 WHERE email = %s AND expires_at > now()
                 ORDER BY created_at DESC""",
            (email.strip().lower(),),
        )

        current_hash = hash_token(current_token) if current_token else ""
        sessions = []
        for row in rows:
            sessions.append(
                {
                    # A short, non-reversible label so the UI has something to key on.
                    # Eight hex characters of a SHA-256 identify a row among an
                    # account's handful of sessions and are useless for anything else.
                    "id": row["token_hash"][:8],
                    "user_agent": row["user_agent"] or "",
                    "ip": row["ip"] or "",
                    "created_at": _iso(row["created_at"]),
                    "expires_at": _iso(row["expires_at"]),
                    "is_current": row["token_hash"] == current_hash,
                }
            )
        return sessions

    async def delete_other_sessions(self, email: str, *, keep_token: str = "") -> int:
        """Sign out everywhere else. Returns how many were ended.

        Keeping the caller's own session is the point: someone who has just discovered a
        session they do not recognise should not have to sign in again to act on it, and
        being logged out by your own security action reads as the action failing.
        """
        from vanna.core.auth import hash_token

        keep = hash_token(keep_token) if keep_token else ""
        return await self.db.execute(
            f"DELETE FROM {SCHEMA}.sessions WHERE email = %s AND token_hash <> %s",
            (email.strip().lower(), keep),
        )

    # ------------------------------------------------------------------
    # API tokens
    # ------------------------------------------------------------------

    async def create_token(
        self, email: str, *, name: str = "", ttl_days: Optional[int] = None
    ) -> str:
        """Issue an API token. Returned once; there is no endpoint that shows it again."""
        from vanna.core.auth import generate_token, hash_token

        token = generate_token()
        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.api_tokens (token_hash, email, name, expires_at)
                VALUES (%s, %s, %s,
                        CASE WHEN %s::int IS NULL THEN NULL
                             ELSE now() + make_interval(days => %s::int) END)""",
            (hash_token(token), email.strip().lower(), name, ttl_days, ttl_days),
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
                    f"""UPDATE {SCHEMA}.api_tokens SET last_used_at = now()
                         WHERE token_hash = %s""",
                    (digest,),
                )
            except Exception as exc:  # pragma: no cover - bookkeeping only
                logger.debug("Could not stamp token use: %s", exc)
        return user

    async def list_tokens(self, email: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT token_hash, name, created_at, expires_at, last_used_at
                  FROM {SCHEMA}.api_tokens WHERE email = %s ORDER BY created_at DESC""",
            (email.strip().lower(),),
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
                (email.strip().lower(), token_id),
            )
        )


async def seed_first_admin(
    accounts: Accounts, *, admin_emails: List[str], password: str = ""
) -> Optional[str]:
    """Give the first platform admin a password, on an empty install.

    Returns the password when one was generated, so the caller can log it once.

    Only ever runs when there are no accounts at all. A deployment that has been set up
    must not have a seeded account reappear underneath it, and the check being "no users"
    rather than "this user is missing" is what guarantees a deliberately deleted admin
    stays deleted.
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
