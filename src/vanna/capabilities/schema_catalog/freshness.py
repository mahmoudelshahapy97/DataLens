"""Detect when catalog and knowledge sources have gone stale.

A derived index -- the schema catalog, the example embeddings -- is only as
good as its last rebuild. A stale index does not raise; it quietly returns the
wrong schema and the agent writes confident SQL against a column that was
dropped last week. That is the worst failure shape available: silent, and
indistinguishable from correct behaviour until someone checks a number by hand.

This module makes staleness observable, and optionally self-correcting, by
fingerprinting the sources an index is built from. When the fingerprint moves,
the index is stale. Content hashing rather than mtime, because mtime changes on
every checkout, every ``touch``, and every file copy, so an mtime-based watcher
mostly fires when nothing has actually changed.

Two use modes:

* **Check** -- ``SourceFingerprint.has_changed()`` in a health endpoint or
  before serving a request, so staleness surfaces as a warning.
* **Watch** -- ``watch()`` in a background task during active development, so
  edits reindex automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Floor on the polling interval. Guards against a pathological tight loop
#: hashing a large tree continuously.
MIN_POLL_SECONDS = 1.0


@dataclass
class SourceFingerprint:
    """Content hash over a set of files and directories.

    Args:
        paths: Files and directories to fingerprint. Directories are walked
            for files matching ``patterns``. Missing paths are simply absent
            from the hash, so a watcher started before a file exists picks it
            up on the poll after it appears.
        patterns: Glob patterns applied when walking directories.
    """

    paths: Sequence[Path]
    patterns: Sequence[str] = field(default_factory=lambda: ("*.md", "*.yml",
                                                             "*.yaml", "*.json"))
    _last: Optional[str] = field(default=None, repr=False)

    def files(self) -> List[Path]:
        """Every file contributing to the fingerprint, in stable order.

        Sorted so the hash depends only on content, never on filesystem
        iteration order -- otherwise the same tree would fingerprint
        differently on different machines.
        """
        found: List[Path] = []
        for path in self.paths:
            if path.is_file():
                found.append(path)
            elif path.is_dir():
                for pattern in self.patterns:
                    found.extend(p for p in path.rglob(pattern) if p.is_file())
        return sorted(set(found))

    def compute(self) -> str:
        """Hash the current content of all watched files.

        Paths are hashed alongside content so that renaming a file -- which
        changes what the index should contain, without changing any bytes --
        still registers as a change.
        """
        digest = hashlib.sha256()
        for path in self.files():
            try:
                digest.update(str(path).encode())
                digest.update(path.read_bytes())
            except OSError as e:
                # A file vanishing mid-scan is itself a change; fold the error
                # into the hash rather than crashing the watcher.
                logger.debug("Could not read %s while fingerprinting: %s", path, e)
                digest.update(f"<unreadable:{path}>".encode())
        return digest.hexdigest()[:16]

    def has_changed(self) -> bool:
        """True if content differs from the last call. Updates the baseline.

        The first call always reports ``True`` -- there is no baseline yet, and
        assuming freshness would skip the initial build.
        """
        current = self.compute()
        changed = current != self._last
        self._last = current
        return changed

    def mark_current(self) -> str:
        """Record the current state as the baseline without reporting change."""
        self._last = self.compute()
        return self._last


def knowledge_fingerprint(
    knowledge_root: str, *, extra_paths: Iterable[str] = ()
) -> SourceFingerprint:
    """Fingerprint a markdown knowledge directory plus anything else supplied."""
    paths = [Path(knowledge_root)]
    paths.extend(Path(p) for p in extra_paths)
    return SourceFingerprint(paths=paths, patterns=("*.md",))


def dbt_fingerprint(project_dir: str) -> SourceFingerprint:
    """Fingerprint a dbt project's schema files and compiled manifest."""
    root = Path(project_dir)
    return SourceFingerprint(
        paths=[root / "models", root / "seeds", root / "target" / "manifest.json"],
        patterns=("*.yml", "*.yaml", "*.json"),
    )


async def watch(
    fingerprint: SourceFingerprint,
    on_change: Callable[[], Awaitable[None]],
    *,
    interval_seconds: float = 5.0,
    run_immediately: bool = True,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Poll *fingerprint* and call *on_change* whenever content changes.

    Intended to run as a background task::

        task = asyncio.create_task(watch(fp, reindex))
        ...
        stop.set(); await task

    A failing callback is logged and the loop continues. A watcher that dies on
    the first transient error is worse than no watcher, because the index then
    silently stops updating while everything appears fine.

    Args:
        fingerprint: Sources to watch.
        on_change: Called on each detected change.
        interval_seconds: Poll interval, floored at :data:`MIN_POLL_SECONDS`.
        run_immediately: Run the callback once at startup, before polling.
            Usually correct -- the index may already be stale from changes made
            while the process was down.
        stop_event: Set to end the loop.
    """
    interval = max(MIN_POLL_SECONDS, interval_seconds)
    stop_event = stop_event or asyncio.Event()

    if run_immediately:
        fingerprint.mark_current()
        await _safe_call(on_change)

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop requested
        except asyncio.TimeoutError:
            pass

        if fingerprint.has_changed():
            logger.info(
                "Watched sources changed; rebuilding derived index "
                "(%d files)",
                len(fingerprint.files()),
            )
            await _safe_call(on_change)


async def _safe_call(callback: Callable[[], Awaitable[None]]) -> None:
    try:
        await callback()
    except Exception as e:
        logger.error("Reindex callback failed: %s", e, exc_info=True)
