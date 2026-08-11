"""Turning catalog and knowledge objects into indexable documents.

The interesting decision is that a table becomes **two** documents: one for its
structure, and one for the values its low-cardinality columns actually contain.

That second document is what lets "how many cancelled orders last week" find the
``orders`` table when nothing in the schema is called "cancelled" -- the scanner
already recorded that ``status`` holds ``CANCELLED``, and until now that
knowledge only reached the prompt, never the search. Keeping it as a separate
document rather than appending to the first stops a table with forty enum values
outranking a better match purely on length.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from ..schema_catalog.models import TableMetadata
from .base import IndexDocument

#: Enum values per column that reach the index. A column with hundreds is not
#: an enum in any useful sense, and the scanner has usually already declined to
#: record it -- this is a second bound for the ones that slip through.
MAX_VALUES_PER_COLUMN = 50


def documents_for_table(
    table: TableMetadata, *, tenant_id: str = "default"
) -> List[IndexDocument]:
    """Structure and values, as separate documents."""
    parts: List[str] = [table.table_name]
    if table.schema_name:
        parts.append(table.schema_name)
    if table.description:
        parts.append(table.description)

    for column in table.columns:
        parts.append(column.name)
        if column.description:
            parts.append(column.description)

    documents = [
        IndexDocument(
            id=f"table:{table.tenant_id}:{table.qualified_name}",
            text=" ".join(parts),
            kind="table",
            tenant_id=tenant_id,
            metadata={"table": table.table_name, "qualified": table.qualified_name},
        )
    ]

    values: List[str] = []
    for column in table.columns:
        if not column.categories:
            continue
        # The column name travels with its values so a query naming both
        # ("cancelled status") scores higher than one naming either alone.
        values.append(column.name)
        values.extend(str(v) for v in column.categories[:MAX_VALUES_PER_COLUMN])

    if values:
        documents.append(
            IndexDocument(
                id=f"values:{table.tenant_id}:{table.qualified_name}",
                text=" ".join(values),
                kind="column_values",
                tenant_id=tenant_id,
                # Below structural matches: a question that names a table
                # outright should beat one that merely mentions a value it
                # happens to contain.
                boost=0.8,
                metadata={"table": table.table_name, "qualified": table.qualified_name},
            )
        )

    return documents


def documents_for_tables(
    tables: Sequence[TableMetadata], *, tenant_id: str = "default"
) -> List[IndexDocument]:
    documents: List[IndexDocument] = []
    for table in tables:
        documents.extend(documents_for_table(table, tenant_id=tenant_id))
    return documents


def documents_for_examples(
    examples: Iterable, *, tenant_id: str = "default"
) -> List[IndexDocument]:
    """Question/SQL pairs from the example store.

    Both halves are indexed. Matching the SQL as well as the question is what
    lets a question about a table nobody phrased that way find an example that
    joins it.
    """
    documents: List[IndexDocument] = []
    for example in examples:
        status = getattr(example, "status", None)
        status_value = getattr(status, "value", status)

        documents.append(
            IndexDocument(
                id=f"example:{getattr(example, 'id', id(example))}",
                text=f"{getattr(example, 'question', '')} {getattr(example, 'sql', '')}",
                kind="example",
                tenant_id=tenant_id,
                # A human confirmed this one answers its question; a candidate
                # is only a signal that somebody clicked thumbs-up.
                boost=1.25 if status_value == "verified" else 1.0,
                metadata={
                    "question": getattr(example, "question", ""),
                    "status": status_value,
                    "tables": list(getattr(example, "tables", []) or []),
                },
            )
        )
    return documents
