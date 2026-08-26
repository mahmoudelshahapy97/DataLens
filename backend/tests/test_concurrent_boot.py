"""Four workers starting at once.

Every test in this file corresponds to a bug that reached a running container and
that the rest of the suite structurally could not catch, because the rest of the
suite builds one application in one process.

uvicorn runs four workers. Each one independently creates the database, applies
migrations, seeds the first workspace and opens the vector index -- and every one of
those was written as check-then-act. What that produced on a first boot:

* three workers died on ``CREATE DATABASE`` (``UniqueViolation``, which the handler
  did not catch -- it caught ``DuplicateDatabase``, which is the *other* way
  PostgreSQL reports this),
* the first administrator was seeded four times, each with a different generated
  password, three of which were printed to the log and were already dead,
* two workers got 409 from Qdrant, treated it as "the vector index is unavailable",
  and served keyword-only retrieval for the life of the process -- so the quality of
  an answer depended on which worker received the question.

Concurrency here is real threads and real connections, not mocks. A mocked race does
not reproduce a race.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List
from urllib.parse import urlsplit, urlunsplit

import pytest

pytestmark = pytest.mark.integration

#: How many concurrent starters to simulate. Matches the worker count in the image.
WORKERS = 4


@pytest.fixture
def fresh_database_url(database_url: str):
    """A database name that does not exist yet, dropped afterwards."""
    import psycopg2

    parts = urlsplit(database_url)
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    name = f"vanna_race_{uuid.uuid4().hex[:10]}"
    target = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

    yield target

    connection = psycopg2.connect(admin_url, connect_timeout=10)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
    connection.close()


class TestDatabaseCreation:
    def test_concurrent_creation_does_not_kill_the_losers(self, fresh_database_url):
        """Three of four workers used to die here.

        ``ensure_database`` caught ``DuplicateDatabase`` (42P04), which PostgreSQL
        raises when the database already existed when the statement began. A genuine
        race raises ``UniqueViolation`` (23505) on ``pg_database_datname_index``
        instead -- so the handler covered the case that was not happening and missed
        the one that was.
        """
        from vanna_app.db import ensure_database

        errors: List[BaseException] = []
        barrier = __import__("threading").Barrier(WORKERS)

        def start() -> None:
            try:
                barrier.wait(timeout=10)  # all four hit CREATE together
                ensure_database(fresh_database_url)
            except BaseException as exc:  # noqa: BLE001 - the assertion is the point
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda _: start(), range(WORKERS)))

        assert not errors, f"{len(errors)} of {WORKERS} starters failed: {errors[:2]}"

    def test_the_database_is_usable_afterwards(self, fresh_database_url):
        import psycopg2

        from vanna_app.db import ensure_database

        ensure_database(fresh_database_url)
        connection = psycopg2.connect(fresh_database_url, connect_timeout=10)
        connection.close()

    def test_a_second_call_is_a_no_op(self, fresh_database_url):
        from vanna_app.db import ensure_database

        ensure_database(fresh_database_url)
        ensure_database(fresh_database_url)  # must not raise

    def test_a_bad_password_still_surfaces_as_itself(self, database_url):
        """The recoverable case must stay narrow.

        Treating every connection failure as "missing, create it" would turn a
        credentials problem into a confusing permissions error from CREATE DATABASE.
        """
        import psycopg2

        from vanna_app.db import ensure_database

        parts = urlsplit(database_url)
        wrong = urlunsplit(
            (parts.scheme, f"postgres:definitely-wrong@{parts.hostname}:{parts.port}",
             "/postgres", "", "")
        )
        with pytest.raises(psycopg2.OperationalError):
            ensure_database(wrong)


class TestMigrations:
    def test_concurrent_upgrades_apply_each_migration_once(self, fresh_database_url):
        """The advisory lock's actual job.

        Without it two replicas race and the loser crashes on a duplicate object;
        with it one applies and the rest find nothing to do.
        """
        from vanna_app.db import AppDatabase
        from vanna_app.migrate import discover, upgrade

        from vanna_app.db import ensure_database

        ensure_database(fresh_database_url)

        results: List[Any] = []
        errors: List[BaseException] = []
        barrier = __import__("threading").Barrier(WORKERS)

        def start() -> None:
            db = AppDatabase(fresh_database_url, minconn=1, maxconn=2, create_if_missing=False)
            try:
                barrier.wait(timeout=10)
                results.append(upgrade(db))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda _: start(), range(WORKERS)))

        assert not errors, f"concurrent migration failed: {errors[:2]}"
        # Exactly one starter did the work; the others found it done.
        applied = [r for r in results if r]
        assert len(applied) == 1
        assert len(applied[0]) == len(discover())

    def test_the_ledger_has_no_duplicates(self, fresh_database_url):
        from vanna_app.db import SCHEMA, AppDatabase, ensure_database
        from vanna_app.migrate import upgrade

        ensure_database(fresh_database_url)
        db = AppDatabase(fresh_database_url, minconn=1, maxconn=2, create_if_missing=False)
        try:
            upgrade(db)
            upgrade(db)
            rows = db.run_sync(
                f"SELECT version, count(*) AS n FROM {SCHEMA}.schema_migrations "
                "GROUP BY version HAVING count(*) > 1",
                fetch="all",
            )
            assert rows == []
        finally:
            db.close()


class TestSeeding:
    async def test_concurrent_seeding_creates_one_administrator(self, app_db):
        """Four workers, one account, one password.

        Unserialised, every worker saw an empty ``users`` table, every worker
        seeded, and each generated its own password. The last write won and the
        other three passwords -- all printed to the log as *the* first-run password
        -- were dead on arrival.
        """
        from vanna_app.accounts import Accounts, seed_first_admin
        from vanna_app.locks import KEY_SEED, once

        accounts = Accounts(app_db)

        async def seed() -> Any:
            return await once(
                app_db,
                KEY_SEED,
                lambda: seed_first_admin(accounts, admin_emails=["root@example.com"]),
            )

        results = await asyncio.gather(*[seed() for _ in range(WORKERS)])

        # Exactly one starter generated a password; the rest found the work done.
        generated = [r for r in results if r]
        assert len(generated) == 1, f"{len(generated)} workers generated a password"

        # And the one that was generated is the one that works.
        assert await accounts.verify("root@example.com", generated[0]) is not None
        assert await accounts.count() == 1

    async def test_concurrent_workspace_seeding_creates_one_workspace(self, directory):
        from vanna_app.locks import KEY_SEED, once
        from vanna_app.tenancy import seed_directory

        async def seed() -> None:
            await once(
                directory.db,
                KEY_SEED,
                lambda: seed_directory(
                    directory,
                    default_tenant="demo",
                    default_database_url=None,
                    admin_emails=["root@example.com"],
                ),
            )

        await asyncio.gather(*[seed() for _ in range(WORKERS)])

        assert await directory.count_tenants() == 1
        # And exactly one set of starter questions, not four.
        assert len(await directory.list_starters("demo")) == 3

    async def test_the_lock_is_released_even_when_the_work_raises(self, app_db):
        """A lock leaked on failure would hang every subsequent boot."""
        from vanna_app.locks import KEY_SEED, once

        async def boom() -> None:
            raise RuntimeError("seeding failed")

        with pytest.raises(RuntimeError):
            await once(app_db, KEY_SEED, boom)

        # If the lock were still held, this would block until the test timed out.
        await asyncio.wait_for(once(app_db, KEY_SEED, _noop), timeout=10)


async def _noop() -> None:
    return None


class TestQdrantCollection:
    """The 409 that downgraded retrieval, without needing a Qdrant."""

    def test_a_conflict_is_recognised_as_already_exists(self):
        from vanna.integrations.qdrant.search_index import _is_already_exists

        class Conflict(Exception):
            status_code = 409

        assert _is_already_exists(Conflict("Collection already exists!"))

    def test_the_message_alone_is_enough(self):
        # Not every client version exposes `status_code`.
        from vanna.integrations.qdrant.search_index import _is_already_exists

        assert _is_already_exists(
            Exception("Wrong input: Collection `vanna_knowledge` already exists!")
        )

    @pytest.mark.parametrize(
        "exc",
        [
            Exception("connection refused"),
            Exception("unauthorized"),
            ValueError("dimension mismatch"),
        ],
    )
    def test_a_real_failure_is_not_swallowed(self, exc):
        """The check must stay narrow.

        Catching the exception *type* would treat an authentication failure or a
        wrong URL as a successful creation, and the index would then fail on every
        write instead of at startup where somebody would see it.
        """
        from vanna.integrations.qdrant.search_index import _is_already_exists

        assert not _is_already_exists(exc)

    def test_a_conflicting_creation_yields_a_working_index(self):
        """The behaviour that matters: a losing worker still gets a usable client."""
        from vanna.integrations.qdrant.search_index import QdrantSearchIndex

        class Conflict(Exception):
            status_code = 409

        class FakeClient:
            def get_collections(self):
                return type("R", (), {"collections": []})()

            def create_collection(self, **_kwargs):
                raise Conflict("Collection `vanna_knowledge` already exists!")

        index = QdrantSearchIndex.__new__(QdrantSearchIndex)
        index.collection = "vanna_knowledge"
        index.embedder = type("E", (), {"dimension": 384})()

        # Must not raise: raising is what made the caller downgrade to lexical.
        index._ensure_collection(FakeClient(), lambda **k: None, type("D", (), {"COSINE": 1}))

    def test_a_genuine_failure_still_propagates(self):
        from vanna.integrations.qdrant.search_index import QdrantSearchIndex

        class FakeClient:
            def get_collections(self):
                raise ConnectionError("connection refused")

        index = QdrantSearchIndex.__new__(QdrantSearchIndex)
        index.collection = "vanna_knowledge"
        index.embedder = type("E", (), {"dimension": 384})()

        with pytest.raises(ConnectionError):
            index._ensure_collection(FakeClient(), lambda **k: None, type("D", (), {"COSINE": 1}))
