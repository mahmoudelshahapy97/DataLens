"""Generating a starter manifest from a scanned catalog.

The adoption path. Without this, a semantic layer is a blank-page problem: an
empty ``models/`` directory and a specification to read, which is where most
semantic-layer projects quietly stop.

What it produces is deliberately a *draft*. Every table becomes a model, every
column a column, every foreign key a relationship -- a manifest that is exactly
as smart as the database schema, and no smarter. The value is not in the output
being right; it is in the next step being "rename this and add a description"
rather than "learn a file format".
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence

from ..capabilities.schema_catalog.models import (
    RelationshipMetadata,
    TableMetadata,
)
from .models import (
    JoinType,
    Manifest,
    Relationship,
    SemanticColumn,
    SemanticModel,
)

#: Column-name suffixes that mark a join key. Hidden in the generated manifest:
#: they are needed to compile a join and are noise in a prompt, and the
#: relationship column added alongside is the thing the model should use.
_KEY_SUFFIXES = ("_id", "_key", "_fk")

_JOIN_TYPE_BY_NAME = {
    "one_to_one": JoinType.ONE_TO_ONE,
    "one_to_many": JoinType.ONE_TO_MANY,
    "many_to_one": JoinType.MANY_TO_ONE,
    "many_to_many": JoinType.MANY_TO_MANY,
}


def _singular(name: str) -> str:
    """A rough singular, for naming a relationship column.

    Rough is fine: it produces ``orders.customer`` from a ``customers`` table,
    and where it guesses badly the author renames one line. Anything more
    ambitious would need an inflection library to be wrong less often.
    """
    lowered = name.lower()
    for suffix, replacement in (("ies", "y"), ("ses", "s"), ("s", "")):
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 1:
            return lowered[: -len(suffix)] + replacement
    return lowered


def _is_key_column(name: str) -> bool:
    lowered = name.lower()
    return lowered == "id" or lowered.endswith(_KEY_SUFFIXES)


def model_from_table(
    table: TableMetadata, *, hide_keys: bool = True
) -> SemanticModel:
    """One table, as a draft model."""
    columns: List[SemanticColumn] = []
    for column in table.columns:
        columns.append(
            SemanticColumn(
                name=column.name,
                type=(column.data_type or "VARCHAR").upper(),
                description=column.description,
                not_null=not column.nullable,
                is_primary_key=column.is_primary_key,
                # Surrogate keys clutter a prompt without helping the model
                # write a better query -- the relationship column does that.
                is_hidden=hide_keys
                and _is_key_column(column.name)
                and not column.is_primary_key,
                # Carried, not recomputed. `scanner.py` already profiled these
                # with sensitive-column and free-text exclusion applied.
                categories=column.categories,
                sample_values=column.sample_values,
            )
        )

    primary = next((c.name for c in table.columns if c.is_primary_key), None)

    return SemanticModel(
        name=table.table_name,
        description=table.description,
        table_reference=table.qualified_name,
        columns=columns,
        primary_key=primary,
    )


def manifest_from_catalog(
    tables: Sequence[TableMetadata],
    relationships: Sequence[RelationshipMetadata] = (),
    *,
    hide_keys: bool = True,
    add_relationship_columns: bool = True,
) -> Manifest:
    """Build a draft manifest from what a scan found."""
    models = [model_from_table(t, hide_keys=hide_keys) for t in tables]
    by_name = {m.name.lower(): m for m in models}

    semantic_relationships: List[Relationship] = []
    used_names: set = set()

    for edge in relationships:
        left, right = edge.from_table, edge.to_table
        if left.lower() not in by_name or right.lower() not in by_name:
            # An edge to a table outside the scanned set cannot be compiled.
            continue

        name = edge.name or f"{left}_{right}"
        # Two foreign keys between the same pair of tables would otherwise
        # collide, and the second would silently overwrite the first.
        candidate, suffix = name, 2
        while candidate.lower() in used_names:
            candidate, suffix = f"{name}_{suffix}", suffix + 1
        used_names.add(candidate.lower())

        join_type = _JOIN_TYPE_BY_NAME.get(
            (edge.join_type or "").lower(), JoinType.MANY_TO_ONE
        )

        semantic_relationships.append(
            Relationship(
                name=candidate,
                models=[left, right],
                join_type=join_type,
                condition=(
                    f"{left}.{edge.from_column} = {right}.{edge.to_column}"
                ),
                description=edge.description,
            )
        )

        if add_relationship_columns:
            _attach_relationship_column(by_name[left.lower()], right, candidate)

    return Manifest(models=models, relationships=semantic_relationships)


def _attach_relationship_column(
    model: SemanticModel, target_model: str, relationship_name: str
) -> None:
    """Add ``orders.customer`` so the far model's columns are reachable."""
    handle = _singular(target_model)

    # Never shadow a real column: if `customer` already exists as data, the
    # traversal handle takes the relationship's name instead.
    if model.column(handle):
        handle = relationship_name

    if model.column(handle):
        return

    model.columns.append(
        SemanticColumn(
            name=handle,
            type=target_model,
            relationship=relationship_name,
            description=f"Related {target_model}.",
        )
    )
