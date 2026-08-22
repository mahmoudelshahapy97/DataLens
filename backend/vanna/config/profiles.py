"""Named connection profiles.

One file, ``~/.vanna/profiles.yml``, holding every connection a user has and a
pointer to the active one. Values keep their ``${VAR}`` placeholders on disk and
are resolved only when a connection is opened -- see :mod:`vanna.config.env` for
why that ordering is the security property rather than the file permissions.

A note on ``chmod 0600``: it is applied, and on Windows it is close to a no-op,
because NTFS uses ACLs and not POSIX mode bits. It is defence in depth only. The
thing actually keeping credentials out of this file is that they are never
written to it.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..core.errors import ErrorCode, ErrorPhase, VannaError
from .env import (
    env_search_path,
    load_env,
    looks_like_literal_secret,
    resolve_placeholders,
)

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_PATH = Path.home() / ".vanna" / "profiles.yml"

#: Overrides the profiles file for every command, not just ``vanna profile``.
#: Without it there is no way to point the rest of the CLI at an alternate
#: file -- which makes an isolated test run, a CI job, or two projects on
#: different servers impossible to express.
PROFILES_PATH_ENV = "VANNA_PROFILES_PATH"


def default_profiles_path() -> Path:
    """The profiles file to use, honouring the environment override."""
    override = os.getenv(PROFILES_PATH_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_PROFILES_PATH


@dataclass
class Profile:
    """One named connection.

    ``settings`` is deliberately free-form. Every runner takes a different shape
    -- a DSN here, a project and dataset there, a file path for SQLite -- and a
    typed model per dialect would be twelve models to maintain for no gain at
    this layer. The runner factory validates what it needs.
    """

    name: str
    dialect: str
    settings: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"dialect": self.dialect, **self.settings}
        if self.description:
            payload["description"] = self.description
        return payload

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> "Profile":
        data = dict(raw or {})
        dialect = str(data.pop("dialect", "") or "").lower()
        if not dialect:
            raise VannaError(
                ErrorCode.MISCONFIGURED,
                f"Profile {name!r} has no 'dialect'.",
                phase=ErrorPhase.PROFILE_RESOLUTION,
                hint="Add e.g. `dialect: postgres` to the profile.",
            )
        description = str(data.pop("description", "") or "")
        return cls(name=name, dialect=dialect, settings=data, description=description)

    # -- resolution ----------------------------------------------------

    def resolve(self, environment: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Settings with every ``${VAR}`` substituted.

        Raises rather than connecting with a half-substituted value: a DSN
        containing a literal ``${PGPASSWORD}`` produces an authentication error
        that sends people looking at the database, not at their shell.
        """
        environment = environment if environment is not None else load_env()
        resolved, missing = resolve_placeholders(self.settings, environment)
        if missing:
            names = ", ".join(missing)
            raise VannaError(
                ErrorCode.MISCONFIGURED,
                f"Profile {self.name!r} needs {names}, which is not set.",
                phase=ErrorPhase.PROFILE_RESOLUTION,
                hint=(
                    f"Export {names}, or add it to one of: "
                    + ", ".join(str(p) for p in env_search_path())
                ),
                metadata={"missing": missing},
            )
        return resolved

    def literal_secret_fields(self) -> List[str]:
        """Setting keys whose value looks like a real credential."""
        return sorted(
            key
            for key, value in self.settings.items()
            if isinstance(value, str) and looks_like_literal_secret(value)
        )


class ProfileStore:
    """Reads and writes ``profiles.yml``."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_profiles_path()

    # -- io ------------------------------------------------------------

    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"profiles": {}, "active": None}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise VannaError(
                ErrorCode.MISCONFIGURED,
                f"{self.path} is not valid YAML.",
                phase=ErrorPhase.PROFILE_RESOLUTION,
                cause=exc,
            )
        data.setdefault("profiles", {})
        data.setdefault("active", None)
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(data, sort_keys=True, default_flow_style=False),
            encoding="utf-8",
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:  # pragma: no cover - platform dependent
            logger.debug("Could not chmod %s: %s", self.path, exc)
        if sys.platform == "win32":
            # Say so once, plainly. Pretending 0600 protected the file on
            # Windows would be worse than not trying.
            logger.debug(
                "%s is not ACL-protected on Windows; keep credentials in "
                "environment variables and reference them as ${VAR}.",
                self.path,
            )

    # -- api -----------------------------------------------------------

    def list(self) -> List[Profile]:
        data = self._read()
        return [
            Profile.from_dict(name, raw)
            for name, raw in sorted(data["profiles"].items())
        ]

    def get(self, name: str) -> Profile:
        data = self._read()
        if name not in data["profiles"]:
            known = ", ".join(sorted(data["profiles"])) or "none"
            raise VannaError(
                ErrorCode.OBJECT_NOT_FOUND,
                f"No profile named {name!r}.",
                phase=ErrorPhase.PROFILE_RESOLUTION,
                hint=f"Known profiles: {known}. Add one with `vanna profile add`.",
            )
        return Profile.from_dict(name, data["profiles"][name])

    def save(self, profile: Profile, *, activate: bool = False) -> None:
        data = self._read()
        data["profiles"][profile.name] = profile.to_dict()
        if activate or data.get("active") is None:
            data["active"] = profile.name
        self._write(data)

    def remove(self, name: str) -> None:
        data = self._read()
        if name not in data["profiles"]:
            raise VannaError(
                ErrorCode.OBJECT_NOT_FOUND,
                f"No profile named {name!r}.",
                phase=ErrorPhase.PROFILE_RESOLUTION,
            )
        del data["profiles"][name]
        if data.get("active") == name:
            # Leave the pointer dangling rather than silently promoting an
            # arbitrary other profile -- switching which database commands run
            # against, without being asked, is not a thing to do quietly.
            data["active"] = None
        self._write(data)

    def active_name(self) -> Optional[str]:
        return self._read().get("active")

    def set_active(self, name: str) -> None:
        self.get(name)  # existence check
        data = self._read()
        data["active"] = name
        self._write(data)

    def active(self) -> Optional[Profile]:
        name = self.active_name()
        return self.get(name) if name else None


def resolve_profile(
    name: Optional[str] = None,
    *,
    store: Optional[ProfileStore] = None,
    project_profile: Optional[str] = None,
) -> Profile:
    """Pick a profile: explicit name, then the project's, then the active one.

    The project's pinned profile beats the globally active one on purpose. A
    project that declares which connection it belongs to should not start
    querying a different database because someone ran ``vanna profile switch``
    in another terminal an hour ago.
    """
    store = store or ProfileStore()

    for candidate in (name, project_profile, store.active_name()):
        if candidate:
            return store.get(candidate)

    raise VannaError(
        ErrorCode.MISCONFIGURED,
        "No connection profile selected.",
        phase=ErrorPhase.PROFILE_RESOLUTION,
        hint="Create one with `vanna profile add`, or pass --profile.",
    )
