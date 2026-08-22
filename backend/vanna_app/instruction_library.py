"""The shipped baseline and the starter packs, loaded from the repo.

Content lives outside the package, beside ``domains/domains.yml``, for the reason
that file's own docstring gives: rules people argue over should arrive as a
reviewable diff rather than as a string literal three call frames deep.

Loading is strict and happens at boot. A malformed baseline that starts the
process and quietly applies nothing is precisely the failure this whole feature
exists to prevent, so every problem raises :class:`InstructionContentError` and
the deployment refuses to start -- the same posture as ``config.validate``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from vanna.capabilities.knowledge import (
    Instruction,
    InstructionOrigin,
    InstructionScope,
)

logger = logging.getLogger("vanna.instructions.library")

def _content_root() -> Path:
    """Where the shipped instruction content lives.

    Two layouts, because the package sits at a different depth in each. In a
    checkout this file is ``backend/vanna_app/instruction_library.py`` and the
    content is ``backend/instructions/``; in the image the package is
    ``/app/vanna_app/`` and the Dockerfile lands the content at
    ``/app/instructions/``. Resolving by walking up until the directory is found
    keeps both working without an environment variable nobody would set.

    ``VANNA_INSTRUCTIONS_DIR`` overrides it for a deployment that mounts its own.
    """
    import os

    override = (os.getenv("VANNA_INSTRUCTIONS_DIR") or "").strip()
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "instructions"
        if (candidate / "baseline.yml").is_file() or (candidate / "packs").is_dir():
            return candidate
    # Nothing found: return the checkout-relative path so the log names something
    # a reader can act on rather than "/instructions".
    return here.parents[1] / "instructions"


_CONTENT = _content_root()

BASELINE_PATH = _CONTENT / "baseline.yml"
PACKS_DIR = _CONTENT / "packs"

#: A baseline id is permanent -- a workspace's decision to switch a rule off is
#: stored against it -- so the shape is constrained rather than left to taste.
_ID_RE = re.compile(r"^platform\.[a-z0-9][a-z0-9-]*$")


class InstructionContentError(ValueError):
    """Shipped instruction content is malformed. Raised at load, fails the boot."""


@dataclass(frozen=True)
class BaselineEntry:
    instruction: Instruction
    disableable: bool


@dataclass(frozen=True)
class Pack:
    id: str
    name: str
    description: str
    instructions: List[Instruction]


def _read_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise InstructionContentError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstructionContentError(f"{path}: expected a mapping at the top level")
    return raw


def _scope_of(entry: Dict[str, Any], where: str) -> InstructionScope:
    try:
        scope = InstructionScope(str(entry.get("scope", "global")))
    except ValueError as exc:
        raise InstructionContentError(
            f"{where}: {entry.get('scope')!r} is not a known scope"
        ) from exc
    if scope != InstructionScope.GLOBAL and not entry.get("scope_ref"):
        raise InstructionContentError(
            f"{where}: scope '{scope.value}' needs a scope_ref, or it applies to nothing"
        )
    return scope


def load_baseline(path: Path = BASELINE_PATH) -> List[BaselineEntry]:
    """Read the deployment-wide baseline. Empty when the file is absent."""
    if not path.is_file():
        logger.info("No baseline instructions at %s", path)
        return []

    raw = _read_yaml(path)
    entries: List[BaselineEntry] = []
    seen: Set[str] = set()

    for index, item in enumerate(raw.get("instructions") or []):
        where = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise InstructionContentError(f"{where}: expected a mapping")

        rule_id = str(item.get("id") or "").strip()
        if not _ID_RE.match(rule_id):
            raise InstructionContentError(
                f"{where}: id {rule_id!r} must look like 'platform.some-name' -- it "
                "is permanent, because a workspace's opt-out is stored against it"
            )
        if rule_id in seen:
            raise InstructionContentError(f"{where}: duplicate id {rule_id!r}")
        seen.add(rule_id)

        text = " ".join(str(item.get("text") or "").split())
        if not text:
            raise InstructionContentError(f"{where}: no rule text")

        entries.append(
            BaselineEntry(
                instruction=Instruction(
                    id=rule_id,
                    text=text,
                    scope=_scope_of(item, where),
                    scope_ref=item.get("scope_ref"),
                    priority=int(item.get("priority", 0) or 0),
                    origin=InstructionOrigin.PLATFORM,
                    locked=True,
                    created_by="platform",
                ),
                disableable=bool(item.get("disableable", False)),
            )
        )

    return entries


def load_packs(directory: Path = PACKS_DIR) -> Dict[str, Pack]:
    """Read every starter pack in ``directory``, keyed by id."""
    if not directory.is_dir():
        logger.info("No instruction packs at %s", directory)
        return {}

    packs: Dict[str, Pack] = {}
    for path in sorted(directory.glob("*.yml")):
        raw = _read_yaml(path)
        pack_id = str(raw.get("id") or "").strip()
        if pack_id != path.stem:
            raise InstructionContentError(
                f"{path}: id {pack_id!r} must match the filename stem {path.stem!r}, "
                "because the id is what a workspace's enablement is recorded against"
            )

        rules: List[Instruction] = []
        for index, item in enumerate(raw.get("instructions") or []):
            where = f"{path}[{index}]"
            if not isinstance(item, dict):
                raise InstructionContentError(f"{where}: expected a mapping")
            text = " ".join(str(item.get("text") or "").split())
            if not text:
                raise InstructionContentError(f"{where}: no rule text")
            rules.append(
                Instruction(
                    text=text,
                    scope=_scope_of(item, where),
                    scope_ref=item.get("scope_ref"),
                    priority=int(item.get("priority", 0) or 0),
                    origin=InstructionOrigin.LIBRARY,
                    source_pack=pack_id,
                )
            )

        if not rules:
            raise InstructionContentError(f"{path}: a pack with no rules")

        packs[pack_id] = Pack(
            id=pack_id,
            name=str(raw.get("name") or pack_id),
            description=" ".join(str(raw.get("description") or "").split()),
            instructions=rules,
        )

    return packs


class InstructionLibrary:
    """What the platform ships: the baseline, and the packs on offer."""

    def __init__(
        self,
        baseline: Optional[Sequence[BaselineEntry]] = None,
        packs: Optional[Dict[str, Pack]] = None,
    ) -> None:
        self.baseline = list(baseline or [])
        self.packs = dict(packs or {})

    @classmethod
    def load(
        cls,
        *,
        baseline_path: Path = BASELINE_PATH,
        packs_dir: Path = PACKS_DIR,
    ) -> "InstructionLibrary":
        library = cls(load_baseline(baseline_path), load_packs(packs_dir))
        logger.info(
            "Instruction library: %d baseline rule(s) (%d disableable), %d pack(s)",
            len(library.baseline),
            len(library.disableable_ids()),
            len(library.packs),
        )
        return library

    def baseline_instructions(self) -> List[Instruction]:
        """The baseline rules themselves.

        Returned as a list on every call so a caller mutating one copy cannot
        reach the loaded content, and so a reload is visible immediately.
        """
        return [entry.instruction for entry in self.baseline]

    def disableable_ids(self) -> Set[str]:
        return {e.instruction.id for e in self.baseline if e.disableable}

    def pack(self, pack_id: str) -> Optional[Pack]:
        return self.packs.get(pack_id)
