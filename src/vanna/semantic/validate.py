"""Manifest validation.

Everything checked here is something that would otherwise surface as a compiler
error, several steps removed from the line that caused it. "column not found:
_rel_customer_region" is not a message anyone can act on; "model 'orders'
declares a relationship column 'customer' pointing at relationship
'orders_customers', which does not exist" is.

Three levels, because the useful bar differs by situation:

* ``error``   -- the manifest cannot compile. Always enforced.
* ``warning`` -- it will compile but something is probably wrong.
* ``strict``  -- style and completeness, for a project that wants a clean bill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from .models import JoinType, Manifest, SemanticColumn, SemanticModel

#: Aggregate names that make an expression invalid at model level. A model CTE
#: has no GROUP BY, so `SUM(x)` in a calculated column produces SQL the database
#: rejects with a message about grouping that points nowhere near the manifest.
_AGGREGATES = frozenset(
    {"sum", "count", "avg", "min", "max", "median", "stddev", "variance",
     "array_agg", "string_agg", "group_concat", "listagg", "percentile_cont"}
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Severity(str):
    ERROR = "error"
    WARNING = "warning"
    STRICT = "strict"


@dataclass
class SemanticIssue:
    """One problem found in a manifest."""

    severity: str
    code: str
    message: str
    where: str = ""
    hint: str = ""

    def __str__(self) -> str:
        location = f" ({self.where})" if self.where else ""
        return f"[{self.severity}] {self.message}{location}"


def _identifiers(expression: str) -> Set[str]:
    """Bare identifiers in an expression, lowercased.

    Deliberately naive -- it over-collects function names and keywords. Callers
    only ever use it to ask "is this known?", so a false positive costs nothing
    and a parser costs a dependency and a dialect decision.
    """
    return {m.group(0).lower() for m in _IDENTIFIER.finditer(expression or "")}


def _contains_aggregate(expression: str) -> Optional[str]:
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression or ""):
        if match.group(1).lower() in _AGGREGATES:
            return match.group(1)
    return None


def _calculated_dependencies(
    model: SemanticModel, column: SemanticColumn
) -> Set[str]:
    """Which of the model's own columns a calculated expression references."""
    names = {c.name.lower() for c in model.columns}
    return {n for n in _identifiers(column.expression or "") if n in names}


def _detect_cycles(model: SemanticModel) -> List[List[str]]:
    """Calculated columns that depend on each other, directly or transitively."""
    graph: Dict[str, Set[str]] = {
        c.name.lower(): _calculated_dependencies(model, c) - {c.name.lower()}
        for c in model.columns
        if c.is_calculated
    }

    cycles: List[List[str]] = []
    visiting: Set[str] = set()
    done: Set[str] = set()

    def walk(node: str, path: List[str]) -> None:
        if node in done:
            return
        if node in visiting:
            start = path.index(node)
            cycles.append(path[start:] + [node])
            return
        visiting.add(node)
        for dependency in sorted(graph.get(node, ())):
            if dependency in graph:  # only calculated columns can form a cycle
                walk(dependency, path + [dependency])
        visiting.discard(node)
        done.add(node)

    for name in sorted(graph):
        walk(name, [name])
    return cycles


