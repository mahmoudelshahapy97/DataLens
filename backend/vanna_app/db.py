"""The pooled connection to the control-plane database.

Synchronous psycopg2 driven through ``asyncio.to_thread``. The alternative is an
async driver and a second connection library in the image; at control-plane volumes
-- a handful of queries per request, none of them hot -- a thread hop costs less
than the dependency.

Two things changed from the original:

**The pool waits instead of failing.** ``ThreadedConnectionPool.getconn`` *raises*
``PoolError`` the moment every connection is checked out, so with the old
``maxconn=8`` the ninth concurrent control-plane query was a 500 rather than a
queue. An ``asyncio.Semaphore`` sized to the pool now gates entry, so callers wait
their turn and only give up after ``VANNA_APP_POOL_WAIT_SECONDS``. A timeout is
still an error, but it is an error that means "the control plane is saturated" and
says so, rather than one that means "you were unlucky with concurrency".

**Failure is not silent.** ``build_app_database`` used to catch every exception and
return ``None``, after which the whole application ran with no directory, no
accounts, and therefore no authentication. It now raises, and only the explicitly
opted-in demo path is allowed to continue without a control plane.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("vanna.db")

SCHEMA = "vanna_app"


class ControlPlaneUnavailable(RuntimeError):
    """The control plane could not be reached or opened."""


def _admin_url(url: str) -> str:
    """Same server, pointed at the always-present ``postgres`` database.

    ``CREATE DATABASE`` cannot run from inside the database being created, and the
    target may not exist yet on a first boot.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/") or "postgres"


def ensure_database(url: str) -> None:
    """Create the control-plane database if it is missing.

    A first boot against a fresh PostgreSQL should not require an operator to
    hand-create a database, and ``CREATE DATABASE`` is not transactional, hence the
    explicit autocommit.
    """
    name = database_name(url)
    if _database_reachable(url):
        return

    logger.info("Control-plane database %r missing -- creating it", name)
    connection = psycopg2.connect(_admin_url(url), connect_timeout=10)
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            # psycopg2 cannot parameterise an identifier; the name comes from our
            # own configured URL, and quoting is the escaping path.
            cursor.execute(f'CREATE DATABASE "{name}"')
    except _ALREADY_EXISTS as exc:
        # Somebody else created it between our check and our CREATE.
        #
        # This is not hypothetical: uvicorn starts four workers, each builds the
        # application independently, and on a first boot all four find the database
        # missing within the same millisecond. Three then lose the race and -- until
        # this caught the right exception -- died, taking the worker with them.
        #
        # Two exceptions matter, and the obvious one is not the common one.
        # PostgreSQL raises DuplicateDatabase (42P04) when the database already
        # existed at the start of the statement, and UniqueViolation (23505) on
        # pg_database_datname_index when two CREATEs are genuinely concurrent. Only
        # the first was handled here, so the actual race was the unhandled case.
        logger.info("Another process created %r first (%s)", name, type(exc).__name__)
    finally:
        connection.close()

    # Verify rather than assume. If the create failed for a reason that merely looks
    # like a race, the failure belongs here, at boot, and not later as a confusing
    # connection error from somewhere in the request path.
    if not _database_reachable(url):
        raise ControlPlaneUnavailable(
            f"Database {name!r} could not be created and is still unreachable."
        )


#: The two ways PostgreSQL reports "somebody else got there first".
_ALREADY_EXISTS = (psycopg2.errors.DuplicateDatabase, psycopg2.errors.UniqueViolation)


def _database_reachable(url: str) -> bool:
    """Whether the target database exists and accepts a connection.

    Anything other than "does not exist" is re-raised: a bad password or an
    unreachable host must surface as itself rather than being retried as a creation
    problem, which would replace a clear error with a confusing one.
    """
    try:
        connection = psycopg2.connect(url, connect_timeout=10)
        connection.close()
        return True
    except psycopg2.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise
        return False


