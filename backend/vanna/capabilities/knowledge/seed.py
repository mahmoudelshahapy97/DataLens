"""Generate starter examples from schema metadata.

A brand-new deployment has an empty example store, so the first users get no
few-shot examples at all -- which is exactly when the agent is least able to
guess local conventions and most likely to produce something wrong. That bad
first impression is also self-reinforcing: nobody rates a wrong answer
positively, so nothing gets captured, so the store stays empty.

Seeding breaks the cycle. Every pair generated here is derived from catalog
metadata, so it is correct by construction -- no LLM involved, nothing to
review.

The column-selection rules are the part that matters, and they are less obvious
than they look:

* **Identifier columns are never summed.** ``SUM(customer_id)`` is numerically
  valid and semantically meaningless. Primary keys, foreign keys, and
  ``*_id``-shaped names are all excluded from measure selection.
* **Enum columns become filter examples with real values**, which teaches the
  exact literal the database stores -- the single most common thing a model
  gets wrong.
* **Only one measure and one dimension per table.** The goal is to demonstrate
  the shape of a correct query, not to enumerate the schema.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Optional, Sequence, Set

from .models import Example, ExampleStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.capabilities.schema_catalog import (
        ColumnMetadata,
        RelationshipMetadata,
        TableMetadata,
    )
    from vanna.core.tool import ToolContext

    from .base import ExampleStore

#: Tag marking an example as machine-generated, so a reviewer can tell it apart
#: from human-authored knowledge and bulk-remove it if the schema is replaced.
SEED_TAG = "source:seed"

_NUMERIC_TYPES = {
    "int", "integer", "bigint", "smallint", "tinyint", "mediumint",
    "float", "double", "decimal", "numeric", "real", "number", "money",
}

#: Names that are identifiers regardless of what the catalog says about keys.
#: Many warehouses declare no constraints at all, so the name is often the only
#: signal that a column is a join key rather than a measure.
_ID_LIKE = re.compile(r"(^|_)(id|key|code|uuid|guid|num|no)$", re.IGNORECASE)


def _is_numeric(column: "ColumnMetadata") -> bool:
    base = (column.data_type or "").split("(")[0].strip().lower()
    return any(t in base for t in _NUMERIC_TYPES)


def _is_identifier(column: "ColumnMetadata", relationship_keys: Set[str]) -> bool:
    """True when a column identifies a row rather than measuring something."""
    if column.is_primary_key or column.foreign_key is not None:
        return True
    if column.name.lower() in relationship_keys:
        return True
    return bool(_ID_LIKE.search(column.name))


def _relationship_keys(
    table: "TableMetadata", relationships: Sequence["RelationshipMetadata"]
) -> Set[str]:
    """Columns of *table* participating in any declared join."""
    name = table.table_name.lower()
    qualified = table.qualified_name.lower()
    keys: Set[str] = set()
    for rel in relationships:
        if rel.from_table.lower() in (name, qualified):
            keys.add(rel.from_column.lower())
        if rel.to_table.lower() in (name, qualified):
            keys.add(rel.to_column.lower())
    return keys


def _pick_measure(
    table: "TableMetadata", relationship_keys: Set[str]
) -> Optional["ColumnMetadata"]:
    """First numeric column that is genuinely a measure."""
    for column in table.columns:
        if _is_numeric(column) and not _is_identifier(column, relationship_keys):
            return column
    return None


def _pick_dimension(
    table: "TableMetadata", relationship_keys: Set[str]
) -> Optional["ColumnMetadata"]:
    """Best column to group by, or None if the table has no good one.

    An enum-like column is strongly preferred: its distinct values are known to
    be few, so the example produces a short, readable result.

    The fallback deliberately excludes free-text columns (``title``, ``name``,
    ``description``, …). Grouping by one yields roughly one row per record --
    "How many albums are there by title?" is not a question anybody asks, and
    as a few-shot example it teaches the model a pattern that produces useless
    output. Returning None is better: the table then contributes only the
    examples that are actually meaningful.
    """
    from ..schema_catalog.scanner import is_free_text_column

    for column in table.columns:
        if column.categories and not _is_identifier(column, relationship_keys):
            return column
    for column in table.columns:
        if (
            not _is_numeric(column)
            and not _is_identifier(column, relationship_keys)
            and not is_free_text_column(column.name)
        ):
            return column
    return None


def generate_seed_examples(
    tables: Sequence["TableMetadata"],
    relationships: Sequence["RelationshipMetadata"] = (),
    *,
    max_per_table: int = 4,
    include_joins: bool = True,
) -> List[Example]:
    """Build starter question/SQL pairs from catalog metadata.

    Args:
        tables: Catalog tables, ideally after a scan so ``categories`` and key
            metadata are populated.
        relationships: Known join paths, used for join examples and to avoid
            treating a foreign key as a measure.
        max_per_table: Cap per table, so a wide schema does not swamp
            retrieval with near-identical examples.
        include_joins: Emit one example per relationship.

    Returns:
        Examples tagged :data:`SEED_TAG`, status ``CANDIDATE``. Candidate, not
        verified: these are structurally correct but nobody has confirmed they
        reflect how the business actually asks its questions.
    """
    examples: List[Example] = []

    for table in tables:
        name = table.qualified_name
        keys = _relationship_keys(table, relationships)
        produced: List[Example] = []

        produced.append(
            _example(
                f"Show me some rows from {table.table_name}",
                f"SELECT * FROM {name} LIMIT 100",
                table,
            )
        )

        measure = _pick_measure(table, keys)
        dimension = _pick_dimension(table, keys)

        if measure is not None:
            produced.append(
                _example(
                    f"What is the total {measure.name} in {table.table_name}?",
                    f"SELECT SUM({measure.name}) AS total_{measure.name} FROM {name}",
                    table,
                )
            )

        if measure is not None and dimension is not None:
            produced.append(
                _example(
                    f"What is {measure.name} by {dimension.name} "
                    f"in {table.table_name}?",
                    f"SELECT {dimension.name}, SUM({measure.name}) AS total_{measure.name}\n"
                    f"FROM {name}\n"
                    f"GROUP BY {dimension.name}\n"
                    f"ORDER BY total_{measure.name} DESC",
                    table,
                )
            )
        elif dimension is not None:
            produced.append(
                _example(
                    f"How many {table.table_name} are there by {dimension.name}?",
                    f"SELECT {dimension.name}, COUNT(*) AS n\n"
                    f"FROM {name}\n"
                    f"GROUP BY {dimension.name}\n"
                    f"ORDER BY n DESC",
                    table,
                )
            )

        # Filter examples carrying the real stored literal. This is the
        # highest-value template: it shows the exact casing and spelling, which
        # is what stops a model writing 'active' against a column holding
        # 'ACTIVE' and reporting zero rows as though that were the answer.
        for column in table.columns:
            if not column.categories:
                continue
            value = str(column.categories[0]).replace("'", "''")
            produced.append(
                _example(
                    f"Show {table.table_name} where {column.name} "
                    f"is {column.categories[0]}",
                    f"SELECT * FROM {name} WHERE {column.name} = '{value}' LIMIT 100",
                    table,
                )
            )
            break  # one is enough to establish the pattern

        examples.extend(produced[:max_per_table])

    if include_joins:
        by_name = {t.qualified_name.lower(): t for t in tables}
        by_name.update({t.table_name.lower(): t for t in tables})
        for rel in relationships:
            left = by_name.get(rel.from_table.lower())
            right = by_name.get(rel.to_table.lower())
            if left is None or right is None:
                continue
            examples.append(
                Example(
                    question=(
                        f"Show {left.table_name} together with their "
                        f"related {right.table_name}"
                    ),
                    sql=(
                        f"SELECT a.*, b.*\n"
                        f"FROM {left.qualified_name} a\n"
                        f"JOIN {right.qualified_name} b\n"
                        f"  ON a.{rel.from_column} = b.{rel.to_column}\n"
                        f"LIMIT 100"
                    ),
                    status=ExampleStatus.CANDIDATE,
                    tables=[left.qualified_name, right.qualified_name],
                    tags=[SEED_TAG],
                    tenant_id=left.tenant_id,
                    data_source_id=left.data_source_id,
                )
            )

    return examples


def _example(question: str, sql: str, table: "TableMetadata") -> Example:
    return Example(
        question=question,
        sql=sql,
        status=ExampleStatus.CANDIDATE,
        tables=[table.qualified_name],
        tags=[SEED_TAG],
        tenant_id=table.tenant_id,
        data_source_id=table.data_source_id,
    )


async def seed_example_store(
    context: "ToolContext",
    store: "ExampleStore",
    tables: Sequence["TableMetadata"],
    relationships: Sequence["RelationshipMetadata"] = (),
    *,
    only_if_empty: bool = True,
    dialect: Optional[str] = None,
) -> int:
    """Write starter examples into *store*. Returns how many were added.

    ``only_if_empty`` defaults to True so re-running a scan does not keep
    re-adding seeds alongside the human-curated examples that have accumulated
    since -- and does not resurrect seeds a reviewer deliberately deleted.
    """
    if only_if_empty:
        existing = await store.list_all(context)
        if existing:
            return 0

    added = 0
    for example in generate_seed_examples(tables, relationships):
        try:
            await store.add(
                context,
                example.question,
                example.sql,
                status=ExampleStatus.CANDIDATE,
                data_source_id=example.data_source_id,
                tags=example.tags,
                dialect=dialect,
            )
            added += 1
        except ValueError:
            # A generated example that will not parse means the identifier
            # needs quoting for this dialect. Skip it rather than poisoning
            # the store -- the other seeds are still useful.
            continue
    return added
