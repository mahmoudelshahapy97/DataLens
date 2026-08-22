"""Serving packaged agent skills.

A skill is a markdown workflow guide that ships *inside the wheel* and is served
on demand. The alternative -- copying guides into every agent client at install
time -- guarantees version drift: the CLI upgrades, the cached guide does not,
and the agent starts following instructions for commands that no longer exist.

So the client gets a ~50-line stub that knows only how to ask, and the content
always matches the installed version.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import resources
from typing import List, Optional

from ..core.errors import ErrorCode, ErrorPhase, VannaError

logger = logging.getLogger(__name__)

#: Package directory holding one subdirectory per skill.
_CONTENT_PACKAGE = "vanna.skills.content"

_SKILL_FILE = "SKILL.md"
_REFERENCES_DIR = "references"

#: Skill names are used as path segments. Restricting the character set means
#: neither the filesystem nor a future HTTP route has to defend itself.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class SkillSummary:
    name: str
    description: str

    def __str__(self) -> str:
        return f"{self.name:<20} {self.description}"


def _root():
    return resources.files(_CONTENT_PACKAGE)


def _validate_name(name: str) -> str:
    if not _SAFE_NAME.match(name or ""):
        raise VannaError(
            ErrorCode.INVALID_REQUEST,
            f"{name!r} is not a valid skill name.",
            phase=ErrorPhase.SKILL_DELIVERY,
            hint="Run `vanna skills list` to see what is available.",
        )
    return name


def _skill_dir(name: str):
    directory = _root() / _validate_name(name)
    if not directory.is_dir():
        raise VannaError(
            ErrorCode.OBJECT_NOT_FOUND,
            f"No skill named {name!r}.",
            phase=ErrorPhase.SKILL_DELIVERY,
            hint="Run `vanna skills list`.",
        )
    return directory


def _front_matter_description(text: str) -> str:
    """First sentence of the YAML front matter's ``description``.

    Parsed with a regex rather than a YAML load: this runs for every skill on
    every ``list``, the field is one line, and a malformed guide should still
    appear in the listing rather than break it.
    """
    match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return ""
    description = match.group(1).strip().strip("\"'")
    head, separator, _ = description.partition(". ")
    return (head + ("." if separator else "")) if len(head) < 100 else head[:97] + "..."


def list_skills() -> List[SkillSummary]:
    """Every packaged skill, with a one-line summary."""
    try:
        entries = sorted(_root().iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, ModuleNotFoundError):  # pragma: no cover
        return []

    summaries: List[SkillSummary] = []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        guide = entry / _SKILL_FILE
        if not guide.is_file():
            continue
        summaries.append(
            SkillSummary(entry.name, _front_matter_description(guide.read_text(encoding="utf-8")))
        )
    return summaries


def get_skill(name: str, *, full: bool = False) -> str:
    """One skill's guide, optionally with its reference files appended."""
    directory = _skill_dir(name)
    guide = directory / _SKILL_FILE
    if not guide.is_file():
        raise VannaError(
            ErrorCode.OBJECT_NOT_FOUND,
            f"Skill {name!r} has no {_SKILL_FILE}.",
            phase=ErrorPhase.SKILL_DELIVERY,
        )

    text = guide.read_text(encoding="utf-8")
    if not full:
        return text

    references = directory / _REFERENCES_DIR
    if not references.is_dir():
        return text

    parts = [text]
    for reference in sorted(references.iterdir(), key=lambda p: p.name):
        if reference.is_file() and reference.name.endswith(".md"):
            parts.append(
                f"# Reference: {reference.name.removesuffix('.md')}\n\n"
                + reference.read_text(encoding="utf-8")
            )
    return "\n\n---\n\n".join(parts)


def read_reference(name: str, reference: str) -> str:
    """One reference file belonging to a skill.

    The name is matched against the directory's own listing rather than joined
    onto a path. Path joining here would be a traversal primitive the moment
    this is exposed over MCP -- and it will be.
    """
    directory = _skill_dir(name) / _REFERENCES_DIR
    if not directory.is_dir():
        raise VannaError(
            ErrorCode.OBJECT_NOT_FOUND,
            f"Skill {name!r} has no references.",
            phase=ErrorPhase.SKILL_DELIVERY,
        )

    wanted = reference.removesuffix(".md")
    for candidate in directory.iterdir():
        if not candidate.is_file():
            continue
        if candidate.name.removesuffix(".md") == wanted:
            return candidate.read_text(encoding="utf-8")

    available = sorted(
        c.name.removesuffix(".md") for c in directory.iterdir() if c.is_file()
    )
    raise VannaError(
        ErrorCode.OBJECT_NOT_FOUND,
        f"Skill {name!r} has no reference {reference!r}.",
        phase=ErrorPhase.SKILL_DELIVERY,
        hint="Available: " + (", ".join(available) or "none"),
    )
