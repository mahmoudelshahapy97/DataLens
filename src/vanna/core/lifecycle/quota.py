"""Quota and rate limiting as lifecycle hooks.

``LifecycleHook`` has been an interface with no implementations, so the two
things every hosted deployment needs -- a per-user cap and a burst limiter --
were left as an exercise. These are those.

Both refuse by **raising**, not by returning a modified message. A hook that
quietly rewrites an over-quota question into "you are over quota" would send
that text to the model as if the user had typed it, and the model would try to
answer it. Refusal has to stop the request.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Deque, Dict, Optional

from .base import LifecycleHook

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..user.models import User

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """Raised when a user has exhausted their allowance."""


class RateLimitExceededError(Exception):
    """Raised when a user is sending requests too quickly."""


class InMemoryQuotaHook(LifecycleHook):
    """Caps messages per user over a rolling window.

    In-memory, so counts are **per process**: two workers each enforce the
    limit independently and the effective cap is the limit times the worker
    count. Fine for a single-process deployment or a soft guard; back it with
    Redis for a real one. Said plainly here because a quota that silently
    permits several times its stated limit is worse than no quota -- someone
    will rely on the number.

    Args:
        max_messages: Allowance per window.
        window_seconds: Rolling window. Default 24 hours.
        exempt_groups: Groups the limit does not apply to.
        per_tenant: Count per tenant instead of per user, for plan-level caps.
    """

    def __init__(
        self,
        *,
        max_messages: int = 100,
        window_seconds: int = 86_400,
        exempt_groups: tuple = ("admin",),
        per_tenant: bool = False,
    ) -> None:
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self.exempt_groups = set(exempt_groups)
        self.per_tenant = per_tenant
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    def _key(self, user: "User") -> str:
        if self.per_tenant:
            return f"tenant:{getattr(user, 'tenant_id', 'default')}"
        return f"user:{getattr(user, 'tenant_id', 'default')}:{user.id}"

    def _prune(self, key: str, now: float) -> Deque[float]:
        events = self._events[key]
        cutoff = now - self.window_seconds
        while events and events[0] < cutoff:
            events.popleft()
        return events

    def usage(self, user: "User") -> tuple:
        """Return ``(used, limit)`` for *user*, for display in a UI."""
        events = self._prune(self._key(user), time.time())
        return len(events), self.max_messages

    async def before_message(self, user: "User", message: str) -> Optional[str]:
        if self.exempt_groups & set(getattr(user, "group_memberships", []) or []):
            return None

        now = time.time()
        key = self._key(user)
        events = self._prune(key, now)

        if len(events) >= self.max_messages:
            oldest = events[0]
            resets_in = int(self.window_seconds - (now - oldest))
            hours = max(1, resets_in // 3600)
            logger.info(
                "Quota exceeded key=%s used=%d limit=%d",
                key,
                len(events),
                self.max_messages,
            )
            raise QuotaExceededError(
                f"You have used all {self.max_messages} of your requests for "
                f"this period. More become available in about {hours} hour"
                f"{'s' if hours != 1 else ''}."
            )

        events.append(now)
        return None


class RateLimitHook(LifecycleHook):
    """Limits requests per user over a short window.

    Distinct from quota, and both are usually wanted: quota bounds *cost* over
    a day, rate limiting bounds *load* over a minute. A user with plenty of
    quota can still saturate a warehouse connection pool by holding down enter.

    Args:
        max_requests: Allowance per window.
        window_seconds: Window length. Default 60 seconds.
        exempt_groups: Groups the limit does not apply to.
    """

    def __init__(
        self,
        *,
        max_requests: int = 10,
        window_seconds: int = 60,
        exempt_groups: tuple = ("admin",),
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.exempt_groups = set(exempt_groups)
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    async def before_message(self, user: "User", message: str) -> Optional[str]:
        if self.exempt_groups & set(getattr(user, "group_memberships", []) or []):
            return None

        now = time.time()
        key = f"{getattr(user, 'tenant_id', 'default')}:{user.id}"
        events = self._events[key]
        cutoff = now - self.window_seconds
        while events and events[0] < cutoff:
            events.popleft()

        if len(events) >= self.max_requests:
            wait = max(1, int(self.window_seconds - (now - events[0])))
            logger.info("Rate limit hit key=%s", key)
            raise RateLimitExceededError(
                f"Too many requests. Please wait {wait} second"
                f"{'s' if wait != 1 else ''} before asking again."
            )

        events.append(now)
        return None
