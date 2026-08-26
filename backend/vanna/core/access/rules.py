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
from typing import Any, Dict, List, Optional, Tuple

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


# ----------------------------------------------------------------------
# Masking
# ----------------------------------------------------------------------
#
# A third thing that can happen to a column, and the weakest. Read the note on
# `MASK_STRATEGIES` in `core/grants/models.py` for where it sits relative to
# dropping; what matters *here* is how it is applied.
#
# **The expression is rewritten inside the model, not applied to the result set.**
# That is the same argument as row rules, one paragraph up: a masked expression
# folded into a model's definition ends up inside that model's CTE, and a user's
# subquery, UNION or derived table sits above the CTE and cannot reach past it.
# Masking a result set after execution is defeated by any path that reads the rows
# first -- a cache keyed on (data source, SQL) with no notion of who is asking
# being the obvious one, and the one most products actually ship.

#: Per-dialect spellings. Only what differs; everything else uses the ANSI form.
#:
#: `hash` has no ANSI spelling at all, which is why the fallback is a truncation
#: rather than a checksum: a mask that silently does nothing on an unrecognised
#: dialect would be the worst possible failure here, so the fallback degrades to
#: something visibly redacted instead.
_MASK_SQL = {
    "hash": {
        "postgres": "'[' || substr(md5({src}::text), 1, 8) || ']'",
        "mysql": "concat('[', substr(md5(cast({src} as char)), 1, 8), ']')",
        "duckdb": "'[' || substr(md5(CAST({src} AS VARCHAR)), 1, 8) || ']'",
        "clickhouse": "concat('[', substring(hex(MD5(toString({src}))), 1, 8), ']')",
        # SQLite ships no hash function, so there is nothing to spell here. It
        # falls through to the constant below rather than to a truncation: a
        # truncation reveals the first characters, which is *more* than `hash`
        # promises, and a mask that quietly reveals more than it says is worse
        # than one that visibly reveals less.
        "oracle": "'[' || SUBSTR(RAWTOHEX(STANDARD_HASH(TO_CHAR({src}), 'MD5')), 1, 8) || ']'",
        None: "'[hashed]'",
    },
    "partial": {
        "postgres": "substr({src}::text, 1, 2) || '***'",
        "mysql": "concat(substr(cast({src} as char), 1, 2), '***')",
        "duckdb": "substr(CAST({src} AS VARCHAR), 1, 2) || '***'",
        "clickhouse": "concat(substring(toString({src}), 1, 2), '***')",
        "sqlite": "substr(CAST({src} AS TEXT), 1, 2) || '***'",
        "oracle": "SUBSTR(TO_CHAR({src}), 1, 2) || '***'",
        None: "'***'",
    },
    # NULL is the one spelling every dialect agrees on. Cast so the column keeps
    # its type: an untyped NULL changes the result schema, and a client that reads
    # by position gets a different shape than the one it was written against.
    "null": {None: "CAST(NULL AS VARCHAR)"},
}


def mask_expression(source: str, strategy: str, dialect: Optional[str] = None) -> str:
    """The SQL that replaces a column's expression when it is masked.

    ``source`` is the column's existing source expression -- its own name, or the
    physical column it renames, or a calculation. Masking wraps whatever was there
    rather than assuming a bare column name, so a mask on a calculated column
    masks the calculation's result rather than producing invalid SQL.
    """
    if strategy in (None, "", "none"):
        return source
    spellings = _MASK_SQL.get(strategy)
    if spellings is None:
        # An unrecognised strategy is a bug, and the safe reading of a bug in this
        # file is "reveal nothing".
        logger.error("Unknown mask strategy %r; masking to NULL.", strategy)
        return _MASK_SQL["null"][None].format(src=source)
    template = spellings.get((dialect or "").lower()) or spellings.get(None)
    return str(template).format(src=source)


def apply_column_masks(
    manifest: Manifest,
    masks: Dict[str, str],
    *,
    dialect: Optional[str] = None,
) -> Tuple[Manifest, List[str]]:
    """Rewrite masked columns' expressions. Returns a copy and what was masked.

    ``masks`` is keyed ``"model.column"``, casefolded -- the shape
    ``resolve_grants`` produces once its effective columns are walked.

    A masked column keeps ``can_filter``/``can_aggregate`` semantics unchanged,
    and that is worth stating because it looks like an oversight: filtering on a
    hashed column filters on the hash, which is exactly what makes a pseudonym
    useful, and aggregating a NULL-masked column produces NULL, which is the
    honest answer. Neither needs a separate flag.
    """
    scoped = manifest.model_copy(deep=True)
    masked: List[str] = []

    for model in scoped.models:
        for column in model.columns:
            key = f"{model.name}.{column.name}".casefold()
            strategy = masks.get(key)
            if not strategy or strategy == "none":
                continue

            column.expression = mask_expression(
                column.source_expression(), strategy, dialect
            )
            # A masked column is not a column anybody may write through: the
            # value that would be written is the masked one.
            column.is_calculated = True
            masked.append(f"{model.name}.{column.name}")

    return scoped, masked
