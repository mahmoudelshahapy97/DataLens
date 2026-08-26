"""One browser for the whole end-to-end run.

Each e2e module used to declare its own session-scoped ``browser`` fixture. Two
fixtures with the same name in two modules are two *different* fixtures, so
running the directory rather than a single file opened a second
``sync_playwright()`` while the first was still live -- and Playwright answers
that with

    It looks like you are using Playwright Sync API inside the asyncio loop.
    Please use the Async API instead.

which is a confusing way to say "there is already one of these". Every test in
the second module errored at setup, so ``pytest tests/e2e`` reported 42 errors
while each file passed on its own. Sharing one fixture removes the second
context entirely.

``--disable-dev-shm-usage`` matters wherever ``/dev/shm`` is small, which is
every CI container: without it Chromium's shared memory fills and the renderer
dies as "Page crashed" with nothing else to go on.
"""

from __future__ import annotations

import pytest

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        instance = p.chromium.launch(args=["--disable-dev-shm-usage"])
        yield instance
        instance.close()
