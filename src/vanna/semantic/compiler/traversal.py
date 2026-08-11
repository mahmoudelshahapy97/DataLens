"""Relationship paths: ``orders.customer.region``.

sqlglot cannot resolve these on its own, and would not be wrong to refuse:
``customer`` is an edge in a graph, not a column in a table, so
``qualify_columns`` sees an unknown identifier. Every path is therefore
rewritten to a plain qualified column against a join alias *before*
qualification runs, and the joins those aliases need are collected on the way.

Three decisions here are load-bearing:

**Alias by full path, never by model name.** ``orders.customer.region`` and
``orders.approver.region`` both land on ``customers``. Aliasing by model would
make the second silently read the first's join.

**Refuse MANY_TO_MANY.** There is no single correct join across one, and the
plausible guesses each produce a different number. A clear error beats a
cartesian product that looks like data.

**Report fan-out rather than hide it.** Traversing a one-to-many edge multiplies
the left side's rows. That is correct for listing and catastrophic for summing,
and only the reader knows which they meant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...core.errors import ErrorCode, ErrorPhase, VannaError
from ..models import JoinType, Manifest, SemanticModel

#: Characters allowed in a generated alias.
_SAFE = re.compile(r"[^A-Za-z0-9_]")


def alias_for_path(root_model: str, path: Tuple[str, ...]) -> str:
    """A join alias derived from the whole traversal path.

    ``orders`` + ``("customer", "region_owner")`` becomes
    ``_rel_customer__region_owner``. The leading underscore keeps it out of the
    way of real column names, and the full path keeps two routes to the same
    model distinct.
    """
    joined = "__".join(_SAFE.sub("_", part.lower()) for part in path)
    return f"_rel_{joined}"


@dataclass
class JoinStep:
    """One edge to add to a model's CTE."""

    alias: str
    target_model: str
    relationship_name: str
    condition: str
    join_type: JoinType
    path: Tuple[str, ...]

    @property
    def fans_out(self) -> bool:
        return self.join_type.fans_out


def projected_name(alias: str, column: str) -> str:
    """Name a traversed column takes as a column *of the model's CTE*.

    The join alias only exists inside the CTE, so every traversed column has to
    surface under a name the outer query can select. Prefixing with the alias
    keeps ``customer.region`` and ``approver.region`` distinct.
    """
    return f"{alias}_{_SAFE.sub('_', column.lower())}"


@dataclass
class TraversalPlan:
    """Every join a query needs, and which columns it must surface."""

    steps: Dict[str, JoinStep] = field(default_factory=dict)
    fan_out_paths: List[Tuple[str, ...]] = field(default_factory=list)
    #: alias -> the columns of the joined model that were actually referenced.
    _projections: Dict[str, List[str]] = field(default_factory=dict)

    def add(self, step: JoinStep) -> None:
        # Same alias means same path means same join; adding it twice would
        # produce a duplicate JOIN and a self-referential ambiguity error.
        if step.alias not in self.steps:
            self.steps[step.alias] = step
            if step.fans_out:
                self.fan_out_paths.append(step.path)

    def project(self, alias: str, column: str) -> None:
        """Record that a traversed column needs surfacing from the CTE."""
        columns = self._projections.setdefault(alias, [])
        if column not in columns:
            columns.append(column)

    def projections(self) -> List[Tuple[str, str]]:
        """``(alias, column)`` pairs, in a stable order."""
        return [
            (alias, column)
            for alias in sorted(self._projections)
            for column in sorted(self._projections[alias])
        ]

    @property
    def ordered(self) -> List[JoinStep]:
        """Joins in a stable order.

        Sorted by path depth first, so a two-hop join is emitted after the
        one-hop join whose alias it references.
        """
        return sorted(self.steps.values(), key=lambda s: (len(s.path), s.alias))


def resolve_path(
    manifest: Manifest,
    root: SemanticModel,
    parts: List[str],
    plan: TraversalPlan,
) -> Tuple[str, str]:
    """Resolve ``[handle, ..., column]`` to ``(qualifier, column)``.

    Walks the relationship columns, registering a join per hop, and returns the
    alias the final column should be qualified with.
    """
    current = root
    path: Tuple[str, ...] = ()
    qualifier = root.name

    for index, part in enumerate(parts[:-1]):
        column = current.column(part)
        if column is None or not column.is_relationship:
            raise VannaError(
                ErrorCode.COMPILATION_FAILED,
                f"{current.name!r} has no relationship named {part!r}.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
                hint=(
                    "Relationship columns on this model: "
                    + (", ".join(c.name for c in current.relationship_columns) or "none")
                ),
            )

        relationship = manifest.relationship(column.relationship or "")
        if relationship is None:
            raise VannaError(
                ErrorCode.INVALID_MANIFEST,
                f"relationship {column.relationship!r} is not defined.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
            )

        direction = relationship.direction_from(current.name)
        if direction is JoinType.MANY_TO_MANY:
            raise VannaError(
                ErrorCode.COMPILATION_FAILED,
                f"cannot traverse {relationship.name!r}: it is many-to-many.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
                hint=(
                    "Model the join table as its own model with two "
                    "relationships, so the intended join is explicit."
                ),
            )

        target_name = relationship.other(current.name)
        target = manifest.model(target_name or "")
        if target is None:
            raise VannaError(
                ErrorCode.INVALID_MANIFEST,
                f"relationship {relationship.name!r} points at {target_name!r}, "
                "which is not a model.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
            )

        path = path + (part,)
        alias = alias_for_path(root.name, path)

        plan.add(
            JoinStep(
                alias=alias,
                target_model=target.name,
                relationship_name=relationship.name,
                # The declared condition names models; the joined side becomes
                # an alias, so its references are rewritten to match.
                condition=_rewrite_condition(
                    relationship.condition,
                    from_model=current.name,
                    from_alias=qualifier,
                    to_model=target.name,
                    to_alias=alias,
                ),
                join_type=direction,
                path=path,
            )
        )

        current, qualifier = target, alias

    leaf = parts[-1]
    column = current.column(leaf)
    if column is None:
        raise VannaError(
            ErrorCode.COMPILATION_FAILED,
            f"{current.name!r} has no column {leaf!r}.",
            phase=ErrorPhase.SEMANTIC_COMPILE,
            hint="Columns: " + ", ".join(c.name for c in current.visible_columns),
        )

    return qualifier, column.name


def _rewrite_condition(
    condition: str,
    *,
    from_model: str,
    from_alias: str,
    to_model: str,
    to_alias: str,
) -> str:
    """Point a declared join condition at the aliases actually in scope.

    ``orders.customer_id = customers.id`` becomes
    ``orders.customer_id = _rel_customer.id``. Done textually on a word
    boundary: the condition is authored by a human against model names, and a
    full parse would need the target dialect for a substitution that is
    unambiguous without one.
    """
    result = condition
    for model, alias in ((to_model, to_alias), (from_model, from_alias)):
        if model.lower() == alias.lower():
            continue
        result = re.sub(
            rf"\b{re.escape(model)}\s*\.", f"{alias}.", result, flags=re.IGNORECASE
        )
    return result
