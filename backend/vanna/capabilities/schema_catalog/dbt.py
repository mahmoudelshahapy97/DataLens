"""Import dbt project metadata into the schema catalog.

Most analytics organisations already maintain, in dbt, exactly the metadata a
text-to-SQL agent needs: model descriptions, column descriptions,
relationships expressed as `relationships` tests, and enumerations expressed as
`accepted_values` tests. That content is reviewed, versioned, and kept current
by people who understand the data -- it is strictly better than anything a
scanner can infer or an LLM can guess.

Importing it is the single highest-yield metadata source available, and it
costs one file read.

Two entry points, and the difference matters:

``import_manifest``
    Reads ``target/manifest.json``, produced by ``dbt compile``/``dbt run``.
    Fully resolved: real database and schema names, compiled refs, everything
    dbt itself knows. **Prefer this.**

``import_project``
    Reads the ``schema.yml`` files directly. No dbt invocation needed, so it
    works against a plain checkout, but names are logical rather than physical
    and some resolution is unavailable.

Both merge into an existing catalog rather than replacing it: dbt supplies
*meaning* (descriptions, accepted values, relationships) while a scan supplies
*structure* (real types, nullability, row counts). Neither alone is complete,
and each fills the other's gaps.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from .models import (
    CatalogStatus,
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    ScanReport,
    TableMetadata,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

    from .base import SchemaCatalog

logger = logging.getLogger(__name__)


class DbtImporter:
    """Reads dbt metadata into :class:`TableMetadata` and relationships.

    Args:
        include_sources: Also import dbt ``source`` definitions, not just
            models. Sources are the raw tables upstream of the warehouse and
            are often what users ask about by name.
        include_seeds: Import ``seed`` CSVs -- typically small lookup tables,
            which are disproportionately useful to an agent because they define
            the codes appearing in fact tables.
        merge: Preserve fields already in the catalog that dbt does not
            describe (data types, row counts from a scan). Turning this off
            makes an import authoritative and discards scanned detail.
    """

    def __init__(
        self,
        *,
        include_sources: bool = True,
        include_seeds: bool = True,
        merge: bool = True,
    ) -> None:
        self.include_sources = include_sources
        self.include_seeds = include_seeds
        self.merge = merge

    # ------------------------------------------------------------------
    # manifest.json
    # ------------------------------------------------------------------

    async def import_manifest(
        self,
        context: "ToolContext",
        catalog: "SchemaCatalog",
        manifest_path: str,
        *,
        data_source_id: str = "default",
    ) -> ScanReport:
        """Import from a compiled ``target/manifest.json``."""
        report = ScanReport()
        path = Path(manifest_path)
        if not path.exists():
            report.errors.append(f"dbt manifest not found: {manifest_path}")
            return report

        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            report.errors.append(f"Could not parse dbt manifest: {e}")
            return report

        tenant = getattr(context, "tenant_id", "default")
        tables: List[TableMetadata] = []

        wanted_types = {"model"}
        if self.include_seeds:
            wanted_types.add("seed")

        for node in (manifest.get("nodes") or {}).values():
            if not isinstance(node, dict):
                continue
            if node.get("resource_type") not in wanted_types:
                continue
            table = self._node_to_table(node, tenant, data_source_id)
            if table:
                tables.append(table)

        if self.include_sources:
            for node in (manifest.get("sources") or {}).values():
                if isinstance(node, dict):
                    table = self._node_to_table(node, tenant, data_source_id)
                    if table:
                        tables.append(table)

        relationships = self._relationships_from_tests(
            manifest, tenant, data_source_id
        )

        # `relationships` tests are the authoritative join graph in a dbt
        # project -- richer than inferred FKs, because many warehouses do not
        # enforce (or even store) foreign keys at all. Fold them back onto the
        # columns so a table's own description carries its join paths.
        self._apply_relationships_to_columns(tables, relationships)

        if self.merge:
            tables = await self._merge_with_existing(
                context, catalog, tables, data_source_id
            )

        if tables:
            await catalog.upsert_tables(context, tables)
        if relationships:
            await catalog.upsert_relationships(context, relationships)

        report.tables_scanned = len(tables)
        report.columns_profiled = sum(len(t.columns) for t in tables)
        report.categories_found = sum(
            1 for t in tables for c in t.columns if c.categories
        )
        report.relationships_found = len(relationships)
        return report

    def _node_to_table(
        self, node: Dict, tenant: str, data_source_id: str
    ) -> Optional[TableMetadata]:
        name = node.get("alias") or node.get("name")
        if not name:
            return None

        columns = []
        for col_name, col in (node.get("columns") or {}).items():
            if not isinstance(col, dict):
                continue
            columns.append(
                ColumnMetadata(
                    name=col_name,
                    data_type=(col.get("data_type") or "unknown"),
                    description=col.get("description") or None,
                )
            )

        return TableMetadata(
            table_name=str(name),
            schema_name=node.get("schema"),
            description=node.get("description") or None,
            columns=columns,
            status=CatalogStatus.SCANNED,
            last_synced_at=datetime.now(timezone.utc),
            tenant_id=tenant,
            data_source_id=data_source_id,
            metadata={
                "source": "dbt",
                "resource_type": node.get("resource_type"),
                "unique_id": node.get("unique_id"),
                "tags": node.get("tags") or [],
                "materialization": (node.get("config") or {}).get("materialized"),
            },
        )

    def _relationships_from_tests(
        self, manifest: Dict, tenant: str, data_source_id: str
    ) -> List[RelationshipMetadata]:
        """Derive join paths from dbt ``relationships`` tests."""
        relationships: List[RelationshipMetadata] = []
        nodes = manifest.get("nodes") or {}

        for node in nodes.values():
            if not isinstance(node, dict):
                continue
            if node.get("resource_type") != "test":
                continue
            meta = node.get("test_metadata") or {}
            if meta.get("name") != "relationships":
                continue

            kwargs = meta.get("kwargs") or {}
            from_column = node.get("column_name") or kwargs.get("column_name")
            to_field = kwargs.get("field")
            to_ref = kwargs.get("to", "")

            # `to` is a rendered ref like "ref('customers')".
            to_table = self._parse_ref(str(to_ref))
            from_table = self._model_name(node, nodes)

            if not (from_table and to_table and from_column and to_field):
                continue

            relationships.append(
                RelationshipMetadata(
                    name=f"{from_table}.{from_column}->{to_table}",
                    from_table=from_table,
                    from_column=str(from_column),
                    to_table=to_table,
                    to_column=str(to_field),
                    join_type="many_to_one",
                    description="Declared by a dbt relationships test",
                    tenant_id=tenant,
                    data_source_id=data_source_id,
                )
            )
        return relationships

    @staticmethod
    def _parse_ref(value: str) -> Optional[str]:
        """Extract the model name from ``ref('name')`` or a bare name."""
        import re

        match = re.search(r"ref\(\s*['\"]([^'\"]+)['\"]", value)
        if match:
            return match.group(1)
        cleaned = value.strip().strip("\"'")
        return cleaned or None

    @staticmethod
    def _model_name(test_node: Dict, nodes: Dict) -> Optional[str]:
        """Resolve which model a test attaches to, via depends_on."""
        depends = (test_node.get("depends_on") or {}).get("nodes") or []
        for unique_id in depends:
            target = nodes.get(unique_id)
            if isinstance(target, dict) and target.get("resource_type") in (
                "model",
                "seed",
            ):
                return target.get("alias") or target.get("name")
        # Fall back to the attached_node reference dbt emits on newer versions.
        attached = test_node.get("attached_node")
        if attached:
            target = nodes.get(attached)
            if isinstance(target, dict):
                return target.get("alias") or target.get("name")
        return None

    @staticmethod
    def _apply_relationships_to_columns(
        tables: List[TableMetadata], relationships: Iterable[RelationshipMetadata]
    ) -> None:
        by_name = {t.table_name.lower(): t for t in tables}
        for rel in relationships:
            table = by_name.get(rel.from_table.lower())
            if not table:
                continue
            column = table.get_column(rel.from_column)
            if column and not column.foreign_key:
                column.foreign_key = ForeignKey(
                    column=column.name,
                    references_table=rel.to_table,
                    references_column=rel.to_column,
                )

    async def _merge_with_existing(
        self,
        context: "ToolContext",
        catalog: "SchemaCatalog",
        incoming: List[TableMetadata],
        data_source_id: str,
    ) -> List[TableMetadata]:
        """Overlay dbt's descriptions onto scanned structural detail.

        Precedence is chosen per field rather than per record, because the two
        sources are authoritative about different things: dbt owns meaning
        (descriptions), the scan owns structure (real types, nullability, row
        counts, observed values). Taking whole records from either side would
        throw away half the picture.
        """
        try:
            existing = await catalog.get_tables(
                context, data_source_id=data_source_id
            )
        except Exception:
            return incoming

        by_name = {t.table_name.lower(): t for t in existing}
        merged = []
        for table in incoming:
            current = by_name.get(table.table_name.lower())
            if current is None:
                merged.append(table)
                continue

            table.row_count_estimate = current.row_count_estimate
            if not table.schema_name:
                table.schema_name = current.schema_name
            if not table.description:
                table.description = current.description

            scanned_columns = {c.name.lower(): c for c in current.columns}
            for column in table.columns:
                scanned = scanned_columns.pop(column.name.lower(), None)
                if scanned is None:
                    continue
                # dbt rarely records a real physical type; the scan always does.
                if column.data_type in ("unknown", "", None):
                    column.data_type = scanned.data_type
                column.nullable = scanned.nullable
                column.is_primary_key = scanned.is_primary_key
                column.categories = column.categories or scanned.categories
                column.low_cardinality = (
                    column.low_cardinality or scanned.low_cardinality
                )
                column.sample_values = column.sample_values or scanned.sample_values
                column.foreign_key = column.foreign_key or scanned.foreign_key

            # Columns the scan found but dbt never documented still exist in
            # the database and the agent still needs to know about them.
            table.columns.extend(scanned_columns.values())
            merged.append(table)
        return merged

    # ------------------------------------------------------------------
    # schema.yml (no dbt invocation required)
    # ------------------------------------------------------------------

    async def import_project(
        self,
        context: "ToolContext",
        catalog: "SchemaCatalog",
        project_dir: str,
        *,
        data_source_id: str = "default",
    ) -> ScanReport:
        """Import from ``schema.yml`` files in a dbt project directory."""
        report = ScanReport()
        root = Path(project_dir)
        if not root.is_dir():
            report.errors.append(f"dbt project directory not found: {project_dir}")
            return report

        try:
            import yaml
        except ImportError:  # pragma: no cover - PyYAML is a core dependency
            report.errors.append("PyYAML is required to read dbt schema files")
            return report

        tenant = getattr(context, "tenant_id", "default")
        tables: List[TableMetadata] = []
        relationships: List[RelationshipMetadata] = []

        yaml_files = [
            p
            for p in root.rglob("*.yml")
            if "target" not in p.parts and "dbt_packages" not in p.parts
        ] + [
            p
            for p in root.rglob("*.yaml")
            if "target" not in p.parts and "dbt_packages" not in p.parts
        ]

        for path in yaml_files:
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                report.errors.append(f"{path.name}: {e}")
                continue
            if not isinstance(doc, dict):
                continue

            entries = list(doc.get("models") or [])
            if self.include_seeds:
                entries += list(doc.get("seeds") or [])
            for entry in entries:
                if isinstance(entry, dict):
                    parsed = self._yaml_entry_to_table(
                        entry, tenant, data_source_id, None
                    )
                    if parsed:
                        table, rels = parsed
                        tables.append(table)
                        relationships.extend(rels)

            if self.include_sources:
                for source in doc.get("sources") or []:
                    if not isinstance(source, dict):
                        continue
                    schema_name = source.get("schema") or source.get("name")
                    for entry in source.get("tables") or []:
                        if isinstance(entry, dict):
                            parsed = self._yaml_entry_to_table(
                                entry, tenant, data_source_id, schema_name
                            )
                            if parsed:
                                table, rels = parsed
                                tables.append(table)
                                relationships.extend(rels)

        self._apply_relationships_to_columns(tables, relationships)

        if self.merge:
            tables = await self._merge_with_existing(
                context, catalog, tables, data_source_id
            )

        if tables:
            await catalog.upsert_tables(context, tables)
        if relationships:
            await catalog.upsert_relationships(context, relationships)

        report.tables_scanned = len(tables)
        report.columns_profiled = sum(len(t.columns) for t in tables)
        report.categories_found = sum(
            1 for t in tables for c in t.columns if c.categories
        )
        report.relationships_found = len(relationships)
        return report

    def _yaml_entry_to_table(
        self,
        entry: Dict,
        tenant: str,
        data_source_id: str,
        schema_name: Optional[str],
    ):
        name = entry.get("identifier") or entry.get("name")
        if not name:
            return None

        columns: List[ColumnMetadata] = []
        relationships: List[RelationshipMetadata] = []

        for col in entry.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = col.get("name")
            if not col_name:
                continue

            column = ColumnMetadata(
                name=str(col_name),
                data_type=str(col.get("data_type") or "unknown"),
                description=col.get("description") or None,
            )

            # dbt tests carry real semantics worth importing:
            #   accepted_values -> the column's enumeration
            #   unique/not_null -> key and nullability facts
            #   relationships   -> a join path
            #
            # Warehouses frequently declare no primary keys at all, so a
            # dbt-tested `unique` + `not_null` pair is often the only reliable
            # PK signal available -- and it is a stronger one than the
            # information_schema, because it is asserted and continuously
            # verified by the test suite.
            is_unique = False
            is_not_null = False
            for test in col.get("tests") or col.get("data_tests") or []:
                if isinstance(test, str):
                    if test == "not_null":
                        is_not_null = True
                        column.nullable = False
                    elif test == "unique":
                        is_unique = True
                    continue
                if not isinstance(test, dict):
                    continue

                if "accepted_values" in test:
                    values = (test["accepted_values"] or {}).get("values") or []
                    if values:
                        column.categories = [str(v) for v in values]
                        column.low_cardinality = True

                if "relationships" in test:
                    spec = test["relationships"] or {}
                    to_table = self._parse_ref(str(spec.get("to", "")))
                    to_field = spec.get("field")
                    if to_table and to_field:
                        column.foreign_key = ForeignKey(
                            column=column.name,
                            references_table=to_table,
                            references_column=str(to_field),
                        )
                        relationships.append(
                            RelationshipMetadata(
                                name=f"{name}.{column.name}->{to_table}",
                                from_table=str(name),
                                from_column=column.name,
                                to_table=to_table,
                                to_column=str(to_field),
                                join_type="many_to_one",
                                description="Declared by a dbt relationships test",
                                tenant_id=tenant,
                                data_source_id=data_source_id,
                            )
                        )

            if is_unique and is_not_null:
                column.is_primary_key = True
            columns.append(column)

        table = TableMetadata(
            table_name=str(name),
            schema_name=schema_name,
            description=entry.get("description") or None,
            columns=columns,
            status=CatalogStatus.SCANNED,
            last_synced_at=datetime.now(timezone.utc),
            tenant_id=tenant,
            data_source_id=data_source_id,
            metadata={"source": "dbt", "tags": entry.get("tags") or []},
        )
        return table, relationships
