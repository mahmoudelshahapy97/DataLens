"""Rendering a manifest as text for a prompt.

Mirrors ``schema_catalog/describe.py`` in shape and in threshold, because it
feeds the same place: the retrieval enhancer's schema section. What differs is
what it says -- a physical description lists columns and types, a semantic one
also says which of them are computed, what they mean, and how models join,
which is the part that stops the model inventing a relationship.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from .models import Cube, Manifest, SemanticModel

#: Same budget the physical catalog uses. Above it, the caller should search
#: rather than send everything -- see ``SchemaCatalog.get_context``.
SEMANTIC_FULL_TEXT_THRESHOLD = 30_000


def describe_model(model: SemanticModel, *, manifest: Optional[Manifest] = None) -> str:
    """One model, as prompt text."""
    lines: List[str] = [f"Model: {model.name}"]
    if model.description:
        lines.append(f"  {model.description}")

    for column in model.visible_columns:
        parts = [f"    {column.name} ({column.type}"]
        if column.is_primary_key:
            parts.append(", primary key")
        if column.is_calculated:
            # Showing the expression is what stops the model recomputing it
            # from the underlying columns and getting a different answer.
            parts.append(f", computed as {column.expression}")
        parts.append(")")
        line = "".join(parts)

        if column.description:
            line += f" -- {column.description}"
        if column.categories:
            shown = ", ".join(column.categories[:12])
            more = "" if len(column.categories) <= 12 else ", ..."
            line += f" [values: {shown}{more}]"
        elif column.sample_values:
            line += f" [e.g. {', '.join(column.sample_values[:3])}]"
        lines.append(line)

    for column in model.relationship_columns:
        lines.append(
            f"    {column.name} -> {column.type} "
            f"(use {model.name}.{column.name}.<column> to read its fields)"
        )

    if manifest is not None:
        edges = manifest.relationships_for(model.name)
        if edges:
            lines.append("  Joins:")
            for edge in edges:
                lines.append(
                    f"    {edge.condition}  [{edge.direction_from(model.name).value}]"
                )

    return "\n".join(lines)


def describe_cube(cube: Cube) -> str:
    lines = [f"Cube: {cube.name} (over {cube.base_object})"]
    if cube.description:
        lines.append(f"  {cube.description}")
    if cube.measures:
        lines.append("  Measures:")
        for measure in cube.measures:
            suffix = f" -- {measure.description}" if measure.description else ""
            lines.append(f"    {measure.name} = {measure.expression}{suffix}")
    if cube.dimensions:
        lines.append(
            "  Dimensions: " + ", ".join(d.name for d in cube.dimensions)
        )
    if cube.time_dimensions:
        lines.append(
            "  Time dimensions: " + ", ".join(d.name for d in cube.time_dimensions)
        )
    return "\n".join(lines)


def describe_manifest(
    manifest: Manifest, *, models: Optional[Iterable[str]] = None
) -> str:
    """The whole manifest, or a named subset, as prompt text."""
    wanted = {m.lower() for m in models} if models is not None else None
    selected = [
        m for m in manifest.models if wanted is None or m.name.lower() in wanted
    ]

    blocks = [describe_model(m, manifest=manifest) for m in selected]

    for cube in manifest.cubes:
        if wanted is None or cube.base_object.lower() in wanted:
            blocks.append(describe_cube(cube))

    for view in manifest.views:
        if wanted is None or view.name.lower() in wanted:
            description = f" -- {view.description}" if view.description else ""
            blocks.append(f"View: {view.name}{description}")

    return "\n\n".join(blocks)
