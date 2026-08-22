"""Quota, rate limiting and login throttling, shared across every worker.

The library ships in-memory versions of the first two and states plainly in their
docstrings that the counts are per process. That is honest and it is also a hard
ceiling: two uvicorn workers enforcing "200 questions per day" independently permit
400, so the deployment could only ever run one process. Everything else about this
stack scales horizontally; the counters were what stopped it.

These are the same hooks with the dictionary replaced by one table and one
statement::

    INSERT INTO counters (bucket_key, window_start, count) VALUES (…, 1)
    ON CONFLICT (bucket_key, window_start)
    DO UPDATE SET count = counters.count + 1, updated_at = now()
    RETURNING count

One round trip, atomic, no read-modify-write race between workers. The window start
is computed **in the database** rather than in Python, so workers with skewed clocks
still agree on which bucket they are incrementing.

Fixed windows, not sliding. A sliding window needs the individual event timestamps,
which is a row per request; the boundary effect a fixed window permits -- briefly up
to twice the limit across an edge -- is acceptable for a daily cap and a per-minute
burst guard, and is bounded for the login throttle by its short window.

**Behaviour when the control plane is unreachable** differs per limiter, on purpose:

* *quota* fails **closed**. It bounds spend, and spend is the thing that cannot be
  undone after the fact.
* *rate limiting* fails **open**. It bounds load, and converting a database blip
  into a total refusal of service is worse than the load it was guarding against.
* *login throttling* fails **closed**. A login cannot succeed without the control
  plane anyway, so refusing costs nothing real and keeps the throttle from being
  bypassable by making the database unhappy.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Optional, Tuple

from vanna.core.lifecycle import LifecycleHook
from vanna.core.lifecycle.quota import QuotaExceededError, RateLimitExceededError

from .db import SCHEMA

logger = logging.getLogger("vanna.limits")


class Counters:
    """Fixed-window counters in the control plane."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _window_sql() -> str:
        """SQL for the start of the current window of ``%s`` seconds.

        Floor of epoch/width, in the database, so every worker lands on the same
        boundary regardless of its own clock.
        """
        return "to_timestamp(floor(extract(epoch from now()) / %s) * %s)"

    async def hit(self, key: str, window_seconds: int) -> int:
        """Record one event and return the running count for this window."""
        row = await self.db.fetch_one(
            f"""INSERT INTO {SCHEMA}.counters (bucket_key, window_start, count)
                VALUES (%s, {self._window_sql()}, 1)
                ON CONFLICT (bucket_key, window_start)
                DO UPDATE SET count = {SCHEMA}.counters.count + 1,
                              updated_at = now()
                RETURNING count""",
            (key, window_seconds, window_seconds),
        )
        return int((row or {}).get("count") or 0)

    async def peek(self, key: str, window_seconds: int) -> int:
        """The count so far, without recording anything."""
        row = await self.db.fetch_one(
            f"""SELECT count FROM {SCHEMA}.counters
                 WHERE bucket_key = %s AND window_start = {self._window_sql()}""",
            (key, window_seconds, window_seconds),
        )
        return int((row or {}).get("count") or 0)

    async def clear(self, key: str) -> int:
        """Forget every window for one key -- used after a successful login."""
        return await self.db.execute(
            f"DELETE FROM {SCHEMA}.counters WHERE bucket_key = %s", (key,)
        )

    async def resets_in(self, window_seconds: int) -> int:
        """Seconds until the current window rolls over."""
        row = await self.db.fetch_one(
            f"""SELECT ceil(extract(epoch from
                    ({self._window_sql()} + make_interval(secs => %s)) - now()
                ))::int AS seconds""",
            (window_seconds, window_seconds, window_seconds),
        )
        return max(1, int((row or {}).get("seconds") or window_seconds))

    async def purge(self, older_than_seconds: int = 172_800) -> int:
        """Drop windows nothing will read again. Called from the startup job."""
        return await self.db.execute(
            f"""DELETE FROM {SCHEMA}.counters
                 WHERE window_start < now() - make_interval(secs => %s)""",
            (older_than_seconds,),
        )


def _hours(seconds: int) -> str:
    hours = max(1, math.ceil(seconds / 3600))
    return f"{hours} hour{'s' if hours != 1 else ''}"


