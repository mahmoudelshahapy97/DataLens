"""Checking a dashboard before it is saved, and again before it is rendered.

Twice, deliberately. A dashboard stored six months ago by a version of the code
with a weaker check is still going to be rendered today, and the render-time
pass is what stops that being someone else's problem.

Two properties this borrows from WrenAI's ``verify.py``, which got them right:

**Fail closed on an unknown kind.** A tile whose ``kind`` is not recognised is
rejected, not skipped. A typo that silently drops a panel from a dashboard
someone is reading numbers off is worse than an error.

**Default-deny the secret scan.** Every string field is scanned rather than a
chosen few. An allowlist of "fields that might contain a credential" loses the
race against whatever field gets added next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List

from .models import Dashboard, Tile, TileKind
from .params import (
    ParameterType,
    declared_placeholders,
    placeholders_in,
)

#: Shapes that indicate a credential pasted into a tile. Narrow on purpose:
#: this runs on SQL, and a pattern loose enough to catch everything would
#: reject legitimate queries, which teaches people to disable the check.
_SECRET_PATTERNS = (
    # user:password@host in a connection string
    re.compile(r"\b\w+://[^/\s:@]+:[^/\s@]+@[^/\s]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|bearer)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9+/=_\-]{8,}"
    ),
)


@dataclass
class VerifyIssue:
    severity: str          # "error" | "warning"
    message: str
    tile_id: str = ""

    def __str__(self) -> str:
        where = f" (tile {self.tile_id})" if self.tile_id else ""
        return f"[{self.severity}] {self.message}{where}"


def _strings(value: Any) -> Iterable[str]:
    """Every string anywhere in a nested structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _scan_for_secrets(payload: Any) -> List[str]:
    """Matched patterns, never the matched text.

    Reporting the match would copy the credential into a log and an error
    message -- turning a check that exists to contain a secret into a second
    place it leaks.
    """
    found: List[str] = []
    for text in _strings(payload):
        for index, pattern in enumerate(_SECRET_PATTERNS):
            if pattern.search(text):
                found.append(
                    ("a connection string with a password", "an AWS access key id",
                     "an inline credential")[index]
                )
    return sorted(set(found))


def verify_tile(tile: Tile) -> List[VerifyIssue]:
    issues: List[VerifyIssue] = []

    # Pydantic already rejects an unknown kind at parse time; this catches a
    # tile constructed in code and states the rule explicitly where a reader
    # will look for it.
    if tile.kind not in set(TileKind):
        issues.append(
            VerifyIssue("error", f"Unknown tile kind {tile.kind!r}.", tile.id)
        )
        return issues

    if tile.requires_query() and tile.query is None:
        issues.append(
            VerifyIssue("error", f"A {tile.kind.value} tile needs a query.", tile.id)
        )

    if tile.kind is TileKind.TEXT and not (tile.text or "").strip():
        issues.append(VerifyIssue("error", "A text tile needs text.", tile.id))

    if tile.kind is TileKind.CHART and tile.chart is None:
        # Not an error: the heuristics in PlotlyChartGenerator pick something
        # reasonable. Worth saying, because "reasonable" is rarely what someone
        # building a dashboard by hand had in mind.
        issues.append(
            VerifyIssue(
                "warning",
                "No chart spec; the chart type will be guessed from the data.",
                tile.id,
            )
        )

    for secret in _scan_for_secrets(tile.model_dump(mode="json")):
        issues.append(
            VerifyIssue("error", f"Tile appears to contain {secret}.", tile.id)
        )

    return issues


def _sql_of(tile: Tile) -> str:
    """The statement text a tile carries, for scanning. Empty when it has none."""
    query = tile.query
    for attribute in ("sql",):
        text = getattr(query, attribute, None)
        if isinstance(text, str):
            return text
    filters = getattr(query, "filters", None)
    if isinstance(filters, list):
        return " ".join(str(f) for f in filters)
    return ""


def verify_parameters(dashboard: Dashboard) -> List[VerifyIssue]:
    """The declarations, and whether the tiles agree with them.

    The important check is the last one. A statement using ``{{ since }}`` that no
    parameter declares would render as valid SQL with a silently different meaning --
    the failure a dashboard must not have, so it is an error at save time rather than
    a surprise at read time.
    """
    issues: List[VerifyIssue] = []

    seen = set()
    for parameter in dashboard.parameters:
        if parameter.name in seen:
            issues.append(
                VerifyIssue("error", f"Duplicate parameter {parameter.name!r}.")
            )
        seen.add(parameter.name)

        if parameter.type is ParameterType.ENUM:
            if not parameter.options:
                # Without options there is no allowlist, and without an allowlist the
                # value would have to be trusted.
                issues.append(
                    VerifyIssue(
                        "error",
                        f"Parameter {parameter.name!r} is an enum with no options, so "
                        "no value could ever be accepted for it.",
                    )
                )
            elif (
                parameter.default is not None
                and str(parameter.default) not in parameter.options
            ):
                issues.append(
                    VerifyIssue(
                        "error",
                        f"Parameter {parameter.name!r} defaults to "
                        f"{parameter.default!r}, which is not one of its options.",
                    )
                )

        if (
            parameter.type is ParameterType.INTEGER
            and parameter.minimum is not None
            and parameter.maximum is not None
            and parameter.minimum > parameter.maximum
        ):
            issues.append(
                VerifyIssue(
                    "error",
                    f"Parameter {parameter.name!r} has a minimum above its maximum.",
                )
            )

    available = declared_placeholders(dashboard.parameters)
    for tile in dashboard.tiles:
        used = placeholders_in(_sql_of(tile))
        for name in sorted(used - available):
            issues.append(
                VerifyIssue(
                    "error",
                    f"This tile uses {{{{ {name} }}}} but the report declares no "
                    f"parameter that fills it.",
                    tile.id,
                )
            )

    used_anywhere = set()
    for tile in dashboard.tiles:
        used_anywhere |= placeholders_in(_sql_of(tile))
    for name in sorted(available - used_anywhere):
        issues.append(
            VerifyIssue(
                "warning",
                f"Parameter placeholder {{{{ {name} }}}} is declared but no tile uses "
                "it, so changing it will appear to do nothing.",
            )
        )

    return issues


def verify_dashboard(dashboard: Dashboard) -> List[VerifyIssue]:
    """Every problem found, errors first."""
    issues: List[VerifyIssue] = []

    if not dashboard.title.strip():
        issues.append(VerifyIssue("error", "A dashboard needs a title."))

    issues.extend(verify_parameters(dashboard))

    if not dashboard.tiles:
        issues.append(VerifyIssue("warning", "This dashboard has no tiles."))

    seen = set()
    for tile in dashboard.tiles:
        if tile.id in seen:
            # Duplicate ids make the render results ambiguous -- two tiles would
            # claim the same TileResult.
            issues.append(VerifyIssue("error", f"Duplicate tile id {tile.id!r}."))
        seen.add(tile.id)
        issues.extend(verify_tile(tile))

    issues.sort(key=lambda i: 0 if i.severity == "error" else 1)
    return issues


def has_errors(issues: Iterable[VerifyIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)