def validate_manifest(
    manifest: Manifest,
    *,
    level: str = Severity.ERROR,
    known_tables: Optional[Iterable[str]] = None,
) -> List[SemanticIssue]:
    """Check a manifest, returning every problem found.

    All checks run rather than stopping at the first failure, so an author can
    fix a batch in one pass instead of peeling them off one at a time.

    Args:
        level: Lowest severity to report. ``error`` is always included.
        known_tables: Physical tables, usually from a scan. When supplied, a
            model pointing at a table that does not exist is caught here rather
            than at the first query.
    """
    issues: List[SemanticIssue] = []
    model_names = {m.name.lower() for m in manifest.models}
    physical = {t.lower() for t in (known_tables or ())}

    # -- names --------------------------------------------------------
    seen: Set[str] = set()
    for name in [m.name for m in manifest.models] + [v.name for v in manifest.views]:
        if name.lower() in seen:
            issues.append(
                SemanticIssue(
                    Severity.ERROR,
                    "duplicate_name",
                    f"{name!r} is defined more than once.",
                    hint="Model and view names share one namespace; both can appear in FROM.",
                )
            )
        seen.add(name.lower())

    # -- models -------------------------------------------------------
    for model in manifest.models:
        where = f"model {model.name}"

        if bool(model.table_reference) == bool(model.ref_sql):
            issues.append(
                SemanticIssue(
                    Severity.ERROR,
                    "model_source",
                    f"{model.name!r} must set exactly one of table_reference and ref_sql.",
                    where,
                    hint="table_reference for a plain table; ref_sql for a query.",
                )
            )

        if not model.columns:
            issues.append(
                SemanticIssue(
                    Severity.ERROR, "model_empty",
                    f"{model.name!r} declares no columns.", where
                )
            )

        if physical and model.table_reference:
            table = model.table_reference.split(".")[-1].strip('"').lower()
            if table not in physical:
                issues.append(
                    SemanticIssue(
                        Severity.WARNING, "unknown_table",
                        f"{model.name!r} points at {model.table_reference!r}, "
                        "which the scanned catalog does not contain.",
                        where,
                        hint="Run `vanna project from-catalog` or check the name.",
                    )
                )

        if model.primary_key and not model.column(model.primary_key):
            issues.append(
                SemanticIssue(
                    Severity.ERROR, "unknown_primary_key",
                    f"primary_key {model.primary_key!r} is not one of "
                    f"{model.name!r}'s columns.",
                    where,
                )
            )

        for column in model.columns:
            column_where = f"{model.name}.{column.name}"

            if column.is_relationship:
                if manifest.relationship(column.relationship or "") is None:
                    issues.append(
                        SemanticIssue(
                            Severity.ERROR, "unknown_relationship",
                            f"column {column.name!r} references relationship "
                            f"{column.relationship!r}, which is not defined.",
                            column_where,
                        )
                    )
                elif column.type.lower() not in model_names:
                    issues.append(
                        SemanticIssue(
                            Severity.WARNING, "relationship_type",
                            f"a relationship column's type should be the related "
                            f"model's name; {column.type!r} is not a model.",
                            column_where,
                        )
                    )
                continue

            if column.is_calculated:
                if not column.expression:
                    issues.append(
                        SemanticIssue(
                            Severity.ERROR, "missing_expression",
                            f"{column.name!r} is calculated but has no expression.",
                            column_where,
                        )
                    )
                    continue

                aggregate = _contains_aggregate(column.expression)
                if aggregate:
                    issues.append(
                        SemanticIssue(
                            Severity.ERROR, "aggregate_in_model",
                            f"{column.name!r} uses {aggregate}(), which cannot appear "
                            "in a model column.",
                            column_where,
                            hint="A model row is one row; put aggregates in a cube measure.",
                        )
                    )

                known = {c.name.lower() for c in model.columns}
                referenced = _identifiers(column.expression)
                # Only flag names that look like they were meant to be columns:
                # anything followed by '(' is a function call, and literals and
                # keywords are filtered by requiring a near-miss on a real name.
                functions = {
                    m.group(1).lower()
                    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", column.expression)
                }
                if not (referenced - functions) & known:
                    issues.append(
                        SemanticIssue(
                            Severity.WARNING, "expression_references_nothing",
                            f"{column.name!r}'s expression references none of "
                            f"{model.name!r}'s columns.",
                            column_where,
                            hint="Expressions are written over this model's column names.",
                        )
                    )

            if level == Severity.STRICT and not column.description:
                issues.append(
                    SemanticIssue(
                        Severity.STRICT, "undocumented_column",
                        f"{column.name!r} has no description.", column_where
                    )
                )

        for cycle in _detect_cycles(model):
            issues.append(
                SemanticIssue(
                    Severity.ERROR, "calculated_cycle",
                    "calculated columns depend on each other: " + " -> ".join(cycle),
                    where,
                )
            )

        for rule in model.row_level_access_controls:
            declared = {p.name.lower() for p in rule.required_properties}
            used = {m.group(1).lower() for m in re.finditer(r"@([A-Za-z_][A-Za-z0-9_]*)", rule.condition)}
            for missing in sorted(used - declared):
                issues.append(
                    SemanticIssue(
                        Severity.ERROR, "undeclared_session_property",
                        f"row rule {rule.name!r} uses @{missing} but does not declare it.",
                        where,
                        hint="Add it to requiredProperties, or the rule cannot be resolved.",
                    )
                )
            if not used:
                issues.append(
                    SemanticIssue(
                        Severity.WARNING, "constant_row_rule",
                        f"row rule {rule.name!r} has no session property, so it "
                        "filters every caller identically.",
                        where,
                    )
                )

        for column in model.columns:
            rule = column.column_level_access_control
            if rule and len(rule.required_properties) != 1:
                issues.append(
                    SemanticIssue(
                        Severity.ERROR, "clac_properties",
                        f"column rule {rule.name!r} must require exactly one session "
                        f"property, not {len(rule.required_properties)}.",
                        f"{model.name}.{column.name}",
                    )
                )

    # -- relationships ------------------------------------------------
    for relationship in manifest.relationships:
        where = f"relationship {relationship.name}"
        for endpoint in relationship.models:
            if endpoint.lower() not in model_names:
                issues.append(
                    SemanticIssue(
                        Severity.ERROR, "dangling_relationship",
                        f"{endpoint!r} is not a model.", where
                    )
                )
        if not relationship.condition.strip():
            issues.append(
                SemanticIssue(
                    Severity.ERROR, "missing_condition",
                    f"{relationship.name!r} has no join condition.", where
                )
            )
        if relationship.join_type is JoinType.MANY_TO_MANY:
            issues.append(
                SemanticIssue(
                    Severity.WARNING, "many_to_many",
                    f"{relationship.name!r} is MANY_TO_MANY; the compiler refuses to "
                    "traverse it, because there is no single correct join.",
                    where,
                    hint="Model the join table explicitly as two relationships.",
                )
            )

    # -- cubes --------------------------------------------------------
    for cube in manifest.cubes:
        where = f"cube {cube.name}"
        base = manifest.model(cube.base_object)
        if base is None:
            issues.append(
                SemanticIssue(
                    Severity.ERROR, "unknown_base_object",
                    f"base_object {cube.base_object!r} is not a model.", where
                )
            )
            continue
        if not cube.measures:
            issues.append(
                SemanticIssue(
                    Severity.WARNING, "cube_without_measures",
                    f"{cube.name!r} declares no measures.", where
                )
            )
        for measure in cube.measures:
            if not _contains_aggregate(measure.expression):
                issues.append(
                    SemanticIssue(
                        Severity.WARNING, "measure_not_aggregated",
                        f"measure {measure.name!r} is not an aggregate.",
                        where,
                        hint="A measure is grouped by every selected dimension; "
                             "a bare column will error or return an arbitrary row.",
                    )
                )

    # -- views --------------------------------------------------------
    for view in manifest.views:
        if not view.statement.strip():
            issues.append(
                SemanticIssue(
                    Severity.ERROR, "empty_view",
                    f"view {view.name!r} has no statement.", f"view {view.name}"
                )
            )

    return [i for i in issues if _included(i.severity, level)]


_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.STRICT: 2}


def _included(severity: str, level: str) -> bool:
    return _ORDER.get(severity, 0) <= _ORDER.get(level, 0)


def has_errors(issues: Iterable[SemanticIssue]) -> bool:
    return any(i.severity == Severity.ERROR for i in issues)