class PostgresQuotaHook(LifecycleHook):
    """Caps questions per workspace over a rolling window, across all workers.

    Counted **per tenant**, matching how the limit is sold and how the usage screen
    reports it. The in-memory hook defaulted to per-user, which disagreed with both.

    Two exemptions, and neither is the one the original had:

    * ``exempt_metadata_flag`` -- a caller answering on their own LLM key is not
      spending the workspace's budget. The generation is still recorded.
    * ``exempt_groups`` -- **empty by default**. The library hook exempts ``admin``,
      and the application never overrode it, so every tenant admin had unlimited
      quota *and* no rate limit. An admin is a person asking questions like anybody
      else; if a workspace needs a higher ceiling, that is what plans and overrides
      are for.
    """

    def __init__(
        self,
        counters: Counters,
        *,
        max_messages: int,
        window_seconds: int = 86_400,
        exempt_groups: Tuple[str, ...] = (),
        exempt_metadata_flag: Optional[str] = None,
        fail_closed: bool = True,
    ) -> None:
        self.counters = counters
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self.exempt_groups = set(exempt_groups)
        self.exempt_metadata_flag = exempt_metadata_flag
        self.fail_closed = fail_closed

    def _key(self, user: Any) -> str:
        return f"quota:tenant:{getattr(user, 'tenant_id', 'default')}"

    def _exempt(self, user: Any) -> bool:
        if self.exempt_groups & set(getattr(user, "group_memberships", None) or []):
            return True
        flag = self.exempt_metadata_flag
        return bool(flag and (getattr(user, "metadata", None) or {}).get(flag))

    async def usage(self, user: Any) -> Tuple[int, int]:
        """``(used, limit)`` for display. Never raises."""
        try:
            return await self.counters.peek(self._key(user), self.window_seconds), self.max_messages
        except Exception:
            return 0, self.max_messages

    async def before_message(self, user: Any, message: str) -> Optional[str]:
        if self._exempt(user):
            return None

        try:
            used = await self.counters.hit(self._key(user), self.window_seconds)
        except Exception as exc:
            logger.error("Quota counter unavailable: %s", exc)
            if self.fail_closed:
                raise QuotaExceededError(
                    "Usage limits cannot be checked right now, so this request was "
                    "not sent. Try again in a moment."
                ) from exc
            return None

        if used > self.max_messages:
            resets_in = await self._safe_reset()
            logger.info(
                "Quota exceeded tenant=%s used=%d limit=%d",
                getattr(user, "tenant_id", "?"), used, self.max_messages,
            )
            raise QuotaExceededError(
                f"This workspace has used all {self.max_messages} of its requests "
                f"for the current period. More become available in about "
                f"{_hours(resets_in)}."
            )
        return None

    async def _safe_reset(self) -> int:
        try:
            return await self.counters.resets_in(self.window_seconds)
        except Exception:
            return self.window_seconds


class PostgresRateLimitHook(LifecycleHook):
    """Limits requests per user over a short window, across all workers.

    Distinct from quota, and both are wanted: quota bounds cost over a day, this
    bounds load over a minute. A user with plenty of quota can still saturate a
    warehouse connection pool by holding down enter.

    Fails **open**. A per-minute burst guard that turns a control-plane blip into a
    total outage has inverted its own cost/benefit -- the SQL runner's own timeout
    and row cap are still in force underneath it.
    """

    def __init__(
        self,
        counters: Counters,
        *,
        max_requests: int,
        window_seconds: int = 60,
        exempt_groups: Tuple[str, ...] = (),
    ) -> None:
        self.counters = counters
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.exempt_groups = set(exempt_groups)

    async def before_message(self, user: Any, message: str) -> Optional[str]:
        if self.exempt_groups & set(getattr(user, "group_memberships", None) or []):
            return None

        key = f"rate:{getattr(user, 'tenant_id', 'default')}:{getattr(user, 'id', '')}"
        try:
            used = await self.counters.hit(key, self.window_seconds)
        except Exception as exc:
            logger.error("Rate-limit counter unavailable, allowing request: %s", exc)
            return None

        if used > self.max_requests:
            try:
                wait = await self.counters.resets_in(self.window_seconds)
            except Exception:
                wait = self.window_seconds
            logger.info("Rate limit hit key=%s", key)
            raise RateLimitExceededError(
                f"Too many requests. Please wait {wait} second"
                f"{'s' if wait != 1 else ''} before asking again."
            )
        return None


