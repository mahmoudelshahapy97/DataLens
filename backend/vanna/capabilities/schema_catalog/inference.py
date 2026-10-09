"""Infer join relationships a database never declared.

Plenty of warehouses declare no foreign keys at all -- loaded by ELT tools,
migrated from systems without constraints, or built by people who consider
constraints a write-path cost. For those the catalog has no edges, the join
graph is empty, and the model is back to guessing joins on same-named columns.

The guess this module makes is the one a person reading the schema makes:
``orders.customer_id`` refers to ``customers``' key. Each candidate is scored:

* the referencing column is named ``<stem>_id`` / ``<stem>id`` and a table
  named ``<stem>`` (or its plural) exists in the same schema, whose
  single-column primary key is named like the referencing column (0.85) or
  plain ``id`` (0.8);
* failing that, the column is named exactly like the single-column key of one
  other table in the schema (0.85) -- ``custid`` -> ``customer.custid``;
* the two column types are in the same family -- an integer cannot reference
  a uuid. Unknown types cost 0.1 rather than disqualifying.

The scanner can then sample the data (:func:`containment_sql`): a column whose
non-null values all exist in the target key is raised to 0.95; one with more
than 5% orphans drops to 0.3, below anything that reaches a prompt.

Everything inferred is stored ``review_status="proposed"`` with
``origin="inferred"``. Only edges at or above
:data:`~vanna.capabilities.schema_catalog.models.INFERRED_MIN_CONFIDENCE` are
used before an admin reviews them, and they are labelled as inferred wherever
they are shown.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import ColumnMetadata, RelationshipMetadata, TableMetadata

#: Confidence by how the target key is named.
SAME_NAME_CONFIDENCE = 0.85
ID_NAME_CONFIDENCE = 0.8
UNKNOWN_TYPE_PENALTY = 0.1

#: After sampling: every value found, or too many missing.
VERIFIED_CONFIDENCE = 0.95
ORPHANED_CONFIDENCE = 0.3
MAX_ORPHAN_RATIO = 0.05

#: Rows sampled from the referencing column when verifying.
VERIFY_SAMPLE_ROWS = 200

_FAMILIES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("uuid", ("uuid", "uniqueidentifier")),
    ("integer", ("int", "serial", "number", "numeric", "decimal")),
    ("text", ("char", "text", "string", "clob")),
)


def type_family(data_type: Optional[str]) -> Optional[str]:
    """Coarse type family, or None when the type says nothing useful."""
    lowered = (data_type or "").lower()
    if not lowered or lowered == "unknown":
        return None
    for family, hints in _FAMILIES:
        if any(hint in lowered for hint in hints):
            return family
    return "other"


def _stems(column: str) -> List[str]:
    """``customer_id`` / ``CustomerID`` / ``customerid`` -> ``customer``."""
    lowered = column.lower()
    for suffix in ("_id", "id"):
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 1:
            return [lowered[: -len(suffix)].rstrip("_")]
    return []


def _table_names_for(stem: str) -> List[str]:
    names = [stem, f"{stem}s", f"{stem}es"]
    if stem.endswith("y"):
        names.append(f"{stem[:-1]}ies")
    return names


def _sole_key(table: TableMetadata) -> Optional[ColumnMetadata]:
    keys = [c for c in table.columns if c.is_primary_key]
    return keys[0] if len(keys) == 1 else None


def infer_relationships(
    tables: Sequence[TableMetadata],
    declared: Iterable[RelationshipMetadata] = (),
    *,
    data_source_id: str = "default",
) -> List[RelationshipMetadata]:
    """Candidate relationships for columns no declared foreign key covers."""
    covered: Set[Tuple[str, str]] = {
        (r.from_table.lower(), r.from_column.lower()) for r in declared
    }
    for table in tables:
        for column in table.columns:
            if column.foreign_key is not None:
                covered.add((table.qualified_name.lower(), column.name.lower()))

    # (schema, bare name) -> table; schema None matches only schema None.
    by_name: Dict[Tuple[Optional[str], str], TableMetadata] = {
        ((t.schema_name or "").lower() or None, t.table_name.lower()): t for t in tables
    }

    inferred: List[RelationshipMetadata] = []
    for table in tables:
        schema = (table.schema_name or "").lower() or None
        own_key = _sole_key(table)
        for column in table.columns:
            if (table.qualified_name.lower(), column.name.lower()) in covered:
                continue
            if own_key is not None and own_key.name.lower() == column.name.lower():
                continue  # a table's own key references nothing by naming alone
            match = _target_for(column, schema, table, by_name)
            if match is None:
                continue
            target, key, confidence = match
            inferred.append(
                RelationshipMetadata(
                    name=f"{table.qualified_name}.{column.name}"
                    f"->{target.qualified_name} (inferred)",
                    from_table=table.qualified_name,
                    from_column=column.name,
                    to_table=target.qualified_name,
                    to_column=key.name,
                    join_type="many_to_one",
                    description="Inferred from column names and types; no "
                    "foreign key is declared.",
                    tenant_id=table.tenant_id,
                    data_source_id=data_source_id,
                    origin="inferred",
                    confidence=round(confidence, 2),
                    review_status="proposed",
                )
            )
    return inferred


def _target_for(
    column: ColumnMetadata,
    schema: Optional[str],
    table: TableMetadata,
    by_name: Dict[Tuple[Optional[str], str], TableMetadata],
) -> Optional[Tuple[TableMetadata, ColumnMetadata, float]]:
    return _by_stem(column, schema, table, by_name) or _by_key_name(
        column, schema, table, by_name
    )


def _by_key_name(
    column: ColumnMetadata,
    schema: Optional[str],
    table: TableMetadata,
    by_name: Dict[Tuple[Optional[str], str], TableMetadata],
) -> Optional[Tuple[TableMetadata, ColumnMetadata, float]]:
    """A column named exactly like one other table's single-column key.

    Catches the schemas whose key names do not follow their table names --
    Northwind's ``salesorder.custid`` -> ``customer.custid``, ``orderdetail.
    orderid`` -> ``salesorder.orderid``. Only when exactly one table has such a
    key, and never for a bare ``id``, which every table has.
    """
    name = column.name.lower()
    if name == "id":
        return None
    matches = [
        (target, key)
        for (target_schema, _), target in by_name.items()
        if target_schema == schema and target is not table
        for key in [_sole_key(target)]
        if key is not None and key.name.lower() == name
    ]
    if len(matches) != 1:
        return None
    target, key = matches[0]
    return _scored(column, key, target, SAME_NAME_CONFIDENCE)


def _scored(
    column: ColumnMetadata, key: ColumnMetadata, target: TableMetadata, confidence: float
) -> Optional[Tuple[TableMetadata, ColumnMetadata, float]]:
    """Apply the type check: incompatible families disqualify, unknown costs."""
    ours, theirs = type_family(column.data_type), type_family(key.data_type)
    if ours and theirs and ours != theirs:
        return None
    if ours is None or theirs is None:
        confidence -= UNKNOWN_TYPE_PENALTY
    return target, key, confidence


def _by_stem(
    column: ColumnMetadata,
    schema: Optional[str],
    table: TableMetadata,
    by_name: Dict[Tuple[Optional[str], str], TableMetadata],
) -> Optional[Tuple[TableMetadata, ColumnMetadata, float]]:
    for stem in _stems(column.name):
        for name in _table_names_for(stem):
            target = by_name.get((schema, name))
            if target is None or target is table:
                continue
            key = _sole_key(target)
            if key is None:
                continue
            key_name = key.name.lower()
            if key_name == column.name.lower():
                confidence = SAME_NAME_CONFIDENCE
            elif key_name == "id":
                confidence = ID_NAME_CONFIDENCE
            else:
                continue
            scored = _scored(column, key, target, confidence)
            if scored is not None:
                return scored
    return None


def containment_sql(rel: RelationshipMetadata, *, sample: int = VERIFY_SAMPLE_ROWS) -> str:
    """One query: how many of a sample of referencing values lack a target.

    Returns columns ``sampled`` and ``orphans``. Bounded by the sample, so it
    costs the same on a ten-row table as on a billion-row one. Identifiers are
    double-quoted, as the scanner's own profiling queries are.
    """
    def q(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def table(name: str) -> str:
        return ".".join(q(part) for part in name.split("."))

    return (
        f"SELECT COUNT(*) AS sampled, "
        f"SUM(CASE WHEN EXISTS (SELECT 1 FROM {table(rel.to_table)} t "
        f"WHERE t.{q(rel.to_column)} = s.v) THEN 0 ELSE 1 END) AS orphans "
        f"FROM (SELECT {q(rel.from_column)} AS v FROM {table(rel.from_table)} "
        f"WHERE {q(rel.from_column)} IS NOT NULL LIMIT {int(sample)}) s"
    )


def rescore(rel: RelationshipMetadata, sampled: int, orphans: int) -> RelationshipMetadata:
    """Apply a containment sample to *rel*'s confidence. Empty samples change nothing."""
    if sampled <= 0:
        return rel
    ratio = orphans / sampled
    if orphans == 0:
        confidence = max(rel.confidence or 0.0, VERIFIED_CONFIDENCE)
    elif ratio > MAX_ORPHAN_RATIO:
        confidence = min(rel.confidence or 0.0, ORPHANED_CONFIDENCE)
    else:
        return rel
    return rel.model_copy(update={"confidence": confidence})
