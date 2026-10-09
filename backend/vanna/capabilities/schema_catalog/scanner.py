"""Database introspection: populate the catalog by scanning a live database.

The scanner runs entirely through the :class:`SqlRunner` capability rather than
opening its own connections, so it works against every engine Vanna supports,
inherits the runner's read-only enforcement and timeouts, and needs no new
dependency.

Beyond reading structure it **profiles** low-cardinality columns: for each
candidate, ``SELECT DISTINCT col LIMIT n+1``. If fewer than ``n`` distinct
values come back, the column is enum-like and the values are stored. The
``LIMIT n+1`` is the whole design in one line -- it bounds the cost, and the
boundary itself is the signal (n+1 rows returned means "too many to enumerate,
don't store").

Why this matters more than it looks: the most common cause of wrong-but-valid
SQL is an invented literal. A model writing ``WHERE status = 'active'`` against
a column holding ``'ACTIVE'`` produces a syntactically perfect query that
silently returns zero rows. Capturing the real values at scan time removes that
failure mode entirely, with no runtime cost and no extra round trip.

Scanning is an operator action, not a request-path action. Run it from a CLI, a
scheduled job, or an admin-gated tool.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from ..sql_runner import RunSqlToolArgs, SqlRunner
from .inference import containment_sql, infer_relationships, rescore
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

#: Column names whose contents are never sampled or profiled, however low their
#: cardinality. Sampling writes real data into the catalog, and the catalog is
#: rendered into prompts -- an egress path that bypasses row-level security
#: entirely. Redaction is therefore opt-out, not opt-in.
SENSITIVE_COLUMN_PATTERNS = (
    r"pass(word|wd)?",
    r"secret",
    r"token",
    r"api[_-]?key",
    r"credential",
    r"ssn|social_security",
    r"credit_?card|card_?number|cvv",
    r"salt|hash",
    r"private[_-]?key",
    r"auth",
    r"email",
    r"phone",
    r"address",
    r"dob|date_of_birth|birth_?date",
)

_SENSITIVE_RE = re.compile("|".join(SENSITIVE_COLUMN_PATTERNS), re.IGNORECASE)

#: Types worth profiling. Numerics and timestamps are excluded: a low distinct
#: count on an integer column is usually coincidence, not an enumeration, and
#: listing dates as "categories" is actively misleading.
PROFILABLE_TYPE_HINTS = (
    "char",
    "text",
    "string",
    "enum",
    "bool",
    "uuid",
    "varchar",
    "nvarchar",
)

#: Column names that hold free text or identifiers rather than a closed set of
#: values. A 50-row ``name`` column technically has "few" distinct values, but
#: enumerating customer names into the prompt is noise that crowds out real
#: schema and tells the model nothing it can use in a WHERE clause.
FREE_TEXT_COLUMN_PATTERNS = (
    r"^name$|_name$|^title$|^label$",
    r"description|comment|note|summary|body|content|message",
    r"^url$|_url$|^uri$|^slug$|^path$|^filename$",
    r"^id$|_id$|^uuid$|^guid$|^key$|_key$|^code$|_code$",
)

_FREE_TEXT_RE = re.compile("|".join(FREE_TEXT_COLUMN_PATTERNS), re.IGNORECASE)

#: A column whose distinct values exceed this fraction of its rows is an
#: identifier, not a category, however few rows the table has. Guards the case
#: the absolute ``max_categories`` ceiling misses: a 30-row lookup table where
#: every value is unique.
MAX_DISTINCT_RATIO = 0.5


def is_free_text_column(name: str) -> bool:
    """True if *name* suggests free text or an identifier rather than an enum."""
    return bool(_FREE_TEXT_RE.search(name))


def is_sensitive_column(name: str) -> bool:
    """True if *name* looks like it holds sensitive data."""
    return bool(_SENSITIVE_RE.search(name))


def _cell(row: Any, name: str) -> Any:
    """One column of a result row, whatever case the engine returned it in.

    ``information_schema`` is standard; the *case* its columns come back in is
    not. PostgreSQL and SQLite hand back ``table_name``, MySQL hands back
    ``TABLE_NAME`` -- because that is how MySQL declares them and DictCursor
    reports what the server said. Oracle uppercases everything by convention.

    Reading only the lowercase spelling made every MySQL scan produce a table
    called ``None.None``, which failed validation with a message about a string
    -- an error a long way from its cause. Rather than normalise every driver's
    rows somewhere else, every read of a result row goes through here.
    """
    if not isinstance(row, dict):
        return None
    if name in row:
        return row[name]
    lowered = name.lower()
    for key, value in row.items():
        if str(key).lower() == lowered:
            return value
    return None


class SchemaScanner:
    """Populates a :class:`SchemaCatalog` by introspecting a live database.

    Args:
        runner: Executes the introspection queries. Its execution policy and
            read-only enforcement apply, so a scan cannot modify anything.
        dialect: Engine dialect. Selects the introspection queries.
        profile_cardinality: Detect and record enum-like columns.
        max_categories: Distinct-value ceiling for treating a column as
            enum-like.
        sample_rows: Capture example values for non-enum columns to convey
            format. Disable wholesale for regulated tenants.
        redact_sensitive: Skip profiling and sampling for columns whose names
            match :data:`SENSITIVE_COLUMN_PATTERNS`. Leave enabled.
        infer_relationships: Propose joins for ``*_id`` columns no foreign key
            declares (see :mod:`.inference`). Stored as proposed, labelled
            inferred, and used unreviewed only when confident.
        verify_inferred: Sample each proposed join's data and raise or lower
            its confidence by whether the values actually match. One bounded
            query per candidate.
    """

    def __init__(
        self,
        runner: SqlRunner,
        *,
        dialect: str = "generic",
        profile_cardinality: bool = True,
        max_categories: int = 100,
        sample_rows: bool = False,
        redact_sensitive: bool = True,
        infer_relationships: bool = True,
        verify_inferred: bool = True,
    ) -> None:
        self.runner = runner
        self.dialect = dialect or getattr(runner, "dialect", None) or "generic"
        self.profile_cardinality = profile_cardinality
        self.max_categories = max_categories
        self.sample_rows = sample_rows
        self.redact_sensitive = redact_sensitive
        self.infer_relationships = infer_relationships
        self.verify_inferred = verify_inferred

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        context: "ToolContext",
        catalog: "SchemaCatalog",
        *,
        tables: Optional[Sequence[str]] = None,
        schema: Optional[str] = None,
        data_source_id: str = "default",
    ) -> ScanReport:
        """Scan the database and write the results into *catalog*.

        A failure on one table is recorded in the report and the scan
        continues. Aborting a 200-table scan because one view has a permission
        problem would make the feature unusable on exactly the large schemas
        that need it most.
        """
        started = time.perf_counter()
        report = ScanReport()

        try:
            discovered = await self._list_tables(context, schema=schema)
        except Exception as e:
            report.errors.append(f"Could not list tables: {e}")
            report.duration_ms = (time.perf_counter() - started) * 1000
            return report

        if tables:
            wanted = {t.lower() for t in tables}
            discovered = [
                t
                for t in discovered
                if t[1].lower() in wanted or f"{t[0]}.{t[1]}".lower() in wanted
            ]

        collected: List[TableMetadata] = []
        relationships: List[RelationshipMetadata] = []

        for schema_name, table_name in discovered:
            try:
                meta = await self._scan_table(
                    context,
                    schema_name,
                    table_name,
                    data_source_id=data_source_id,
                )
                collected.append(meta)
                report.tables_scanned += 1
                report.columns_profiled += len(meta.columns)
                report.categories_found += sum(
                    1 for c in meta.columns if c.categories
                )
                relationships.extend(
                    self._relationships_from(meta, data_source_id=data_source_id)
                )
            except Exception as e:
                logger.warning("Failed scanning %s.%s: %s", schema_name, table_name, e)
                report.errors.append(f"{schema_name}.{table_name}: {e}")
                collected.append(
                    TableMetadata(
                        table_name=table_name,
                        schema_name=schema_name,
                        status=CatalogStatus.FAILED,
                        error_message=str(e),
                        tenant_id=getattr(context, "tenant_id", "default"),
                        data_source_id=data_source_id,
                    )
                )

        if self.infer_relationships:
            scanned = [t for t in collected if t.status != CatalogStatus.FAILED]
            inferred = infer_relationships(
                scanned, relationships, data_source_id=data_source_id
            )
            if self.verify_inferred:
                inferred = [await self._verify(context, rel) for rel in inferred]
            relationships.extend(inferred)
            report.relationships_inferred = len(inferred)

        if collected:
            await catalog.upsert_tables(context, collected)
        if relationships:
            await catalog.upsert_relationships(context, relationships)
            report.relationships_found = len(relationships)

        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    async def _verify(
        self, context: "ToolContext", rel: RelationshipMetadata
    ) -> RelationshipMetadata:
        """Rescore an inferred join from a data sample; unchanged on any error."""
        try:
            rows = await self._query(context, containment_sql(rel))
        except Exception as e:
            logger.debug("Could not verify %s: %s", rel.name, e)
            return rel
        if not rows:
            return rel
        sampled = int(_cell(rows[0], "sampled") or 0)
        orphans = int(_cell(rows[0], "orphans") or 0)
        return rescore(rel, sampled, orphans)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    async def _query(self, context: "ToolContext", sql: str) -> List[dict]:
        df = await self.runner.run_sql(RunSqlToolArgs(sql=sql), context)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    async def _list_tables(
        self, context: "ToolContext", *, schema: Optional[str] = None
    ) -> List[tuple]:
        """Return ``(schema_name, table_name)`` pairs."""
        if self.dialect == "sqlite":
            rows = await self._query(
                context,
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name",
            )
            return [(None, _cell(r, "name")) for r in rows]

        # In MySQL a "schema" *is* a database, and one server routinely holds a
        # dozen unrelated ones. "Everything that is not a system schema" therefore
        # means every other customer's database on the same server: a scan of
        # `sakila` came back with 71 tables belonging to booking, chinook,
        # healthcare and northwind. Wrong, and a disclosure.
        #
        # `DATABASE()` is the one the connection string named, which is exactly the
        # scope the workspace was pointed at. PostgreSQL is unaffected -- its
        # schemas live inside one database and the connection already bounds them.
        if schema is None and self.dialect == "mysql":
            current = await self._query(context, "SELECT DATABASE() AS db")
            schema = _cell(current[0], "db") if current else None

        # information_schema is supported by PostgreSQL, MySQL, Snowflake,
        # SQL Server, DuckDB, ClickHouse, and BigQuery (per-dataset).
        where = (
            f"AND table_schema = '{schema}'"
            if schema
            else "AND table_schema NOT IN "
            "('information_schema','pg_catalog','sys','performance_schema','mysql')"
        )
        rows = await self._query(
            context,
            "SELECT table_schema, table_name FROM information_schema.tables "
            f"WHERE table_type IN ('BASE TABLE','VIEW') {where} "
            "ORDER BY table_schema, table_name",
        )
        return [(_cell(r, "table_schema"), _cell(r, "table_name")) for r in rows]

    async def _scan_table(
        self,
        context: "ToolContext",
        schema_name: Optional[str],
        table_name: str,
        *,
        data_source_id: str,
    ) -> TableMetadata:
        from datetime import datetime, timezone

        columns = await self._list_columns(context, schema_name, table_name)
        qualified = f"{schema_name}.{table_name}" if schema_name else table_name

        row_count = await self._row_count(context, qualified)

        if self.profile_cardinality:
            for column in columns:
                await self._profile_column(context, qualified, column, row_count)

        return TableMetadata(
            table_name=table_name,
            schema_name=schema_name,
            columns=columns,
            row_count_estimate=row_count,
            status=CatalogStatus.SCANNED,
            last_synced_at=datetime.now(timezone.utc),
            tenant_id=getattr(context, "tenant_id", "default"),
            data_source_id=data_source_id,
        )

    async def _row_count(
        self, context: "ToolContext", qualified_table: str
    ) -> Optional[int]:
        """Row count for the table, or None if unavailable.

        Used both to populate ``row_count_estimate`` -- which lets the agent
        prefer aggregation over row-by-row retrieval on large tables -- and as
        the denominator for the distinct-ratio test in column profiling.

        ``COUNT(*)`` can be slow on very large tables, so failures (including
        the runner's own timeout) are swallowed: a catalog without row counts
        is still useful, and profiling falls back to the absolute ceiling.
        """
        try:
            rows = await self._query(
                context, f"SELECT COUNT(*) AS n FROM {qualified_table}"
            )
        except Exception as e:
            logger.debug("Row count unavailable for %s: %s", qualified_table, e)
            return None
        if not rows:
            return None
        try:
            return int(_cell(rows[0], "n") or 0)
        except (KeyError, TypeError, ValueError):
            return None

    async def _list_columns(
        self,
        context: "ToolContext",
        schema_name: Optional[str],
        table_name: str,
    ) -> List[ColumnMetadata]:
        if self.dialect == "sqlite":
            # `table_xinfo` is `table_info` plus a `hidden` flag, where 2 and 3
            # mark VIRTUAL and STORED generated columns. It has been available
            # since 3.26 (2018); older builds fall back to the plain form and
            # simply report no generated columns.
            try:
                rows = await self._query(
                    context, f'PRAGMA table_xinfo("{table_name}")'
                )
            except Exception:
                rows = await self._query(context, f'PRAGMA table_info("{table_name}")')
            columns = [
                ColumnMetadata(
                    name=r["name"],
                    data_type=(_cell(r, "type") or "unknown").lower(),
                    nullable=not _cell(r, "notnull"),
                    is_primary_key=bool(_cell(r, "pk")),
                    is_generated=int(_cell(r, "hidden") or 0) in (2, 3),
                    has_default=_cell(r, "dflt_value") is not None,
                )
                for r in rows
            ]
            for fk in await self._query(
                context, f'PRAGMA foreign_key_list("{table_name}")'
            ):
                for column in columns:
                    if column.name == fk.get("from"):
                        column.foreign_key = ForeignKey(
                            column=column.name,
                            references_table=fk.get("table", ""),
                            references_column=fk.get("to") or "",
                        )
            return columns

        schema_filter = (
            f"AND table_schema = '{schema_name}'" if schema_name else ""
        )
        base = (
            "FROM information_schema.columns "
            f"WHERE table_name = '{table_name}' {schema_filter} "
            "ORDER BY ordinal_position"
        )
        # `is_generated` and `is_identity` exist on PostgreSQL 12+ and MySQL
        # 5.7+, but not in SQL Server's information_schema. Rather than keep a
        # per-engine column matrix in sync, ask for the richer shape and fall
        # back to the portable one. An engine that cannot answer reports no
        # generated columns, which costs assignability -- the database still
        # refuses the assignment -- and never costs safety. `column_default`
        # is portable, and its absence only ever makes a column *more* likely
        # to be treated as required on insert.
        try:
            rows = await self._query(
                context,
                "SELECT column_name, data_type, is_nullable, column_default, "
                f"is_generated, is_identity {base}",
            )
        except Exception as e:
            logger.debug("Generated-column facts unavailable for %s: %s", table_name, e)
            rows = await self._query(
                context,
                f"SELECT column_name, data_type, is_nullable, column_default {base}",
            )

        def _yes(value: Any) -> bool:
            # is_nullable/is_identity spell it 'YES'/'NO'; is_generated spells
            # it 'ALWAYS'/'NEVER'. Both are text, and neither is a boolean.
            return str(value or "").strip().upper() in ("YES", "ALWAYS")

        columns = [
            ColumnMetadata(
                name=_cell(r, "column_name"),
                data_type=(_cell(r, "data_type") or "unknown").lower(),
                nullable=str(_cell(r, "is_nullable") or "YES").upper() == "YES",
                is_generated=(
                    _yes(_cell(r, "is_generated")) or _yes(_cell(r, "is_identity"))
                ),
                has_default=_cell(r, "column_default") is not None,
            )
            for r in rows
        ]
        await self._apply_key_constraints(context, schema_name, table_name, columns)
        return columns

    async def _apply_key_constraints(
        self,
        context: "ToolContext",
        schema_name: Optional[str],
        table_name: str,
        columns: List[ColumnMetadata],
    ) -> None:
        """Mark primary keys and foreign keys from information_schema.

        Best-effort: constraint views vary between engines and are often
        restricted. A catalog without key metadata is still useful, so failure
        here is logged and ignored rather than failing the table.
        """
        schema_filter = f"AND tc.table_schema = '{schema_name}'" if schema_name else ""
        try:
            rows = await self._query(
                context,
                "SELECT tc.constraint_type, kcu.column_name, "
                "       ccu.table_name AS ref_table, "
                "       ccu.column_name AS ref_column "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "LEFT JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                f"WHERE tc.table_name = '{table_name}' {schema_filter} "
                "  AND tc.constraint_type IN ('PRIMARY KEY','FOREIGN KEY')",
            )
        except Exception as e:
            logger.debug("Key constraints unavailable for %s: %s", table_name, e)
            return

        by_name = {c.name.lower(): c for c in columns}
        for row in rows:
            column = by_name.get(str(_cell(row, "column_name") or "").lower())
            if not column:
                continue
            if _cell(row, "constraint_type") == "PRIMARY KEY":
                column.is_primary_key = True
            elif (
                _cell(row, "constraint_type") == "FOREIGN KEY"
                and _cell(row, "ref_table")
            ):
                column.foreign_key = ForeignKey(
                    column=column.name,
                    references_table=str(_cell(row, "ref_table")),
                    references_column=str(_cell(row, "ref_column") or ""),
                )

    async def _profile_column(
        self,
        context: "ToolContext",
        qualified_table: str,
        column: ColumnMetadata,
        row_count: Optional[int] = None,
    ) -> None:
        """Detect and record enum-like values for one column.

        Skipped for sensitive names, non-text types, and primary keys (unique
        by definition, so profiling them is pure cost). Failures are ignored:
        profiling is an enhancement, and a permission error on one column must
        not fail the table.
        """
        if self.redact_sensitive and is_sensitive_column(column.name):
            return
        if column.is_primary_key:
            return
        if is_free_text_column(column.name):
            return
        if not any(hint in column.data_type for hint in PROFILABLE_TYPE_HINTS):
            return

        # LIMIT n+1: bounded cost, and the boundary is the signal.
        probe = self.max_categories + 1
        try:
            rows = await self._query(
                context,
                f'SELECT DISTINCT "{column.name}" AS v '
                f"FROM {qualified_table} "
                f'WHERE "{column.name}" IS NOT NULL '
                f"LIMIT {probe}",
            )
        except Exception as e:
            logger.debug("Could not profile %s.%s: %s", qualified_table, column.name, e)
            return

        if not rows:
            return

        values = [
            str(_cell(r, "v")) for r in rows if _cell(r, "v") is not None
        ]

        if len(values) > self.max_categories:
            if self.sample_rows:
                # Too many to enumerate; a few examples still convey the format.
                column.sample_values = values[:3]
            return

        # Absolute count alone is not enough: a 40-row lookup table where every
        # value is unique passes a "<= 100 distinct" test but is an identifier
        # column, not an enumeration. Compare against the row count.
        if row_count is not None and row_count > 0:
            if len(values) / row_count > MAX_DISTINCT_RATIO:
                if self.sample_rows:
                    column.sample_values = values[:3]
                return

        column.low_cardinality = True
        column.categories = sorted(values)

    @staticmethod
    def _relationships_from(
        table: TableMetadata, *, data_source_id: str
    ) -> List[RelationshipMetadata]:
        """Derive join paths from a table's foreign keys."""
        relationships = []
        for column in table.columns:
            fk = column.foreign_key
            if not fk:
                continue
            relationships.append(
                RelationshipMetadata(
                    name=f"{table.qualified_name}.{column.name}"
                    f"->{fk.references_table}",
                    from_table=table.qualified_name,
                    from_column=column.name,
                    to_table=fk.references_table,
                    to_column=fk.references_column,
                    join_type="many_to_one",
                    tenant_id=table.tenant_id,
                    data_source_id=data_source_id,
                )
            )
        return relationships