class AppDatabase:
    """Thin pooled wrapper over the control-plane database.

    Every public method is async and does its blocking work in a worker thread, so a
    slow control-plane query cannot stall the event loop that is streaming somebody
    else's answer.
    """

    def __init__(
        self,
        url: str,
        *,
        minconn: int = 2,
        maxconn: int = 16,
        wait_seconds: int = 10,
        create_if_missing: bool = True,
    ) -> None:
        self.url = url
        self.wait_seconds = wait_seconds
        if create_if_missing:
            ensure_database(url)
        self._pool = ThreadedConnectionPool(minconn, maxconn, url, connect_timeout=10)
        # Gate on entry rather than discovering exhaustion inside psycopg2. Sized to
        # the pool exactly: one permit is one connection.
        self._slots = asyncio.Semaphore(maxconn)
        self._maxconn = maxconn
        self._closed = False

    # -- plumbing ------------------------------------------------------

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._pool.getconn()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            self._pool.putconn(connection)

    def _run(self, sql: str, params: Sequence[Any] = (), *, fetch: str = "none") -> Any:
        with self._connection() as connection:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(sql, params)
                if fetch == "all":
                    return [dict(row) for row in cursor.fetchall()]
                if fetch == "one":
                    row = cursor.fetchone()
                    return dict(row) if row else None
                if fetch == "rowcount":
                    return cursor.rowcount
                return None

    async def _guarded(self, sql: str, params: Sequence[Any], fetch: str) -> Any:
        """Acquire a slot, then do the blocking work off the event loop."""
        if self._closed:
            raise ControlPlaneUnavailable("The control-plane pool is closed.")
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.wait_seconds)
        except asyncio.TimeoutError as exc:
            raise ControlPlaneUnavailable(
                f"The control plane is saturated: all {self._maxconn} connections "
                f"were busy for {self.wait_seconds}s. Raise VANNA_APP_POOL_MAX, or "
                "look for a query holding a connection open."
            ) from exc
        try:
            return await asyncio.to_thread(self._run, sql, params, fetch=fetch)
        finally:
            self._slots.release()

    # -- queries -------------------------------------------------------

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return await self._guarded(sql, params, "all")

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        return await self._guarded(sql, params, "one")

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await self._guarded(sql, params, "rowcount")

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        """The first column of the first row -- for counts and existence checks."""
        row = await self.fetch_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    # -- synchronous escape hatch --------------------------------------

    def run_sync(self, sql: str, params: Sequence[Any] = (), *, fetch: str = "none") -> Any:
        """Blocking query, for startup and CLI paths with no event loop."""
        return self._run(sql, params, fetch=fetch)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """A raw connection with an open transaction, for the migration runner."""
        with self._connection() as connection:
            yield connection

    def health(self) -> bool:
        """Whether the control plane answers. Used by the readiness probe."""
        try:
            self._run("SELECT 1")
            return True
        except Exception as exc:
            logger.warning("Control-plane health check failed: %s", exc)
            return False

    def close(self) -> None:
        self._closed = True
        try:
            self._pool.closeall()
        except Exception as exc:  # pragma: no cover - shutdown only
            logger.debug("Pool close: %s", exc)


def build_app_database(settings: Any) -> Optional[AppDatabase]:
    """Open the control plane.

    Returns ``None`` only when no URL is configured *and* the deployment has
    explicitly opted out of having one. Any other failure raises: a control plane
    that cannot be reached used to leave the process running with no authentication
    at all, which turned a database blip into an open door.
    """
    if not settings.app_database_url:
        if settings.allow_anonymous or settings.is_demo:
            logger.warning(
                "No VANNA_APP_DATABASE_URL: running without a control plane. "
                "Tenants, members, history and saved queries are disabled, and "
                "requests are not authenticated."
            )
            return None
        raise ControlPlaneUnavailable(
            "VANNA_APP_DATABASE_URL is not set. Configure the control plane, or set "
            "VANNA_ALLOW_ANONYMOUS=true to run without authentication."
        )

    try:
        return AppDatabase(
            settings.app_database_url,
            minconn=settings.app_pool_min,
            maxconn=settings.app_pool_max,
            wait_seconds=settings.app_pool_wait_seconds,
        )
    except Exception as exc:
        raise ControlPlaneUnavailable(
            f"Could not open the control-plane database "
            f"({database_name(settings.app_database_url)}): {exc}"
        ) from exc
