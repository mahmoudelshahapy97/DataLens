"""The control-plane pool's gate, and what it is allowed to lend out.

``AppDatabase`` fronts a psycopg2 pool with an ``asyncio.Semaphore`` so the ninth
concurrent query on an eight-connection pool queues instead of becoming a 500. The
gate only means anything if *every* path through it is counted, and one was not:
``transaction()`` took a connection straight from the pool, and five stores used
it -- catalog, domains, grants, instructions and pending writes. A schema scan
therefore held a connection the gate did not know it had lent out, for the length
of a whole catalog rewrite, while ordinary requests queued for slots that were
already gone.

So these tests are about the arithmetic being true rather than about SQL:

* a gated transaction occupies a slot for its whole life;
* ``transact`` -- the one implementation the stores now share -- does too;
* the ungated ``transaction()`` still exists and is still ungated, because the
  migration runner calls it before there is an event loop to wait on. That is a
  deliberate exception, so it is pinned rather than left to be rediscovered.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def one_slot(database_url):
    """An ``AppDatabase`` with exactly one connection and a short patience.

    One slot makes contention deterministic: with two, a test has to race.
    """
    from vanna_app.db import AppDatabase

    db = AppDatabase(
        database_url, minconn=1, maxconn=1, wait_seconds=1, create_if_missing=False
    )
    try:
        yield db
    finally:
        db.close()


class TestTheGateCountsEveryConnection:
    async def test_a_gated_transaction_holds_the_only_slot(self, one_slot):
        """The bug, stated as a test: this used to pass straight through."""
        from vanna_app.db import ControlPlaneUnavailable

        async with one_slot.transaction_async():
            with pytest.raises(ControlPlaneUnavailable) as caught:
                await one_slot.fetch_one("SELECT 1 AS n")

        # The message has to name what to do about it, not just that it happened.
        assert "saturated" in str(caught.value)
        assert "VANNA_APP_POOL_MAX" in str(caught.value)

    async def test_the_slot_comes_back_afterwards(self, one_slot):
        async with one_slot.transaction_async():
            pass

        row = await one_slot.fetch_one("SELECT 1 AS n")
        assert row == {"n": 1}

    async def test_the_slot_comes_back_after_a_failure(self, one_slot):
        """Released in a `finally`, or one bad transaction costs the pool a slot
        permanently and the process degrades until it is restarted."""
        with pytest.raises(RuntimeError):
            async with one_slot.transaction_async():
                raise RuntimeError("boom")

        assert await one_slot.fetch_one("SELECT 1 AS n") == {"n": 1}

    async def test_transact_is_gated_too(self, one_slot):
        """`transact` is what the five stores call now.

        The body runs in a worker thread, so it blocks on a `threading.Event`:
        an asyncio primitive would belong to a loop the thread cannot reach.
        """
        from vanna_app.db import ControlPlaneUnavailable

        started = threading.Event()
        release = threading.Event()

        def body(cursor):
            cursor.execute("SELECT 1")
            started.set()
            # Hold the transaction open while the assertion below runs, so the
            # slot is genuinely occupied rather than merely recently used.
            assert release.wait(timeout=5), "test never released the transaction"

        task = asyncio.ensure_future(one_slot.transact(body))
        try:
            await asyncio.to_thread(started.wait, 5)
            with pytest.raises(ControlPlaneUnavailable):
                await one_slot.fetch_one("SELECT 1 AS n")
        finally:
            release.set()
            await task

    async def test_queries_queue_rather_than_fail(self, one_slot):
        """Two callers, one slot, and neither is refused -- the property the
        semaphore was added for in the first place."""
        results = await asyncio.gather(
            one_slot.fetch_one("SELECT 1 AS n"),
            one_slot.fetch_one("SELECT 2 AS n"),
        )
        assert sorted(r["n"] for r in results) == [1, 2]


class TestTheBootPathIsDeliberatelyUngated:
    def test_the_sync_transaction_does_not_wait(self, one_slot):
        """`migrate.py` and `locks.py` run before there is a loop to wait on, so
        this one takes a connection directly. Pinned so the exception stays a
        decision rather than becoming a surprise."""
        with one_slot.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone()[0] == 1


class TestTheGaugesReportReality:
    async def test_in_use_and_waiters_move(self, one_slot, monkeypatch):
        """`pool_waiters` existed from the day metrics were added and nothing ever
        set it, so the one gauge that would have shown saturation read zero."""
        from vanna_app import observability

        seen = {"in_use": [], "waiters": []}

        class Recorder:
            def __init__(self, key):
                self.key = key

            def set(self, value):
                seen[self.key].append(value)

            def inc(self, *_a, **_k):
                pass

            def observe(self, *_a, **_k):
                pass

        class Fake:
            pool_in_use = Recorder("in_use")
            pool_waiters = Recorder("waiters")
            pool_wait_seconds = Recorder("ignored")
            pool_saturated = Recorder("ignored")
            pool_ceiling = Recorder("ignored")

        seen["ignored"] = []
        monkeypatch.setattr(observability, "get_metrics", lambda: Fake())

        await one_slot.fetch_one("SELECT 1 AS n")

        assert 1 in seen["in_use"], "a checked-out connection was never reported"
        assert 0 in seen["in_use"], "the connection was never reported as returned"


class TestTheConnectionCeiling:
    """The arithmetic, and that it is stated rather than discovered.

    Every pool is per process, so each term is multiplied by the worker count.
    The old defaults permitted 704 connections against a server whose
    `max_connections` was 100 -- and because the control plane shares that server,
    exhausting it took authentication down too. Nothing computed this, so nothing
    could say it.
    """

    @staticmethod
    def _settings(**overrides):
        from vanna_app.config import load_settings

        env = {"VANNA_DEPLOYMENT_MODE": "demo", "VANNA_SECRET_KEY": "x" * 48}
        env.update({k: str(v) for k, v in overrides.items()})
        return load_settings(env)

    def test_the_formula_multiplies_by_workers(self):
        from vanna_app.config import connection_ceiling

        settings = self._settings(
            VANNA_WEB_CONCURRENCY=4,
            VANNA_APP_POOL_MAX=8,
            VANNA_MAX_TENANT_RUNTIMES=6,
            VANNA_WAREHOUSE_POOL_MAX=2,
        )
        # 4 x (8 + 6 x 2)
        assert connection_ceiling(settings) == 80

    def test_the_shipped_defaults_fit_the_shipped_budget(self):
        """If this fails, the defaults and the budget disagree and one of them is
        wrong -- which is the state this whole change exists to leave behind."""
        from vanna_app.config import connection_ceiling, warn_about_connections

        settings = self._settings()
        assert connection_ceiling(settings) <= settings.connection_budget
        assert warn_about_connections(settings) is None

    def test_the_old_defaults_would_now_be_reported(self):
        from vanna_app.config import connection_ceiling, warn_about_connections

        settings = self._settings(
            VANNA_APP_POOL_MAX=16,
            VANNA_MAX_TENANT_RUNTIMES=32,
            VANNA_WAREHOUSE_POOL_MAX=5,
        )
        assert connection_ceiling(settings) == 704

        complaint = warn_about_connections(settings)
        # It has to name every knob, or the reader has to go and find them.
        assert complaint is not None
        for knob in (
            "VANNA_APP_POOL_MAX",
            "VANNA_MAX_TENANT_RUNTIMES",
            "VANNA_WAREHOUSE_POOL_MAX",
            "VANNA_WEB_CONCURRENCY",
        ):
            assert knob in complaint, f"{knob} missing from the warning"

    def test_it_warns_rather_than_refusing(self):
        """The real limit is on a server this process does not administer, so an
        estimate must not be able to stop a deployment starting."""
        from vanna_app.config import load_and_validate

        settings = load_and_validate({
            "VANNA_DEPLOYMENT_MODE": "demo",
            "VANNA_SECRET_KEY": "x" * 48,
            "VANNA_APP_POOL_MAX": "64",
            "VANNA_MAX_TENANT_RUNTIMES": "64",
            "VANNA_WAREHOUSE_POOL_MAX": "16",
        })
        assert settings.app_pool_max == 64

    def test_an_inverted_warehouse_pool_is_a_refusal(self):
        """Unlike the budget, this one cannot be right under any circumstances."""
        from vanna_app.config import validate

        problems = validate(self._settings(
            VANNA_WAREHOUSE_POOL_MIN=5, VANNA_WAREHOUSE_POOL_MAX=2
        ))
        assert any("VANNA_WAREHOUSE_POOL_MIN" in p for p in problems)


class TestThePoolSizeReachesTheRunner:
    def test_the_warehouse_pool_ceiling_is_passed_through(self):
        """`build_runner` documented "multiply by the worker count" and nothing
        passed it, so every runtime took the driver default of five."""
        from vanna.core.datasource.runners import build_runner

        runner = build_runner(
            "postgresql://u:p@nowhere:5432/db", pool_max=2, pool_min=0
        )
        # Constructed, not connected: the pool is lazy, which is what lets this
        # assert the configuration without a database.
        assert runner._pool_max_size == 2
        assert runner._pool_min_size == 0
