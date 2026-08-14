"""``vanna knowledge`` -- keeping the search index in step with the markdown.

The markdown under ``knowledge/`` is the source of truth: it is edited, reviewed
and committed like code. The index is a derived artifact.

With the default in-process BM25 index there is nothing to keep in step -- it is
rebuilt from the files on every search, which costs under a millisecond and can
never be stale. **These commands exist for a persistent backend**, where the
index outlives the process and re-embedding the whole corpus per question is not
an option.

Two commands, covering the two ways files change without the application
knowing:

* ``reindex`` -- a ``git pull``, a bulk edit, a first run against a store that
  already has content.
* ``watch`` -- someone editing a rule in their editor while the server runs.

Changes made *through* the application need neither: the stores index on write.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Tuple

import click


def _knowledge_root(path: Optional[Path]) -> Path:
    """Resolve the knowledge directory, preferring the project's own."""
    from ..config.env import find_project_root

    if path is not None:
        return Path(path)

    root = find_project_root(Path.cwd())
    if root is not None and (Path(root) / "knowledge").is_dir():
        return Path(root) / "knowledge"

    import os

    return Path(os.getenv("VANNA_KNOWLEDGE_DIR", "./knowledge"))


def _index_and_stores(root: Path, backend: Optional[str]):
    """Build the configured index and the two markdown stores over *root*."""
    import os

    from ..capabilities.index import resolve_index
    from ..integrations.local import MarkdownExampleStore, MarkdownInstructionStore

    index = resolve_index(backend or os.getenv("VANNA_INDEX_BACKEND", "lexical"))
    examples = MarkdownExampleStore(str(root), index=index)
    instructions = MarkdownInstructionStore(str(root))
    return index, examples, instructions


def _markdown_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _signature(root: Path) -> List[Tuple[str, float, int]]:
    """(path, mtime, size) for every markdown file.

    mtime *and* size, because a file edited twice within one filesystem
    timestamp tick is not hypothetical when a script is doing the editing.
    """
    out = []
    for path in _markdown_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append((str(path), stat.st_mtime, stat.st_size))
    return out


async def _reindex(root: Path, backend: Optional[str], tenant: str) -> str:
    from ..capabilities.index import documents_for_examples, sync_documents
    from ..cli._runtime import system_context

    index, examples, _ = _index_and_stores(root, backend)
    context = system_context(tenant)

    stored = await examples.list_all(context)
    report = sync_documents(
        index,
        documents_for_examples(stored, tenant_id=tenant),
        tenant_id=tenant,
        kinds="example",
    )
    return f"{index.name}: {report} over {len(stored)} example(s)"


@click.group()
def knowledge() -> None:
    """Index the curated knowledge files."""


@knowledge.command()
@click.option("--path", type=click.Path(path_type=Path), default=None,
              help="Knowledge directory. Defaults to the project's, or VANNA_KNOWLEDGE_DIR.")
@click.option("--backend", default=None,
              help="Index backend. Defaults to VANNA_INDEX_BACKEND.")
@click.option("--tenant", default="default", help="Workspace to index for.")
def reindex(path: Optional[Path], backend: Optional[str], tenant: str) -> None:
    """Bring the index into step with the files on disk.

    Only what changed is re-embedded, and anything removed from disk is removed
    from the index -- a rule someone deleted that keeps being retrieved is worse
    than one that was never indexed, because they believe it is gone.
    """
    import asyncio

    root = _knowledge_root(path)
    if not root.is_dir():
        raise click.ClickException(f"No knowledge directory at {root}")

    click.echo(f"Indexing {root} for tenant {tenant!r}...")
    try:
        summary = asyncio.run(_reindex(root, backend, tenant))
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"Could not index: {exc}")
    click.secho(f"  {summary}", fg="green")


@knowledge.command()
@click.option("--path", type=click.Path(path_type=Path), default=None,
              help="Knowledge directory to watch.")
@click.option("--backend", default=None, help="Index backend.")
@click.option("--tenant", default="default", help="Workspace to index for.")
@click.option("--interval", default=3.0, show_default=True,
              help="Seconds between checks.")
def watch(
    path: Optional[Path], backend: Optional[str], tenant: str, interval: float
) -> None:
    """Re-index whenever a knowledge file changes. Ctrl-C to stop.

    Polls mtimes rather than using filesystem events. That is a deliberate
    trade: `watchdog` would be a new dependency and a platform-specific one, and
    stat-ing a few hundred paths every few seconds costs nothing measurable. The
    cost is up to `--interval` seconds of latency, which for a file a human just
    saved is not a cost at all.
    """
    import asyncio

    root = _knowledge_root(path)
    if not root.is_dir():
        raise click.ClickException(f"No knowledge directory at {root}")

    click.echo(f"Watching {root} (every {interval:g}s). Ctrl-C to stop.")

    # Index once up front, so the watcher does not start by trusting an index
    # it has never checked.
    try:
        click.echo(f"  {asyncio.run(_reindex(root, backend, tenant))}")
    except Exception as exc:  # noqa: BLE001
        click.secho(f"  initial index failed: {exc}", fg="yellow", err=True)

    previous = _signature(root)
    try:
        while True:
            time.sleep(interval)
            current = _signature(root)
            if current == previous:
                continue

            changed = len(set(current) ^ set(previous)) or 1
            previous = current
            stamp = time.strftime("%H:%M:%S")
            try:
                summary = asyncio.run(_reindex(root, backend, tenant))
                click.echo(f"  [{stamp}] {changed} file change(s) -> {summary}")
            except Exception as exc:  # noqa: BLE001
                # A failed re-index must not stop the watcher: the next save is
                # very often the fix for whatever broke this one.
                click.secho(f"  [{stamp}] index failed: {exc}", fg="yellow", err=True)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@knowledge.command("status")
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.option("--backend", default=None)
@click.option("--tenant", default="default")
def status(path: Optional[Path], backend: Optional[str], tenant: str) -> None:
    """What is on disk, what is in the index, and whether they agree."""
    import asyncio

    from ..capabilities.index import documents_for_examples, fingerprint
    from ..cli._runtime import system_context

    root = _knowledge_root(path)
    if not root.is_dir():
        raise click.ClickException(f"No knowledge directory at {root}")

    index, examples, _ = _index_and_stores(root, backend)
    stored = asyncio.run(examples.list_all(system_context(tenant)))
    documents = list(documents_for_examples(stored, tenant_id=tenant))

    click.echo(f"Knowledge: {root}")
    click.echo(f"  files:   {len(_markdown_files(root))} markdown")
    click.echo(f"  examples:{len(stored)}")
    click.echo(f"  backend: {index.name}")

    indexed = index.fingerprints(tenant_id=tenant, kind="example")
    if indexed is None:
        click.echo(
            "  index:   rebuilt on every search, so it cannot be out of date."
        )
        return

    on_disk = {d.id: fingerprint(d) for d in documents}
    missing = [i for i in on_disk if i not in indexed]
    stale = [i for i in on_disk if i in indexed and indexed[i] != on_disk[i]]
    orphan = [i for i in indexed if i not in on_disk]

    click.echo(f"  indexed: {len(indexed)}")
    if not (missing or stale or orphan):
        click.secho("  in step with the files.", fg="green")
        return
    click.secho(
        f"  out of step: {len(missing)} missing, {len(stale)} stale, "
        f"{len(orphan)} no longer on disk. Run `vanna knowledge reindex`.",
        fg="yellow",
    )
