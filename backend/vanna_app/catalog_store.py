"""``SchemaCatalog`` over the control plane.

The catalog used to be one JSON file on disk, which is right for a laptop and
wrong for a deployment: no row-level tenant isolation, two replicas fighting over
the same file, and nothing an administrator can query. This is the same catalog
against the control-plane database.

Two properties are worth stating, because both are load-bearing.

**Structure and meaning are stored separately, and only structure is rewritten.**
``upsert_tables`` replaces a whole ``TableMetadata``, so if a description lived on
the same row a routine re-scan would silently destroy it -- the exact failure
``capabilities/schema_catalog/dbt.py`` already documents on the merge path ("dbt
owns meaning, the scan owns structure"). Descriptions live in
``table_annotations`` / ``column_annotations``, which no scan touches, and are
merged back on read. A re-scan cannot lose curation because it never writes to
where curation lives.

**Everything is keyed on the normalized name.** ``table_key`` and ``column_key``
come from the same ``normalize_table`` / ``normalize_identifier`` the grant tables
use, so a scanned column, a grant on it and an annotation about it all join on
the same columns. That is why a grant survives a re-scan here without any
bookkeeping: it was never pointing at a row id that got recreated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from vanna.capabilities.agent_memory import tenant_scope
from vanna.capabilities.schema_catalog import (
    CatalogStatus,
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    SchemaCatalog,
    TableMetadata,
)
from vanna.capabilities.schema_catalog.search import rank_tables, search_indexed
from vanna.core.grants import normalize_identifier, normalize_table

from .db import SCHEMA

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

logger = logging.getLogger("vanna.catalog")


class PostgresSchemaCatalog(SchemaCatalog):
    """Tenant-scoped schema catalog backed by the control plane.

    Args:
        db: an :class:`~vanna_app.db.AppDatabase`.
        index: optional search index, used the same way
            :class:`LocalSchemaCatalog` uses it -- it also matches on the column
            *values* the scanner profiled, so "cancelled orders" can find the
            orders table through its ``status`` enum.
    """

    def __init__(self, db: Any, *, index: Any = None) -> None:
        self.db = db
        self.index = index

    # ------------------------------------------------------------------
    # Scoping
    # ------------------------------------------------------------------

    @staticmethod
    def _tenant(context: "ToolContext") -> str:
        """The tenant key. Never caller-supplied -- see the ABC's contract."""
        return tenant_scope(context)

    @staticmethod
    def _source(data_source_id: Optional[str]) -> str:
        return data_source_id or "default"

    @staticmethod
    def _source_of(context: Any, data_source_id: Optional[str]) -> str:
        """The data source to file a record under.

        The record's own value wins; otherwise the one the caller was operating
        against, carried in ``ToolContext.metadata``. ``"default"`` is the last
        resort and is now genuinely a last resort -- it used to be the *only*
        answer for a scan, because the scanner does not stamp its tables, and
        that put every scanned row under a key no reader ever looks up.
        """
        if data_source_id:
            return data_source_id
        metadata = getattr(context, "metadata", None) or {}
        return str(metadata.get("data_source_id") or "") or "default"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_tables(
        self,
        context: "ToolContext",
        *,
        schema: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        tenant = self._tenant(context)
        rows = await self.db.fetch_all(
            f"""
            SELECT t.data_source_id, t.table_key, t.schema_name, t.table_name,
                   t.row_count_estimate, t.status, t.error_message, t.last_seen_at,
                   a.description AS annotation_description
              FROM {SCHEMA}.catalog_tables t
              LEFT JOIN {SCHEMA}.table_annotations a
                     ON a.tenant_id = t.tenant_id
                    AND a.data_source_id = t.data_source_id
                    AND a.table_key = t.table_key
             WHERE t.tenant_id = %s
               AND (%s::text IS NULL OR t.data_source_id = %s)
               AND (%s::text IS NULL OR lower(t.schema_name) = lower(%s))
               AND t.lifecycle_status = 'present'
             ORDER BY t.data_source_id, t.table_key
            """,
            (tenant, data_source_id, data_source_id, schema, schema),
        )
        if not rows:
            return []

        columns = await self._columns_for(tenant, data_source_id)
        return [self._table_from_row(row, tenant, columns) for row in rows]

    async def get_table(
        self,
        context: "ToolContext",
        name: str,
        *,
        data_source_id: Optional[str] = None,
    ) -> Optional[TableMetadata]:
        """One table by ``table`` or ``schema.table``.

        Looked up on the normalized key, which casefolds -- the ABC asks for
        case-insensitive matching of unquoted names and that is exactly what
        ``normalize_table`` already produces for the grant tables.
        """
        tenant = self._tenant(context)
        key = normalize_table(name)
        rows = await self.db.fetch_all(
            f"""
            SELECT t.data_source_id, t.table_key, t.schema_name, t.table_name,
                   t.row_count_estimate, t.status, t.error_message, t.last_seen_at,
                   a.description AS annotation_description
              FROM {SCHEMA}.catalog_tables t
              LEFT JOIN {SCHEMA}.table_annotations a
                     ON a.tenant_id = t.tenant_id
                    AND a.data_source_id = t.data_source_id
                    AND a.table_key = t.table_key
             WHERE t.tenant_id = %s
               AND (%s::text IS NULL OR t.data_source_id = %s)
               AND t.lifecycle_status = 'present'
               AND (t.table_key = %s OR split_part(t.table_key, '.', 2) = %s)
             ORDER BY (t.table_key = %s) DESC, t.data_source_id
             LIMIT 1
            """,
            (tenant, data_source_id, data_source_id, key, key, key),
        )
        if not rows:
            return None
        columns = await self._columns_for(tenant, data_source_id)
        return self._table_from_row(rows[0], tenant, columns)

    async def search_tables(
        self,
        context: "ToolContext",
        query: str,
        *,
        limit: int = 10,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        tables = await self.get_tables(context, data_source_id=data_source_id)
        if not tables:
            return []

        if self.index is not None:
            ranked = search_indexed(self.index, context, query, tables, limit)
            if ranked is not None:
                return ranked

        return rank_tables(query, tables, limit)

    async def get_relationships(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> List[RelationshipMetadata]:
        tenant = self._tenant(context)
        rows = await self.db.fetch_all(
            f"""
            SELECT data_source_id, name, join_type, description,
                   from_table_key, from_column_key, to_table_key, to_column_key
              FROM {SCHEMA}.catalog_relationships
             WHERE tenant_id = %s
               AND (%s::text IS NULL OR data_source_id = %s)
               AND lifecycle_status = 'present'
             ORDER BY from_table_key, to_table_key
            """,
            (tenant, data_source_id, data_source_id),
        )
        return [
            RelationshipMetadata(
                name=row["name"],
                from_table=row["from_table_key"],
                from_column=row["from_column_key"],
                to_table=row["to_table_key"],
                to_column=row["to_column_key"],
                join_type=row["join_type"],
                description=row["description"],
                tenant_id=tenant,
                data_source_id=row["data_source_id"],
            )
            for row in rows
        ]

    async def _columns_for(
        self, tenant: str, data_source_id: Optional[str]
    ) -> Dict[Tuple[str, str], List[ColumnMetadata]]:
        """Every column of a data source, grouped by (data_source_id, table_key).

        One query for the whole catalog rather than one per table: building a
        prompt reads all of them, and the per-table version was N+1 against the
        control plane on every question.
        """
        rows = await self.db.fetch_all(
            f"""
            SELECT c.data_source_id, c.table_key, c.column_name, c.data_type,
                   c.nullable, c.is_primary_key, c.is_generated, c.has_default,
                   c.low_cardinality, c.categories, c.sample_values, c.foreign_key,
                   a.description AS annotation_description,
                   a.value_labels
              FROM {SCHEMA}.catalog_columns c
              LEFT JOIN {SCHEMA}.column_annotations a
                     ON a.tenant_id = c.tenant_id
                    AND a.data_source_id = c.data_source_id
                    AND a.table_key = c.table_key
                    AND a.column_key = c.column_key
             WHERE c.tenant_id = %s
               AND (%s::text IS NULL OR c.data_source_id = %s)
               AND c.lifecycle_status = 'present'
             ORDER BY c.data_source_id, c.table_key, c.ordinal
            """,
            (tenant, data_source_id, data_source_id),
        )

        grouped: Dict[Tuple[str, str], List[ColumnMetadata]] = {}
        for row in rows:
            key = (row["data_source_id"], row["table_key"])
            grouped.setdefault(key, []).append(self._column_from_row(row))
        return grouped

    # ------------------------------------------------------------------
    # Row -> model
    # ------------------------------------------------------------------

    @staticmethod
    def _column_from_row(row: Dict[str, Any]) -> ColumnMetadata:
        foreign_key = row.get("foreign_key")
        if isinstance(foreign_key, str):
            foreign_key = json.loads(foreign_key)

        # Coded values are folded into the description because that is what
        # reaches the model: ColumnMetadata.describe() renders `-- {description}`
        # and has no field of its own for a code book. Without it the model
        # guesses the literal, and a wrong literal returns zero rows rather than
        # an error anybody can act on.
        description = row.get("annotation_description") or None
        labels = row.get("value_labels") or {}
        if isinstance(labels, str):
            labels = json.loads(labels)
        if labels:
            rendered = "; ".join(f"{code} = {label}" for code, label in labels.items())
            description = f"{description}. {rendered}" if description else rendered

        return ColumnMetadata(
            name=row["column_name"],
            data_type=row["data_type"],
            nullable=row["nullable"],
            is_primary_key=row["is_primary_key"],
            is_generated=row["is_generated"],
            has_default=row["has_default"],
            low_cardinality=row["low_cardinality"],
            categories=list(row.get("categories") or []) or None,
            sample_values=list(row.get("sample_values") or []) or None,
            foreign_key=ForeignKey(**foreign_key) if foreign_key else None,
            description=description,
        )

    @staticmethod
    def _table_from_row(
        row: Dict[str, Any],
        tenant: str,
        columns: Dict[Tuple[str, str], List[ColumnMetadata]],
    ) -> TableMetadata:
        return TableMetadata(
            table_name=row["table_name"],
            schema_name=row["schema_name"] or None,
            description=row.get("annotation_description") or None,
            columns=columns.get((row["data_source_id"], row["table_key"]), []),
            row_count_estimate=row["row_count_estimate"],
            tenant_id=tenant,
            data_source_id=row["data_source_id"],
            status=CatalogStatus(row["status"]),
            error_message=row["error_message"],
            last_synced_at=row["last_seen_at"],
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_tables(
        self, context: "ToolContext", tables: List[TableMetadata]
    ) -> None:
        """Record a scan.

        Treated as the complete picture for every data source named in *tables*,
        because that is what the only caller passes: ``SchemaScanner.scan``
        collects every table it found and upserts once. Anything previously known
        for those data sources and absent here is marked ``removed`` rather than
        deleted -- deleting would take its annotations and domain membership with
        it, and a table can disappear for reasons that are not permanent.

        An empty list is a no-op rather than "everything is gone", so a scan that
        failed outright cannot blank a working catalog.
        """
        if not tables:
            return

        tenant = self._tenant(context)
        seen: Dict[str, List[str]] = {}
        for table in tables:
            # Stamp the caller's tenant rather than trusting the record: a
            # scanner or an import could otherwise write into another tenant.
            table.tenant_id = tenant
            source = self._source_of(context, table.data_source_id)
            seen.setdefault(source, []).append(normalize_table(table.qualified_name))

        def run(cursor: Any) -> None:
            for table in tables:
                source = self._source_of(context, table.data_source_id)
                table_key = normalize_table(table.qualified_name)
                cursor.execute(
                    f"""
                    INSERT INTO {SCHEMA}.catalog_tables
                        (tenant_id, data_source_id, table_key, schema_name, table_name,
                         row_count_estimate, status, error_message,
                         lifecycle_status, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'present', now())
                    ON CONFLICT (tenant_id, data_source_id, table_key) DO UPDATE SET
                        schema_name        = EXCLUDED.schema_name,
                        table_name         = EXCLUDED.table_name,
                        row_count_estimate = EXCLUDED.row_count_estimate,
                        status             = EXCLUDED.status,
                        error_message      = EXCLUDED.error_message,
                        lifecycle_status   = 'present',
                        last_seen_at       = now()
                    """,
                    (
                        tenant,
                        source,
                        table_key,
                        table.schema_name or "",
                        table.table_name,
                        table.row_count_estimate,
                        str(getattr(table.status, "value", table.status)),
                        table.error_message,
                    ),
                )

                column_keys: List[str] = []
                for ordinal, column in enumerate(table.columns):
                    column_key = normalize_identifier(column.name)
                    column_keys.append(column_key)
                    cursor.execute(
                        f"""
                        INSERT INTO {SCHEMA}.catalog_columns
                            (tenant_id, data_source_id, table_key, column_key,
                             column_name, ordinal, data_type, nullable, is_primary_key,
                             is_generated, has_default, low_cardinality, categories,
                             sample_values, foreign_key, lifecycle_status, last_seen_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s::jsonb, %s::jsonb, %s::jsonb, 'present', now())
                        ON CONFLICT (tenant_id, data_source_id, table_key, column_key)
                        DO UPDATE SET
                            column_name      = EXCLUDED.column_name,
                            ordinal          = EXCLUDED.ordinal,
                            data_type        = EXCLUDED.data_type,
                            nullable         = EXCLUDED.nullable,
                            is_primary_key   = EXCLUDED.is_primary_key,
                            is_generated     = EXCLUDED.is_generated,
                            has_default      = EXCLUDED.has_default,
                            low_cardinality  = EXCLUDED.low_cardinality,
                            categories       = EXCLUDED.categories,
                            sample_values    = EXCLUDED.sample_values,
                            foreign_key      = EXCLUDED.foreign_key,
                            lifecycle_status = 'present',
                            last_seen_at     = now()
                        """,
                        (
                            tenant,
                            source,
                            table_key,
                            column_key,
                            column.name,
                            ordinal,
                            column.data_type,
                            column.nullable,
                            column.is_primary_key,
                            column.is_generated,
                            column.has_default,
                            column.low_cardinality,
                            json.dumps(list(column.categories or [])),
                            json.dumps(list(column.sample_values or [])),
                            json.dumps(column.foreign_key.model_dump())
                            if column.foreign_key
                            else None,
                        ),
                    )

                # A column the scan no longer reports is gone from the table, but
                # only when the scan actually looked: a FAILED table arrives with
                # no columns and must not retire the ones we already knew.
                if table.columns:
                    cursor.execute(
                        f"""
                        UPDATE {SCHEMA}.catalog_columns
                           SET lifecycle_status = 'removed'
                         WHERE tenant_id = %s AND data_source_id = %s
                           AND table_key = %s AND NOT (column_key = ANY(%s))
                           AND lifecycle_status = 'present'
                        """,
                        (tenant, source, table_key, column_keys),
                    )

            for source, keys in seen.items():
                cursor.execute(
                    f"""
                    UPDATE {SCHEMA}.catalog_tables
                       SET lifecycle_status = 'removed'
                     WHERE tenant_id = %s AND data_source_id = %s
                       AND NOT (table_key = ANY(%s))
                       AND lifecycle_status = 'present'
                    """,
                    (tenant, source, keys),
                )

        await self._transact(run)

    async def upsert_relationships(
        self, context: "ToolContext", relationships: List[RelationshipMetadata]
    ) -> None:
        if not relationships:
            return
        tenant = self._tenant(context)

        def run(cursor: Any) -> None:
            for rel in relationships:
                source = self._source(rel.data_source_id)
                cursor.execute(
                    f"""
                    INSERT INTO {SCHEMA}.catalog_relationships
                        (tenant_id, data_source_id, from_table_key, from_column_key,
                         to_table_key, to_column_key, name, join_type, description,
                         lifecycle_status, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'present', now())
                    ON CONFLICT (tenant_id, data_source_id, from_table_key,
                                 from_column_key, to_table_key, to_column_key)
                    DO UPDATE SET
                        name             = EXCLUDED.name,
                        join_type        = EXCLUDED.join_type,
                        description      = EXCLUDED.description,
                        lifecycle_status = 'present',
                        last_seen_at     = now()
                    """,
                    (
                        tenant,
                        source,
                        normalize_table(rel.from_table),
                        normalize_identifier(rel.from_column),
                        normalize_table(rel.to_table),
                        normalize_identifier(rel.to_column),
                        rel.name,
                        rel.join_type,
                        rel.description,
                    ),
                )

        await self._transact(run)

    async def clear(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> int:
        """Drop this tenant's structural catalog. Returns tables removed.

        A real delete, unlike the lifecycle marking a scan does: the caller is
        ``Platform._drop``, which runs when a workspace is repointed at a
        different database, and metadata describing the old one is not stale --
        it is wrong.

        Annotations and domains are deliberately left alone. They belong to the
        people who wrote them, they are keyed by name so they still apply if the
        workspace is pointed back, and a delete here would be unrecoverable.
        """
        tenant = self._tenant(context)
        removed = await self.db.execute(
            f"""
            DELETE FROM {SCHEMA}.catalog_tables
             WHERE tenant_id = %s AND (%s::text IS NULL OR data_source_id = %s)
            """,
            (tenant, data_source_id, data_source_id),
        )
        await self.db.execute(
            f"""
            DELETE FROM {SCHEMA}.catalog_relationships
             WHERE tenant_id = %s AND (%s::text IS NULL OR data_source_id = %s)
            """,
            (tenant, data_source_id, data_source_id),
        )
        return int(removed or 0)

    # ------------------------------------------------------------------
    # Annotations -- what a person wrote, never touched by a scan
    # ------------------------------------------------------------------
    #
    # These are the highest-leverage rows in the catalog and were unreachable:
    # both descriptions already reach the model's prompt, and `value_labels` is
    # what stops it inventing a literal -- a wrong literal returns zero rows
    # rather than an error anybody can act on. Until now the only way to set any
    # of it was raw SQL.
    #
    # No foreign key to `catalog_tables`, by design: an annotation survives a
    # rescan that drops and rebuilds the structural rows, and applies again if a
    # dropped table comes back. So a write here checks the catalog itself rather
    # than relying on the database to.

    async def table_exists(
        self, tenant_id: str, data_source_id: str, table_key: str
    ) -> bool:
        row = await self.db.fetch_one(
            f"""SELECT 1 FROM {SCHEMA}.catalog_tables
                 WHERE tenant_id = %s AND data_source_id = %s AND table_key = %s
                   AND lifecycle_status = 'present'""",
            (tenant_id, data_source_id, table_key),
        )
        return row is not None

    async def column_exists(
        self, tenant_id: str, data_source_id: str, table_key: str, column_key: str
    ) -> bool:
        row = await self.db.fetch_one(
            f"""SELECT 1 FROM {SCHEMA}.catalog_columns
                 WHERE tenant_id = %s AND data_source_id = %s
                   AND table_key = %s AND column_key = %s
                   AND lifecycle_status = 'present'""",
            (tenant_id, data_source_id, table_key, column_key),
        )
        return row is not None

    async def annotate_table(
        self,
        tenant_id: str,
        data_source_id: str,
        table_key: str,
        *,
        description: Optional[str] = None,
        display_name: Optional[str] = None,
        updated_by: str = "",
    ) -> Dict[str, Any]:
        """Write a table's description. Only the fields given are touched.

        ``coalesce(%s, column)`` rather than a plain SET so a screen that edits the
        description cannot blank a display name it never showed.
        """
        await self.db.execute(
            f"""
            INSERT INTO {SCHEMA}.table_annotations
                   (tenant_id, data_source_id, table_key, description, display_name,
                    updated_by)
            VALUES (%s, %s, %s, coalesce(%s, ''), coalesce(%s, ''), %s)
            ON CONFLICT (tenant_id, data_source_id, table_key) DO UPDATE
               SET description  = coalesce(%s, {SCHEMA}.table_annotations.description),
                   display_name = coalesce(%s, {SCHEMA}.table_annotations.display_name),
                   updated_by   = EXCLUDED.updated_by,
                   updated_at   = now()
            """,
            (tenant_id, data_source_id, table_key, description, display_name,
             updated_by, description, display_name),
        )
        return await self.get_table_annotation(tenant_id, data_source_id, table_key)

    async def get_table_annotation(
        self, tenant_id: str, data_source_id: str, table_key: str
    ) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            f"""SELECT table_key, display_name, description, updated_by, updated_at
                  FROM {SCHEMA}.table_annotations
                 WHERE tenant_id = %s AND data_source_id = %s AND table_key = %s""",
            (tenant_id, data_source_id, table_key),
        )
        return dict(row) if row else {
            "table_key": table_key, "display_name": "", "description": "",
        }

    async def list_column_annotations(
        self, tenant_id: str, data_source_id: str, table_key: str
    ) -> Dict[str, Any]:
        """Every column annotation for one table, keyed by column.

        One query rather than one per column: an editor opens on a table and needs
        all of them, and the per-column version was N+1 against the control plane
        for a screen that renders a single table.
        """
        rows = await self.db.fetch_all(
            f"""SELECT column_key, display_name, description, value_labels,
                       sensitivity, updated_by, updated_at
                  FROM {SCHEMA}.column_annotations
                 WHERE tenant_id = %s AND data_source_id = %s AND table_key = %s""",
            (tenant_id, data_source_id, table_key),
        )
        out: Dict[str, Any] = {}
        for row in rows:
            entry = dict(row)
            if isinstance(entry.get("value_labels"), str):
                entry["value_labels"] = json.loads(entry["value_labels"])
            out[entry["column_key"]] = entry
        return out

    async def annotate_column(
        self,
        tenant_id: str,
        data_source_id: str,
        table_key: str,
        column_key: str,
        *,
        description: Optional[str] = None,
        display_name: Optional[str] = None,
        value_labels: Optional[Dict[str, str]] = None,
        sensitivity: Optional[str] = None,
        updated_by: str = "",
    ) -> Dict[str, Any]:
        """Write a column's description, code book and sensitivity.

        ``value_labels`` is replaced wholesale when given -- a code book is edited
        as a set, and merging would make removing a code impossible.
        """
        labels = json.dumps(value_labels) if value_labels is not None else None
        await self.db.execute(
            f"""
            INSERT INTO {SCHEMA}.column_annotations
                   (tenant_id, data_source_id, table_key, column_key,
                    description, display_name, value_labels, sensitivity, updated_by)
            VALUES (%s, %s, %s, %s, coalesce(%s, ''), coalesce(%s, ''),
                    coalesce(%s::jsonb, '{{}}'::jsonb), %s, %s)
            ON CONFLICT (tenant_id, data_source_id, table_key, column_key) DO UPDATE
               SET description  = coalesce(%s, {SCHEMA}.column_annotations.description),
                   display_name = coalesce(%s, {SCHEMA}.column_annotations.display_name),
                   value_labels = coalesce(%s::jsonb, {SCHEMA}.column_annotations.value_labels),
                   sensitivity  = coalesce(%s, {SCHEMA}.column_annotations.sensitivity),
                   updated_by   = EXCLUDED.updated_by,
                   updated_at   = now()
            """,
            (tenant_id, data_source_id, table_key, column_key,
             description, display_name, labels, sensitivity, updated_by,
             description, display_name, labels, sensitivity),
        )
        return await self.get_column_annotation(
            tenant_id, data_source_id, table_key, column_key
        )

    async def get_column_annotation(
        self, tenant_id: str, data_source_id: str, table_key: str, column_key: str
    ) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            f"""SELECT table_key, column_key, display_name, description, value_labels,
                       sensitivity, updated_by, updated_at
                  FROM {SCHEMA}.column_annotations
                 WHERE tenant_id = %s AND data_source_id = %s
                   AND table_key = %s AND column_key = %s""",
            (tenant_id, data_source_id, table_key, column_key),
        )
        if not row:
            return {
                "table_key": table_key, "column_key": column_key,
                "display_name": "", "description": "", "value_labels": {},
                "sensitivity": None,
            }
        out = dict(row)
        if isinstance(out.get("value_labels"), str):
            out["value_labels"] = json.loads(out["value_labels"])
        return out

    # ------------------------------------------------------------------
    # Core columns -- which columns an admin curated as the ones that matter
    # ------------------------------------------------------------------
    #
    # Same posture as the annotations above: not foreign-keyed to
    # catalog_columns, so a selection survives a re-scan and re-applies if a
    # dropped column comes back, and a write checks the live catalog itself
    # rather than relying on a database constraint to.

    async def get_core_columns(
        self, tenant_id: str, data_source_id: str, table_key: str
    ) -> List[str]:
        rows = await self.db.fetch_all(
            f"""SELECT column_key FROM {SCHEMA}.core_columns
                 WHERE tenant_id = %s AND data_source_id = %s AND table_key = %s
                 ORDER BY column_key""",
            (tenant_id, data_source_id, table_key),
        )
        return [row["column_key"] for row in rows]

    async def get_core_columns_map(
        self, tenant_id: str, data_source_id: str, table_keys: Sequence[str]
    ) -> Dict[str, List[str]]:
        """Core columns for several tables at once, grouped by table_key.

        One query rather than one per table, same reasoning as
        ``_columns_for``: the tool may be asked about more than one table.
        """
        if not table_keys:
            return {}
        rows = await self.db.fetch_all(
            f"""SELECT table_key, column_key FROM {SCHEMA}.core_columns
                 WHERE tenant_id = %s AND data_source_id = %s
                   AND table_key = ANY(%s)
                 ORDER BY table_key, column_key""",
            (tenant_id, data_source_id, list(table_keys)),
        )
        grouped: Dict[str, List[str]] = {key: [] for key in table_keys}
        for row in rows:
            grouped.setdefault(row["table_key"], []).append(row["column_key"])
        return grouped

    async def set_core_columns(
        self,
        tenant_id: str,
        data_source_id: str,
        table_key: str,
        column_keys: Sequence[str],
        *,
        marked_by: str = "",
    ) -> List[str]:
        """Replace this table's core-column set wholesale.

        A curated set is edited as a set -- merging would make removing a
        column from it impossible -- so this deletes whatever is not in
        *column_keys* and upserts the rest, in one transaction so a reader
        never sees a half-applied selection.
        """
        keys = list(dict.fromkeys(column_keys))  # de-duplicate, keep order

        def run(cursor: Any) -> None:
            cursor.execute(
                f"""DELETE FROM {SCHEMA}.core_columns
                     WHERE tenant_id = %s AND data_source_id = %s
                       AND table_key = %s AND NOT (column_key = ANY(%s))""",
                (tenant_id, data_source_id, table_key, keys),
            )
            for column_key in keys:
                cursor.execute(
                    f"""
                    INSERT INTO {SCHEMA}.core_columns
                        (tenant_id, data_source_id, table_key, column_key, marked_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, data_source_id, table_key, column_key)
                    DO UPDATE SET marked_by = EXCLUDED.marked_by,
                                  marked_at = now()
                    """,
                    (tenant_id, data_source_id, table_key, column_key, marked_by),
                )

        await self._transact(run)
        return await self.get_core_columns(tenant_id, data_source_id, table_key)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    async def _transact(self, body: Callable[[Any], None]) -> None:
        """Run ``body(cursor)`` inside one transaction, off the event loop.

        A scan is one transaction so a reader never sees half of it -- a catalog
        with the new tables but the old columns would render a prompt describing
        a schema that never existed.

        Through ``transaction_async`` so the connection is counted. This used to
        call the ungated ``transaction()``, which meant a scan could take a pool
        connection the semaphore did not know about -- and a scan holds it for the
        length of a whole catalog rewrite, which is the worst possible thing to
        hide from the gate.
        """
        await self.db.transact(body)
