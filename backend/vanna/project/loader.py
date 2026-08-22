"""Reading and writing ``vanna_project.yml``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..config.env import find_project_root
from ..core.errors import ErrorCode, ErrorPhase, VannaError
from .layout import PROJECT_FILE, ProjectPaths

#: Bumped when the on-disk layout changes in a way that needs migration.
SCHEMA_VERSION = 1


@dataclass
class ProjectConfig:
    """The contents of ``vanna_project.yml``.

    Args:
        name: Human label, used in output.
        dialect: SQL dialect every model compiles to. Also the sqlglot dialect
            used to parse expressions, which is why it lives here rather than
            being inferred per query.
        profile: Connection profile this project belongs to. Pinning it here
            beats the globally active profile, so a project cannot start
            querying a different database because someone switched profiles in
            another terminal.
        fanout_guard: What to do when an aggregate crosses a one-to-many join
            and would silently double-count. ``warn`` is the default because
            the alternative to a warning is either a wrong number or a refusal,
            and a wrong number is the worst of the three.
    """

    name: str
    dialect: str = "sqlite"
    profile: Optional[str] = None
    description: str = ""
    fanout_guard: str = "warn"
    schema_version: int = SCHEMA_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    FANOUT_CHOICES = ("warn", "reject", "allow")

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "name": self.name,
            "dialect": self.dialect,
            "fanout_guard": self.fanout_guard,
        }
        if self.profile:
            payload["profile"] = self.profile
        if self.description:
            payload["description"] = self.description
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], *, source: Path) -> "ProjectConfig":
        data = dict(raw or {})

        version = int(data.pop("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise VannaError(
                ErrorCode.INVALID_PROJECT,
                f"{source} declares schema_version {version}, but this Vanna "
                f"understands up to {SCHEMA_VERSION}.",
                phase=ErrorPhase.PROJECT_LOAD,
                hint="Upgrade Vanna, or edit schema_version if you know the project is compatible.",
            )

        name = str(data.pop("name", "") or "").strip()
        if not name:
            raise VannaError(
                ErrorCode.INVALID_PROJECT,
                f"{source} has no 'name'.",
                phase=ErrorPhase.PROJECT_LOAD,
            )

        fanout = str(data.pop("fanout_guard", "warn") or "warn").lower()
        if fanout not in cls.FANOUT_CHOICES:
            raise VannaError(
                ErrorCode.INVALID_PROJECT,
                f"fanout_guard must be one of {', '.join(cls.FANOUT_CHOICES)}, "
                f"not {fanout!r}.",
                phase=ErrorPhase.PROJECT_LOAD,
            )

        return cls(
            name=name,
            dialect=str(data.pop("dialect", "sqlite") or "sqlite").lower(),
            profile=data.pop("profile", None),
            description=str(data.pop("description", "") or ""),
            fanout_guard=fanout,
            schema_version=version,
            extra=data,
        )


class Project:
    """A project on disk: its config and its paths."""

    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.paths = ProjectPaths(root)
        self.config = config

    @property
    def root(self) -> Path:
        return self.paths.root

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Project {self.config.name!r} at {self.root}>"

    # -- io ------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Project":
        """Load the project at ``path``, or the nearest one above the cwd."""
        root = Path(path).resolve() if path else find_project_root()
        if root is None:
            raise VannaError(
                ErrorCode.OBJECT_NOT_FOUND,
                "No Vanna project found here or in any parent directory.",
                phase=ErrorPhase.PROJECT_LOAD,
                hint="Create one with `vanna project init <name>`.",
            )

        project_file = root / PROJECT_FILE
        if not project_file.is_file():
            raise VannaError(
                ErrorCode.OBJECT_NOT_FOUND,
                f"{project_file} does not exist.",
                phase=ErrorPhase.PROJECT_LOAD,
                hint="Create one with `vanna project init <name>`.",
            )

        try:
            raw = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise VannaError(
                ErrorCode.INVALID_PROJECT,
                f"{project_file} is not valid YAML.",
                phase=ErrorPhase.PROJECT_LOAD,
                cause=exc,
            )

        return cls(root, ProjectConfig.from_dict(raw, source=project_file))

    def save(self) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.project_file.write_text(
            yaml.safe_dump(self.config.to_dict(), sort_keys=False),
            encoding="utf-8",
        )

    @staticmethod
    def find(path: Optional[Path] = None) -> Optional["Project"]:
        """Load the nearest project, or None. For callers where it is optional."""
        try:
            return Project.load(path)
        except VannaError:
            return None
