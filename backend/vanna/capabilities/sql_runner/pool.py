"""A small connection pool, and the one retry that is safe to do with it.

Only ``PostgresRunner`` pooled. MySQL, SQL Server, Oracle and SQLite each opened a
fresh connection per query -- a TCP handshake plus authentication on every question
-- and psycopg2's pool is not reusable for them because each driver has its own
connect signature. So this is the driver-agnostic half: a caller supplies a
``connect`` callable and gets pooling, breakage detection and a safe retry.

Deliberately small. It is not a general-purpose pool and does not try to be: no
background reaper, no idle eviction, no fairness. The pools it replaces did not
exist at all, and a hundred lines that are obviously correct are worth more here
than a thousand that are configurable.

**The retry rule, which is the part worth reading twice.**

Only the *acquisition* of a working connection is retried -- never a statement that
may have reached the server. The failure this rule exists to prevent::

    INSERT -> server executes it -> connection dies before the ack
           -> client sees a failure -> retry -> executed twice

A stale pooled connection is the common, boring failure: the database restarted, a
proxy timed out an idle socket, a firewall dropped it. Discovering that costs one
round trip and retrying it is free of consequence, because nothing was sent. So
``acquire`` will replace a dead connection and hand back a live one, and
``execute`` does not retry anything at all. A timeout is not retried either: a
statement that timed out is still running on the server as far as we know.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

#: Substrings of driver error messages that mean "this connection is dead", as
#: opposed to "your query is wrong" or "you may not do that". Matched on the
#: message because every driver spells its exception hierarchy differently and
#: several raise a bare `OperationalError` for both cases.
#:
#: Kept deliberately narrow. A phrase that also appears in a *query* failure would
#: turn this from "reconnect" into "run it again", which is the bug at the top of
#: this file.
DEAD_CONNECTION_HINTS = (
    "server closed the connection",
    "connection already closed",
    "connection is closed",
    "connection not open",
    "lost connection",
    "mysql server has gone away",
    "broken pipe",
    "connection reset",
    "connection refused",
    "no connection to the server",
    "terminating connection",
    "server has gone away",
    "not connected",
    "communication link failure",
    "socket is closed",
)


def looks_dead(error: BaseException) -> bool:
    """Whether *error* means the connection is unusable, rather than the query.

    Errors are classified by message rather than by type on purpose: pymysql,
    psycopg2 and pyodbc all raise ``OperationalError`` for a dropped socket *and*
    for a bad statement, so the type carries less information than the text.
    """
    text = str(error).lower()
    return any(hint in text for hint in DEAD_CONNECTION_HINTS)


class ConnectionPool:
    """Reuse connections, replace dead ones, and never retry a statement.

    Args:
        connect: Opens a new connection. Called with no arguments.
        close: Closes one. Defaults to calling ``.close()``.
        alive: Optional liveness check, cheaper than a round trip where the
            driver has one (pymysql's ``ping``). Returning False retires the
            connection instead of handing it out.
        max_size: Connections that may exist at once. Beyond this, ``acquire``
            waits.
        name: For log lines, so a saturated pool says which database.
    """

    def __init__(
        self,
        connect: Callable[[], Any],
        *,
        close: Optional[Callable[[Any], None]] = None,
        alive: Optional[Callable[[Any], bool]] = None,
        max_size: int = 2,
        name: str = "warehouse",
    ) -> None:
        self._connect = connect
        self._close = close or (lambda connection: connection.close())
        self._alive = alive
        self._max_size = max(1, max_size)
        self._name = name

        self._idle: List[Any] = []
        self._leased = 0
        self._closed = False
        # A condition rather than a semaphore: `release` has to hand a specific
        # connection back, not merely a permit.
        self._change = threading.Condition()

    # ------------------------------------------------------------------

    def acquire(self, timeout: float = 30.0) -> Any:
        """A connection that is alive, or an exception saying why not.

        Retries only the acquisition: a connection found dead is discarded and
        another is opened. Nothing has been sent on it, so this cannot duplicate
        work -- which is exactly why the retry lives here and not around
        ``execute``.
        """
        with self._change:
            if self._closed:
                raise RuntimeError(f"The {self._name} connection pool is closed.")
            while not self._idle and self._leased >= self._max_size:
                if not self._change.wait(timeout):
                    raise TimeoutError(
                        f"No {self._name} connection became free within {timeout}s "
                        f"({self._max_size} in use). Raise the pool size for this "
                        "data source, or look for a query holding one open."
                    )
                if self._closed:
                    raise RuntimeError(f"The {self._name} connection pool is closed.")
            self._leased += 1
            candidate = self._idle.pop() if self._idle else None

        # Outside the lock: opening a socket can take as long as the network does,
        # and holding the lock through it would serialise every other caller.
        try:
            if candidate is not None:
                if self._usable(candidate):
                    return candidate
                # Dead in the pool. Not an error anybody needs to see: it is what
                # happens when a database restarts, and replacing it is the whole
                # job of this branch.
                logger.debug("Discarding a dead %s connection", self._name)
                self._discard(candidate)
            return self._connect()
        except BaseException:
            with self._change:
                self._leased -= 1
                self._change.notify()
            raise

    def release(self, connection: Any, *, broken: bool = False) -> None:
        """Return a connection, or drop it if the caller says it is broken."""
        with self._change:
            self._leased -= 1
            if self._closed or broken:
                keep = False
            else:
                keep = len(self._idle) < self._max_size
            if keep:
                self._idle.append(connection)
            self._change.notify()
        if not keep:
            self._discard(connection)

    def close(self) -> None:
        """Close every idle connection and refuse new leases.

        Leased connections are *not* closed: their borrower is still using one,
        and closing it underneath them is the use-after-close this codebase has
        already been bitten by once. They are discarded on release instead.
        """
        with self._change:
            self._closed = True
            idle, self._idle = self._idle, []
            self._change.notify_all()
        for connection in idle:
            self._discard(connection)

    # ------------------------------------------------------------------

    @property
    def in_use(self) -> int:
        with self._change:
            return self._leased

    @property
    def size(self) -> int:
        """Connections that exist right now, leased or idle."""
        with self._change:
            return self._leased + len(self._idle)

    def _usable(self, connection: Any) -> bool:
        if self._alive is None:
            return True
        try:
            return bool(self._alive(connection))
        except Exception:
            return False

    def _discard(self, connection: Any) -> None:
        try:
            self._close(connection)
        except Exception as exc:  # pragma: no cover - already on its way out
            logger.debug("Closing a %s connection: %s", self._name, exc)
