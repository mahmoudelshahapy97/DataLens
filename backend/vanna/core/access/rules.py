"""Applying row- and column-level rules to a manifest.

Both work by rewriting the *manifest* before compilation, not by editing SQL
afterwards. That choice is the entire security argument:

**Rows.** A predicate added to a model's definition ends up inside that model's
CTE. A user's subquery, UNION, or derived table sits *above* the CTE and cannot
remove what is inside it. A predicate bolted onto the outer WHERE, by contrast,
is defeated by ``SELECT * FROM (SELECT * FROM orders) x``.

**Columns.** A blocked column is *removed from the model*, so referencing it is
an unknown-column error the agent can report. The alternative -- projecting
NULL -- silently corrupts ``AVG`` and ``SUM``, and the reader is never told the
number is wrong.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ...semantic.models import (
    ColumnLevelAccessControl,
    Manifest,
    NormalizedExprType,
    SemanticModel,
)
from ..errors import ErrorCode, ErrorPhase, VannaError
from .session import SessionProperties, require

logger = logging.getLogger(__name__)

_PROPERTY_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")


@dataclass
class AccessDecision:
    """What access control did to a manifest for one caller."""

    manifest: Manifest
    applied_row_rules: List[str] = field(default_factory=list)
    dropped_columns: List[str] = field(default_factory=list)


def _literal(value: Any) -> str:
    """Render a session value as a SQL literal.

    Escaped by doubling quotes rather than parameterised, because the predicate
    is spliced into a model definition that is later parsed as SQL -- there is
    no bind-parameter channel at this layer. Values originate from the resolved
    user, not from request input, which is what keeps the exposure bounded.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        rendered = ", ".join(_literal(v) for v in value)
        return f"({rendered})" if rendered else "(NULL)"
    return "'" + str(value).replace("'", "''") + "'"


def apply_row_rules(
    manifest: Manifest, properties: SessionProperties
) -> Tuple[Manifest, List[str]]:
    """Fold every model's row rules into its definition.

    Returns a *copy* of the manifest. Mutating the shared one would leak one
    caller's predicate into the next request served by the same process, which
    is the worst possible bug in this file.
    """
    scoped = manifest.model_copy(deep=True)
    applied: List[str] = []

    for model in scoped.models:
        if not model.row_level_access_controls:
            continue

        predicates: List[str] = []
        for rule in model.row_level_access_controls:
            values = require(properties, rule.required_properties, rule_name=rule.name)

            # `values` and `rule` are bound as defaults rather than closed over.
            # The substitution runs inside this iteration, so late binding would
            # not bite today -- but this is the function that decides which rows a
            # user may see, and "correct only because of when it happens to be
            # called" is not a property to leave in it.
            def substitute(
                match: "re.Match[str]",
                values: Dict[str, Any] = values,
                rule: Any = rule,
            ) -> str:
                name = match.group(1).lower()
                if name not in values:
                    # Declared-property validation runs at build time, so this
                    # is a manifest that changed underneath us. Fail closed.
                    raise VannaError(
                        ErrorCode.PERMISSION_DENIED,
                        f"Rule {rule.name!r} references @{name}, which it does "
                        "not declare.",
                        phase=ErrorPhase.ACCESS_CONTROL,
                    )
                return _literal(values[name])

            predicates.append(f"({_PROPERTY_REF.sub(substitute, rule.condition)})")
            applied.append(f"{model.name}.{rule.name}")

        _push_predicate(model, " AND ".join(predicates))

    return scoped, applied


def _push_predicate(model: SemanticModel, predicate: str) -> None:
    """Constrain a model's source so the predicate cannot be escaped.

    For a table-backed model the source becomes a filtered subquery; for a
    query-backed one the existing statement is wrapped. Either way the filter
    ends up inside the CTE the compiler builds, beneath anything the user
    writes.
    """
    if model.ref_sql:
        model.ref_sql = (
            f"SELECT * FROM ({model.ref_sql}) AS _rls WHERE {predicate}"
        )
    else:
        table = model.table_reference or model.name
        model.ref_sql = f"SELECT * FROM {table} WHERE {predicate}"
        # Exactly one source may be set, and it is now the query.
        model.table_reference = None


def _column_allowed(
    rule: ColumnLevelAccessControl, properties: SessionProperties
) -> bool:
    """Whether this caller meets a column rule's threshold."""
    values = require(properties, rule.required_properties, rule_name=rule.name)
    if not values:
        return False

    actual = next(iter(values.values()))
    if actual is None:
        return False

    threshold: Any = rule.threshold.value
    if rule.threshold.data_type is NormalizedExprType.NUMERIC:
        try:
            actual, threshold = float(actual), float(threshold)
        except (TypeError, ValueError):
            # A numeric rule against a non-numeric value cannot be evaluated.
            # Denying is the only safe reading.
            logger.warning(
                "Column rule %r expected a number, got %r; denying.",
                rule.name, actual,
            )
            return False
    else:
        actual, threshold = str(actual), str(threshold)

    operator = rule.operator.value
    return {
        "EQUALS": actual == threshold,
        "NOT_EQUALS": actual != threshold,
        "GREATER_THAN": actual > threshold,
        "LESS_THAN": actual < threshold,
        "GREATER_THAN_OR_EQUALS": actual >= threshold,
        "LESS_THAN_OR_EQUALS": actual <= threshold,
    }[operator]


def apply_column_rules(
    manifest: Manifest, properties: SessionProperties
) -> Tuple[Manifest, List[str]]:
    """Remove columns this caller may not read.

    Removed, not nulled. An unknown-column error is a fact the agent can relay;
    a NULL is a wrong average nobody questions.
    """
    scoped = manifest.model_copy(deep=True)
    dropped: List[str] = []

    for model in scoped.models:
        keep = []
        for column in model.columns:
            rule = column.column_level_access_control
            if rule is None or _column_allowed(rule, properties):
                keep.append(column)
            else:
                dropped.append(f"{model.name}.{column.name}")

        if len(keep) != len(model.columns):
            model.columns = keep

    return scoped, dropped


def apply_access_rules(
    manifest: Manifest, properties: SessionProperties
) -> AccessDecision:
    """Both halves, in the order that matters.

    Columns first: a row rule may legitimately filter on a column this caller
    cannot *select*, and dropping the column first would break the rule that
    protects them.
    """
    with_columns, dropped = apply_column_rules(manifest, properties)
    restored: Dict[str, List[str]] = {}

    # Re-attach any column a row rule needs, marked hidden so it is enforceable
    # but not readable.
    for model in with_columns.models:
        original = manifest.model(model.name)
        if original is None or not model.row_level_access_controls:
            continue
        needed = set()
        for rule in model.row_level_access_controls:
            needed |= {
                token.lower()
                for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rule.condition)
            }
        present = {c.name.lower() for c in model.columns}
        for column in original.columns:
            if column.name.lower() in needed and column.name.lower() not in present:
                hidden = column.model_copy(deep=True)
                hidden.is_hidden = True
                hidden.column_level_access_control = None
                model.columns.append(hidden)
                restored.setdefault(model.name, []).append(column.name)

    if restored:
        logger.debug("Restored columns needed by row rules: %s", restored)

    final, applied = apply_row_rules(with_columns, properties)
    return AccessDecision(
        manifest=final, applied_row_rules=applied, dropped_columns=dropped
    )
