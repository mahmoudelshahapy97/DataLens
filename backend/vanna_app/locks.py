"""Serialising work that must happen exactly once across every worker.

Several things in this deployment are "do this if it has not been done": create the
control-plane database, apply migrations, seed the first workspace, seed the first
administrator. Each was written as check-then-act, which is correct in one process
and wrong in four -- and uvicorn starts four.

The failure mode is not always a crash. Seeding raced silently: every worker found
zero accounts, every worker created the administrator, and each generated a
*different* password. Three of the four passwords printed in the log were dead by
the time anybody read them, and which one survived depended on write ordering. A
first-run instruction that is wrong three times out of four is worse than no
instruction, because the operator does not know to distrust it.

PostgreSQL advisory locks, because the control plane is already there and already
required. Nothing else in this stack is, so a lock service would be a new dependency
bought for a handful of statements that run once per deployment.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger("vanna.locks")

#: Namespace for every lock here. Arbitrary and fixed forever: it only has to be a
#: value nothing else in this database uses. Migrations hold their own key, so a
#: long migration cannot block seeding or vice versa.
KEY_MIGRATE = 0x5641_4E4E_4D49_4752  # "VANNMIGR"
KEY_SEED = 0x5641_4E4E_5345_4544     # "VANNSEED"
KEY_CONFIG = 0x5641_4E4E_434F_4E46    # "VANNCONF"


@contextmanager
def advisory_lock(db: Any, key: int) -> Iterator[bool]:
    """Hold a session-level advisory lock for the duration of the block.

    Blocking, not ``try_advisory_lock``: the callers here need the work *done*
    before they continue, not merely attempted by somebody. A worker that skipped
    seeding because another worker held the lock would carry on and find an empty
    directory, which is the bug being fixed rather than a different one.

    Yields True so a caller can read the block as "we have it".
    """
    with db.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield True
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
            connection.commit()


async def once(db: Any, key: int, work: Any) -> Any:
    """Run ``work`` under an advisory lock, off the event loop.

    ``work`` is an async callable and is awaited inside the lock, so its own
    check-then-act -- "are there any accounts yet?" -- happens with the lock held and
    is therefore no longer a race.

    Acquiring is blocking and happens in a worker thread; the *work* runs on the
    loop, because it is ordinary async control-plane code. The lock is held across
    both, which is the point.
    """
    if db is None:
        return await work()

    connection = await asyncio.to_thread(_acquire, db, key)
    try:
        return await work()
    finally:
        await asyncio.to_thread(_release, db, connection, key)


def _acquire(db: Any, key: int) -> Any:
    connection = db._pool.getconn()  # noqa: SLF001 - the lock is a pool-level concern
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (key,))
    return connection


def _release(db: Any, connection: Any, key: int) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
    except Exception as exc:  # pragma: no cover - shutdown only
        logger.debug("Could not release advisory lock %s: %s", key, exc)
    finally:
        connection.autocommit = False
        db._pool.putconn(connection)  # noqa: SLF001
