"""``.env`` discovery and ``${VAR}`` substitution.

Two small jobs that decide whether secrets ever touch a file we write.

Profiles store ``password: ${PGPASSWORD}``, not the password. The placeholder is
resolved at *connect* time, which is what makes ``vanna profile list`` and
``vanna profile show`` structurally incapable of printing a credential -- there
is nothing in the file to print. That property is worth more than any amount of
output masking, because masking is a thing a future edit can forget.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Marker that identifies a project root when walking up from the cwd.
PROJECT_MARKER = "vanna_project.yml"


#: The only thing substituted: a braced, UPPER_SNAKE name.
#:
#: Deliberately stricter than ``string.Template``, which was the obvious first
#: choice and is wrong here. Template also honours bare ``$NAME`` and collapses
#: ``$$`` to ``$`` -- both of which silently corrupt a password that legitimately
#: contains a dollar sign, producing an authentication failure nobody would
#: think to blame on templating. Braces-only means a value either is a
#: placeholder or is left exactly as written.
_PLACEHOLDER = re.compile(r"\$\{([_A-Z][_A-Z0-9]*)\}")


def parse_env_file(path: Path) -> Dict[str, str]:
    """Read a ``.env`` file into a dict.

    A ~30-line parser rather than a dependency on python-dotenv: this package
    already carries a large optional-extra matrix, and the format we need is
    ``KEY=value`` with comments, blank lines, an optional ``export`` prefix, and
    optional surrounding quotes.
    """
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return values

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # Strip one matched pair of quotes; anything inside is literal.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from ``start`` looking for a directory holding a project file."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / PROJECT_MARKER).is_file():
            return candidate
    return None


def env_search_path(project_root: Optional[Path] = None) -> List[Path]:
    """The ``.env`` files consulted, in precedence order.

    Nearest wins: a file in the working directory overrides the project's, which
    overrides the user's. That matches how every other layered config in a
    developer's life behaves, so it needs no explaining.
    """
    paths = [Path.cwd() / ".env"]
    root = project_root or find_project_root()
    if root is not None:
        paths.append(root / ".env")
    paths.append(Path.home() / ".vanna" / ".env")

    # De-duplicate while preserving order -- cwd and project root are often the
    # same directory, and reporting the same file twice in `profile debug`
    # looks like a bug.
    seen, unique = set(), []
    for path in paths:
        resolved = path.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def load_env(project_root: Optional[Path] = None) -> Dict[str, str]:
    """Merge the environment and every ``.env`` on the search path.

    The real process environment wins over every file: an operator who exported
    a variable to override a checked-in default expects that to work, and a file
    silently beating it would be the single most confusing possible behaviour.
    """
    merged: Dict[str, str] = {}
    for path in reversed(env_search_path(project_root)):  # furthest first
        if path.is_file():
            merged.update(parse_env_file(path))
    merged.update(os.environ)
    return merged


def resolve_placeholders(
    value: Any, environment: Dict[str, str]
) -> Tuple[Any, List[str]]:
    """Substitute ``${VAR}`` throughout a nested structure.

    Returns the resolved value and the names that could not be resolved, so the
    caller can fail with a message naming exactly what to export rather than a
    connection error thirty seconds later.
    """
    missing: List[str] = []

    def _substitute(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name in environment:
            return environment[name]
        # Leave the placeholder in place. The caller reports `missing` and
        # refuses to connect, so this text never reaches a driver -- but if it
        # somehow did, an unresolved `${PGPASSWORD}` in an error is far easier
        # to diagnose than a silently empty password.
        missing.append(name)
        return match.group(0)

    def _resolve(node: Any) -> Any:
        if isinstance(node, str):
            return _PLACEHOLDER.sub(_substitute, node)
        if isinstance(node, dict):
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(v) for v in node]
        return node

    return _resolve(value), sorted(set(missing))


#: Shapes that look like a live credential rather than a placeholder. Used to
#: warn when someone writes a real secret into a profile file.
_LITERAL_SECRET_PATTERNS = (
    re.compile(r"\b\w+://[^/\s:@]+:[^/\s@]+@"),   # user:password@host in a URL
    re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9+/=_\-]{24,}$"),  # long opaque token
)


def looks_like_literal_secret(value: str) -> bool:
    """Whether a value appears to be a real credential, not a ``${VAR}``."""
    if not isinstance(value, str) or "${" in value:
        return False
    return any(pattern.search(value) for pattern in _LITERAL_SECRET_PATTERNS)


def mask(value: Any) -> str:
    """Render a value for display without disclosing it.

    Placeholders are shown verbatim -- the whole point of storing ``${PGPASSWORD}``
    is that it is not a secret and seeing it is how you learn which variable to
    set.
    """
    text = str(value)
    if "${" in text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[0]}{'*' * min(len(text) - 2, 10)}{text[-1]}"