class LoginThrottle:
    """Failed-login budget, per address and per client IP.

    Both keys, so one attacker cannot exhaust a single account's budget to lock out
    its owner, and cannot spread attempts across accounts to evade the per-IP limit.

    Replaces an unbounded ``defaultdict(deque)`` that was per process on both
    counts: with several workers the effective limit was the limit times the worker
    count, and the dictionary grew forever because entries were only removed on a
    *successful* login.
    """

    def __init__(
        self,
        counters: Optional[Counters],
        *,
        max_attempts: int,
        window_seconds: int,
    ) -> None:
        self.counters = counters
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        # Demo mode, or any deployment with no control plane, keeps the old
        # behaviour rather than having no throttle at all.
        self._local: Dict[str, Tuple[float, int]] = {}

    def _keys(self, email: str, ip: str) -> Tuple[str, ...]:
        keys = [f"login:email:{email}"]
        if ip:
            keys.append(f"login:ip:{ip}")
        return tuple(keys)

    async def blocked(self, email: str, ip: str) -> bool:
        if self.counters is None:
            return any(self._local_count(k) >= self.max_attempts for k in self._keys(email, ip))
        try:
            for key in self._keys(email, ip):
                if await self.counters.peek(key, self.window_seconds) >= self.max_attempts:
                    return True
            return False
        except Exception as exc:
            # Fail closed: a login cannot succeed without the control plane anyway,
            # so refusing costs nothing and keeps the throttle from being bypassed
            # by making the database unhappy.
            logger.error("Login throttle unavailable, refusing: %s", exc)
            return True

    async def record_failure(self, email: str, ip: str) -> None:
        if self.counters is None:
            for key in self._keys(email, ip):
                self._local_hit(key)
            return
        try:
            for key in self._keys(email, ip):
                await self.counters.hit(key, self.window_seconds)
        except Exception as exc:  # pragma: no cover - already refused above
            logger.error("Could not record a failed login: %s", exc)

    async def clear(self, email: str) -> None:
        """Forget an address's failures after it authenticates.

        Only the address, never the IP: a shared office address that has just had
        one successful login should not have its budget reset for whoever else is
        working through a password list from the same network.
        """
        if self.counters is None:
            self._local.pop(f"login:email:{email}", None)
            return
        try:
            await self.counters.clear(f"login:email:{email}")
        except Exception as exc:  # pragma: no cover
            logger.debug("Could not clear login counter: %s", exc)

    # -- in-process fallback -------------------------------------------

    def _window(self) -> float:
        return math.floor(time.time() / self.window_seconds) * self.window_seconds

    def _local_count(self, key: str) -> int:
        window, count = self._local.get(key, (0.0, 0))
        return count if window == self._window() else 0

    def _local_hit(self, key: str) -> None:
        now = self._window()
        window, count = self._local.get(key, (0.0, 0))
        self._local[key] = (now, count + 1 if window == now else 1)
        # Bounded, unlike its predecessor: drop stale entries whenever the table
        # grows past a size no legitimate deployment reaches.
        if len(self._local) > 10_000:
            self._local = {
                k: v for k, v in self._local.items() if v[0] == now
            }


def build_limit_hooks(
    counters: Optional[Counters],
    *,
    daily_quota: int,
    rate_limit_per_min: int,
) -> list:
    """The lifecycle hooks for one tenant's agent.

    Falls back to the library's in-memory hooks when there is no control plane,
    which is the demo path -- with the admin exemption removed in both cases.
    """
    if counters is None:
        from vanna.core.lifecycle import InMemoryQuotaHook, RateLimitHook

        return [
            InMemoryQuotaHook(
                max_messages=daily_quota,
                per_tenant=True,
                exempt_groups=(),
                exempt_metadata_flag="byo_key",
            ),
            RateLimitHook(max_requests=rate_limit_per_min, exempt_groups=()),
        ]

    return [
        PostgresQuotaHook(
            counters,
            max_messages=daily_quota,
            exempt_metadata_flag="byo_key",
        ),
        PostgresRateLimitHook(counters, max_requests=rate_limit_per_min),
    ]
