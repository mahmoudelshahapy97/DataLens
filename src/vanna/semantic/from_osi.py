"""Build a :class:`Manifest` from an Open Semantic Interchange model.

`OSI <https://github.com/open-semantic-interchange/OSI>`_ is a vendor-neutral
specification for semantic models, backed by Snowflake, dbt Labs, Databricks,
Cube and AtScale. A team that already publishes one gets this project's semantic
layer without re-authoring their definitions in a second format.

**Import, don't adopt.** OSI is read *into* our ``Manifest`` and nothing
downstream knows it existed. Having one internal representation is what keeps
the compiler, the validator and the access-control layer honest -- a second
model to support would eventually mean a second set of bugs in each of them.

**Nothing is dropped in silence.** OSI can express dialect-specific expressions,
aggregation types and field kinds that have no equivalent here. Skipping one
quietly produces a model that looks complete and computes the wrong number, so
every unmapped construct is returned as a warning for the caller to print.

The shape being read::

    version: "0.2.0"
    semantic_model:
      - name: shop
        ai_context: { instructions: "..." }
        datasets:
          - name: orders
            source: shop.public.orders
            primary_key: [order_id]
            fields:
              - name: amount
                expression: amount
        relationships:
          - { name: r, from: orders, to: customers,
              from_columns: [customer_id], to_columns: [customer_id] }
        metrics:
          - name: total_revenue
            expression: { dialects: [{ dialect: ANSI_SQL, expression: SUM(...) }] }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    Cube,
    JoinType,
    Manifest,
    Measure,
    Relationship,
    SemanticColumn,
    SemanticModel,
)

#: Expression dialect preferred when an OSI field offers several. ANSI first
#: because it is the one most likely to compile everywhere; the rest are kept as
#: warnings rather than guessed between.
PREFERRED_DIALECTS = ("ANSI_SQL", "ANSI", "SQL")

#: OSI vendor extensions we understand. Anything else is another vendor's hint
#: and is left alone.
VENDOR = "WREN"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _expression(raw: Any, *, where: str, warnings: List[str]) -> Tuple[str, bool]:
    """Resolve an OSI expression to SQL.

    Returns ``(expression, is_calculated)``. A plain string is a column
    reference; a ``dialects`` block is a computed expression, and the one we
    pick is recorded when there was a choice to make.
    """
    if raw is None:
        return "", False
    if isinstance(raw, str):
        return raw, False

    if isinstance(raw, dict) and "dialects" in raw:
        options = raw.get("dialects") or []
        by_dialect = {
            str(o.get("dialect") or "").upper(): _text(o.get("expression"))
            for o in options
            if isinstance(o, dict)
        }
        for preferred in PREFERRED_DIALECTS:
            if by_dialect.get(preferred):
                if len(by_dialect) > 1:
                    others = ", ".join(sorted(k for k in by_dialect if k != preferred))
                    warnings.append(
                        f"{where}: used the {preferred} expression; ignored {others}. "
                        "Check it compiles on your dialect."
                    )
                return by_dialect[preferred], True
        if by_dialect:
            dialect, expression = next(iter(by_dialect.items()))
            warnings.append(
                f"{where}: no ANSI expression, used the {dialect} one. "
                "It may not compile on your dialect."
            )
            return expression, True

    warnings.append(f"{where}: could not read the expression; the field was skipped.")
    return "", False


def _wren_extension(node: Dict[str, Any]) -> Dict[str, Any]:
    """Read this vendor's ``custom_extensions`` block, if present.

    OSI carries vendor hints as a JSON string inside a list of extensions. Ours
    supplies column types, which OSI itself does not model -- without them every
    column would default to VARCHAR and numeric comparisons would be wrong.
    """
    for extension in node.get("custom_extensions") or []:
        if not isinstance(extension, dict):
            continue
        if str(extension.get("vendor_name") or "").upper() != VENDOR:
            continue
        data = extension.get("data")
        if isinstance(data, dict):
            return data
        try:
            parsed = json.loads(_text(data))
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            continue
    return {}


def _column(
    field: Dict[str, Any],
    column_types: Dict[str, Any],
    *,
    model_name: str,
    warnings: List[str],
) -> Optional[SemanticColumn]:
    name = _text(field.get("name")).strip()
    if not name:
        warnings.append(f"{model_name}: a field with no name was skipped.")
        return None

    where = f"{model_name}.{name}"
    expression, calculated = _expression(
        field.get("expression"), where=where, warnings=warnings
    )
    if not expression:
        return None

    own = _wren_extension(field)
    column_type = _text(own.get("type") or column_types.get(name) or "VARCHAR")

    return SemanticColumn(
        name=name,
        type=column_type,
        description=_text(field.get("description")) or None,
        # A plain column reference needs no expression; storing one would make
        # every column look computed to the compiler.
        expression=expression if calculated or expression != name else None,
        is_calculated=calculated,
    )


def _relationship(
    raw: Dict[str, Any], *, warnings: List[str]
) -> Optional[Relationship]:
    name = _text(raw.get("name")).strip()
    left = _text(raw.get("from")).strip()
    right = _text(raw.get("to")).strip()
    left_columns = [c for c in (raw.get("from_columns") or []) if c]
    right_columns = [c for c in (raw.get("to_columns") or []) if c]

    if not (left and right and left_columns and right_columns):
        warnings.append(
            f"relationship {name or '(unnamed)'}: missing from/to columns; skipped."
        )
        return None
    if len(left_columns) != len(right_columns):
        warnings.append(
            f"relationship {name}: {len(left_columns)} column(s) on one side and "
            f"{len(right_columns)} on the other; skipped."
        )
        return None

    condition = " AND ".join(
        f'"{left}"."{l}" = "{right}"."{r}"'
        for l, r in zip(left_columns, right_columns)
    )

    # OSI does not state cardinality. MANY_TO_ONE is the safe assumption for a
    # foreign key, and it is the one that makes the fan-out check conservative:
    # guessing ONE_TO_ONE would suppress a warning about double-counted rows.
    declared = _text(raw.get("cardinality")).upper().replace("-", "_")
    join = {
        "MANY_TO_ONE": JoinType.MANY_TO_ONE,
        "ONE_TO_MANY": JoinType.ONE_TO_MANY,
        "ONE_TO_ONE": JoinType.ONE_TO_ONE,
        "MANY_TO_MANY": JoinType.MANY_TO_MANY,
    }.get(declared, JoinType.MANY_TO_ONE)
    if declared and declared not in {
        "MANY_TO_ONE", "ONE_TO_MANY", "ONE_TO_ONE", "MANY_TO_MANY"
    }:
        warnings.append(
            f"relationship {name}: unknown cardinality {declared!r}; "
            "assumed many-to-one."
        )

    return Relationship(
        name=name or f"{left}_{right}",
        models=[left, right],
        join_type=join,
        condition=condition,
        description=_text(raw.get("description")) or None,
    )


def _cube(
    group_name: str,
    metrics: Sequence[Dict[str, Any]],
    base_object: str,
    *,
    warnings: List[str],
) -> Optional[Cube]:
    """Turn OSI metrics into one cube over the group's primary dataset.

    OSI metrics are expressions over the whole semantic model rather than over a
    named base table, so they are collected into a single cube. Splitting them
    per table would require inferring which one each belongs to from its
    expression, and inferring the grain of an aggregate is exactly the mistake
    the semantic layer exists to prevent.
    """
    measures: List[Measure] = []
    for metric in metrics:
        name = _text(metric.get("name")).strip()
        if not name:
            continue
        expression, _ = _expression(
            metric.get("expression"), where=f"metric {name}", warnings=warnings
        )
        if not expression:
            continue

        description = _text(metric.get("description")) or None
        synonyms = ((metric.get("ai_context") or {}).get("synonyms")) or []
        if synonyms:
            # Kept in the description, which is what retrieval indexes -- so
            # "gross sales" still finds total_sales.
            joined = ", ".join(str(s) for s in synonyms)
            description = f"{description or name}. Also called: {joined}."

        measures.append(
            Measure(name=name, expression=expression, description=description)
        )

    if not measures:
        return None
    return Cube(
        name=f"{group_name}_metrics",
        base_object=base_object,
        description=f"Metrics imported from the OSI model {group_name!r}.",
        measures=measures,
    )


def manifest_from_osi(
    document: Dict[str, Any],
    *,
    data_source: Optional[str] = None,
    catalog: str = "vanna",
    schema: str = "public",
) -> Tuple[Manifest, List[str]]:
    """Convert a parsed OSI document into a :class:`Manifest`.

    Args:
        document: The parsed YAML/JSON OSI model.
        data_source: Recorded on the manifest for provenance.
        catalog: Manifest catalog name.
        schema: Manifest schema name.

    Returns:
        ``(manifest, warnings)``. The warnings are not decoration -- each one is
        something present in the source that this manifest does not represent,
        and the caller is expected to show them.
    """
    warnings: List[str] = []

    groups = document.get("semantic_model")
    if isinstance(groups, dict):        # a single model, not a list
        groups = [groups]
    if not groups:
        return (
            Manifest(catalog=catalog, **{"schema": schema}, data_source=data_source),
            ["No `semantic_model` section found -- nothing to import."],
        )

    models: List[SemanticModel] = []
    relationships: List[Relationship] = []
    cubes: List[Cube] = []

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = _text(group.get("name")) or "model"
        instructions = _text((group.get("ai_context") or {}).get("instructions"))

        first_dataset = ""
        for dataset in group.get("datasets") or []:
            if not isinstance(dataset, dict):
                continue
            name = _text(dataset.get("name")).strip()
            if not name:
                warnings.append(f"{group_name}: a dataset with no name was skipped.")
                continue
            first_dataset = first_dataset or name

            extension = _wren_extension(dataset)
            column_types = extension.get("column_types") or {}

            columns: List[SemanticColumn] = []
            for field in dataset.get("fields") or []:
                if not isinstance(field, dict):
                    continue
                column = _column(
                    field, column_types, model_name=name, warnings=warnings
                )
                if column is not None:
                    columns.append(column)

            if not columns:
                warnings.append(f"{name}: no usable fields; the dataset was skipped.")
                continue

            primary_key = [k for k in (dataset.get("primary_key") or []) if k]
            if len(primary_key) > 1:
                warnings.append(
                    f"{name}: composite primary key {primary_key}; used "
                    f"{primary_key[0]!r}, which affects fan-out detection."
                )

            description = _text(dataset.get("description")) or None
            if instructions and description:
                description = f"{description} {instructions}"
            elif instructions:
                description = instructions

            models.append(
                SemanticModel(
                    name=name,
                    description=description,
                    # OSI's `source` is a fully-qualified physical table, which
                    # is exactly what table_reference means here.
                    table_reference=_text(dataset.get("source")) or None,
                    columns=columns,
                    primary_key=primary_key[0] if primary_key else None,
                )
            )

        for raw in group.get("relationships") or []:
            if not isinstance(raw, dict):
                continue
            relationship = _relationship(raw, warnings=warnings)
            if relationship is not None:
                relationships.append(relationship)

        metrics = [m for m in (group.get("metrics") or []) if isinstance(m, dict)]
        if metrics:
            cube = _cube(group_name, metrics, first_dataset, warnings=warnings)
            if cube is not None:
                cubes.append(cube)

        for unsupported in ("hierarchies", "filters", "aggregations"):
            if group.get(unsupported):
                warnings.append(
                    f"{group_name}: `{unsupported}` is not imported and was left out."
                )

    manifest = Manifest(
        catalog=catalog,
        **{"schema": schema},
        data_source=data_source,
        models=models,
        relationships=relationships,
        cubes=cubes,
    )
    return manifest, warnings


def load_osi(path: Path) -> Dict[str, Any]:
    """Parse an OSI file. YAML if PyYAML is available, JSON otherwise."""
    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith((".yml", ".yaml")):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError(
                "Reading a YAML OSI file needs PyYAML. Install it, or convert "
                "the file to JSON first."
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def manifest_from_osi_file(
    path: Path, *, data_source: Optional[str] = None
) -> Tuple[Manifest, List[str]]:
    """Read an OSI file and convert it. See :func:`manifest_from_osi`."""
    return manifest_from_osi(load_osi(Path(path)), data_source=data_source)
