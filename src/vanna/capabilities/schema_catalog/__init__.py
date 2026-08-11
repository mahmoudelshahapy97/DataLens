"""Schema catalog: structural knowledge about the databases the agent queries.

Vanna 2.0's agent had no way to learn a database's structure -- DDL training
existed only in the 0.x legacy path. This capability restores it, and adds
value-level profiling the legacy path never had.

    from vanna.capabilities.schema_catalog import SchemaScanner
    from vanna.integrations.local import LocalSchemaCatalog

    catalog = LocalSchemaCatalog("./catalog.json")
    scanner = SchemaScanner(runner, dialect="postgres")
    report = await scanner.scan(context, catalog)
    print(report.summary())

    ctx = await catalog.get_context(context, "revenue by region last quarter")
    print(ctx.strategy, ctx.char_count)
"""

from .base import SchemaCatalog
from .dbt import DbtImporter
from .freshness import (
    SourceFingerprint,
    dbt_fingerprint,
    knowledge_fingerprint,
    watch,
)
from .describe import (
    SCHEMA_FULL_TEXT_THRESHOLD,
    describe_schema,
    describe_table_names,
)
from .models import (
    CatalogStatus,
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    ScanReport,
    SchemaContext,
    TableMetadata,
)
from .scanner import (
    SENSITIVE_COLUMN_PATTERNS,
    SchemaScanner,
    is_sensitive_column,
)

__all__ = [
    "SchemaCatalog",
    "SchemaScanner",
    "DbtImporter",
    "SourceFingerprint",
    "knowledge_fingerprint",
    "dbt_fingerprint",
    "watch",
    "TableMetadata",
    "ColumnMetadata",
    "RelationshipMetadata",
    "ForeignKey",
    "SchemaContext",
    "ScanReport",
    "CatalogStatus",
    "describe_schema",
    "describe_table_names",
    "SCHEMA_FULL_TEXT_THRESHOLD",
    "is_sensitive_column",
    "SENSITIVE_COLUMN_PATTERNS",
]
