"""Loading the configuration files under ``backend/`` into the catalog.

The one-way bridge from the files to the database. It runs from
``backend/tools/import_config_files.py``, from ``backend/tools/seed_database.py``, and at boot when
``VANNA_CONFIG_BOOTSTRAP`` is set and the catalog is empty -- which is what makes
``docker compose up`` on a fresh volume work without a manual step.

Three properties, and each of them is a decision:

**Idempotent by checksum.** Re-importing a file nobody edited writes nothing, moves
no timestamp and adds no history row. That is what lets this run on every boot.

**Nothing is silently skipped.** A YAML file that will not parse is stored with
``parsed = NULL`` and *reported*. A file dropped quietly is a configuration
difference that only shows up as a missing cube weeks later.

**Secrets are refused, not stored.** ``config_files`` must not become a second
place credentials live. A file carrying one fails the import naming the key, and
``--redact`` is the escape hatch for content that has to come in anyway.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config_store import (
    CONFIG_EXTENSIONS,
    SKIP_DIRECTORIES,
    ConfigParseError,
    ConfigRecord,
    find_secrets,
    normalise_path,
    parse_content,
    redact,
)

logger = logging.getLogger("vanna.config_import")


def content_root(override: str = "") -> Path:
    """The directory the catalog's paths are relative to.

    ``backend/`` in a checkout and ``/app/`` in the image, found the way
    ``instruction_library`` and ``domains`` already find their content: by walking
    up looking for the directories that mark it, rather than counting ``..`` --
    which is silently wrong in one of the two layouts.
    """
    if override:
        return Path(override).resolve()

    env = (os.getenv("VANNA_CONFIG_ROOT") or "").strip()
    if env:
        return Path(env).resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "projects").is_dir() or (parent / "instructions").is_dir():
            return parent
    return here.parents[1]


def discover(root: Path) -> List[Path]:
    """Every catalogable file under ``root``, in a stable order.

    Sorted so two runs report changes in the same order, and so a diff of two
    import logs is readable.
    """
    found: List[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(
            name for name in subdirectories if name not in SKIP_DIRECTORIES
        )
        for filename in sorted(filenames):
            if filename.endswith(CONFIG_EXTENSIONS):
                found.append(Path(directory) / filename)
    return found


@dataclass
class FileOutcome:
    relative_path: str
    kind: str
    result: str  # created | updated | unchanged | unparseable | refused | failed
    detail: str = ""


@dataclass
class ImportReport:
    """What one import run did, in enough detail to act on."""

    root: str = ""
    outcomes: List[FileOutcome] = field(default_factory=list)
    dry_run: bool = False

    def _of(self, *results: str) -> List[FileOutcome]:
        return [o for o in self.outcomes if o.result in results]

    @property
    def created(self) -> List[FileOutcome]:
        return self._of("created")

    @property
    def updated(self) -> List[FileOutcome]:
        return self._of("updated")

    @property
    def unchanged(self) -> List[FileOutcome]:
        return self._of("unchanged")

    @property
    def unparseable(self) -> List[FileOutcome]:
        return self._of("unparseable")

    @property
    def refused(self) -> List[FileOutcome]:
        return self._of("refused")

    @property
    def failed(self) -> List[FileOutcome]:
        return self._of("failed")

    @property
    def ok(self) -> bool:
        """Whether the run is safe to treat as a success.

        An unparseable file is not a failure -- it is stored, reported, and may
        well be a ``.md`` rule that never had structure. A refused or failed one
        is: the catalog does not hold what the caller asked it to hold.
        """
        return not self.refused and not self.failed

    def summary(self) -> str:
        parts = [
            f"{len(self.created)} new",
            f"{len(self.updated)} changed",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.unparseable:
            parts.append(f"{len(self.unparseable)} unparseable")
        if self.refused:
            parts.append(f"{len(self.refused)} refused")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


def read_record(
    path: Path,
    root: Path,
    *,
    allow_secrets: bool = False,
    redact_secrets: bool = False,
) -> Tuple[Optional[ConfigRecord], Optional[FileOutcome]]:
    """One file as a record, or the outcome explaining why it is not one.

    Exactly one of the two is None. Splitting the read from the write is what lets
    ``--dry-run`` report refusals and parse failures without touching the
    database.
    """
    relative = normalise_path(str(path.relative_to(root)))
    raw = path.read_text(encoding="utf-8")

    parsed: Any = None
    unparseable = ""
    try:
        parsed = parse_content(relative, raw)
    except ConfigParseError as exc:
        # Stored anyway, with parsed = NULL. The runtime will not use it -- it
        # reads `parsed` -- but the file is recorded and the caller is told, which
        # is strictly better than a silent skip.
        unparseable = str(exc)

    secrets = find_secrets(parsed, raw)
    if secrets and not (allow_secrets or redact_secrets):
        return None, FileOutcome(
            relative,
            "",
            "refused",
            "looks like it carries a credential at "
            + ", ".join(secrets[:4])
            + ". Warehouse credentials belong in tenant_datasources, encrypted. "
            "Re-run with --redact to store it with those values masked.",
        )

    if secrets and redact_secrets:
        parsed = redact(parsed)

    record = ConfigRecord.from_file(
        relative,
        raw,
        parsed=parsed,
        metadata=(
            {"unparseable": unparseable}
            if unparseable
            else ({"redacted": secrets} if secrets and redact_secrets else {})
        ),
    )
    if unparseable:
        return record, FileOutcome(relative, record.kind, "unparseable", unparseable)
    return record, None


def import_files(
    store: Any,
    *,
    root: Optional[Path] = None,
    paths: Optional[Sequence[Path]] = None,
    actor: str = "importer",
    dry_run: bool = False,
    allow_secrets: bool = False,
    redact_secrets: bool = False,
    project_into: Optional[Any] = None,
) -> ImportReport:
    """Import every configuration file under ``root``. Returns what happened.

    Synchronous, and that is deliberate. This runs from ``create_app`` before
    there is an event loop and from ``backend/tools/`` scripts that have none, over a few
    dozen small files exactly once -- so it takes ``AppDatabase``'s synchronous
    route. Wrapping the async store in ``asyncio.run`` here would bind that
    store's semaphore to a loop that is about to be thrown away, and the loop
    uvicorn starts afterwards would find the gate bound elsewhere.
    """
    base = Path(root) if root is not None else content_root()
    report = ImportReport(root=str(base), dry_run=dry_run)

    for path in list(paths) if paths is not None else discover(base):
        record, outcome = read_record(
            Path(path),
            base,
            allow_secrets=allow_secrets,
            redact_secrets=redact_secrets,
        )
        if record is None:
            # Refused: reported and not stored.
            report.outcomes.append(outcome)  # type: ignore[arg-type]
            continue
        if outcome is not None:
            # Stored, but the caller needs to know it has no parsed form.
            report.outcomes.append(outcome)

        if dry_run:
            existing = store.get_sync(
                record.relative_path,
                scope=record.scope,
                tenant_id=record.tenant_id,
                project=record.project,
            )
            if existing is None:
                result = "created"
            elif existing.checksum == record.checksum:
                result = "unchanged"
            else:
                result = "updated"
        else:
            try:
                result = store.put_sync(
                    record, source="import", actor=actor, project_into=project_into
                )
            except Exception as exc:  # noqa: BLE001 - per file, not fatal
                logger.error("Could not store %s: %s", record.relative_path, exc)
                report.outcomes.append(
                    FileOutcome(record.relative_path, record.kind, "failed", str(exc))
                )
                continue

        if outcome is None:
            report.outcomes.append(
                FileOutcome(record.relative_path, record.kind, result)
            )

    logger.info(
        "Configuration import from %s: %s%s",
        base,
        report.summary(),
        " (dry run)" if dry_run else "",
    )
    return report


def bootstrap_if_empty(
    store: Any, *, root: Optional[Path] = None, project_into: Optional[Any] = None
) -> Optional[ImportReport]:
    """Import the shipped files, but only into an empty catalog.

    The boot-time path. "Only if empty" is the whole safety property: once an
    administrator has edited a cube through the API, a restart must not quietly
    reinstate whatever YAML the image was built with. A deployment that wants the
    files to win says so by running the importer itself.
    """
    if store.list_sync(with_content=False):
        return None

    logger.warning(
        "The configuration catalog is empty -- importing the shipped files. This "
        "happens once, on a fresh volume. Set VANNA_CONFIG_BOOTSTRAP=false to "
        "manage configuration entirely through the API."
    )
    report = import_files(
        store, root=root, actor="bootstrap", project_into=project_into
    )
    for line in describe(report):
        logger.info("bootstrap: %s", line)
    return report


def describe(report: ImportReport) -> List[str]:
    """The report as lines for a terminal or a log."""
    lines = [f"root: {report.root}", f"result: {report.summary()}"]
    for outcome in report.outcomes:
        if outcome.result == "unchanged":
            continue
        detail = f"  -- {outcome.detail}" if outcome.detail else ""
        lines.append(f"{outcome.result:12} {outcome.relative_path}{detail}")
    return lines
