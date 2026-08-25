"""The semantic layer: what a deployment declares about its data.

    from vanna.semantic import Manifest, validate_manifest

    manifest = Manifest.from_json_dict(json.loads(path.read_text()))
    issues = validate_manifest(manifest)

The catalog (``vanna.capabilities.schema_catalog``) records what a scan found.
This records what a human decided it means. Both are tenant-scoped, and they are
kept apart so a rescan can never overwrite a business definition.
"""

from .describe import (
    SEMANTIC_FULL_TEXT_THRESHOLD,
    describe_cube,
    describe_manifest,
    describe_model,
)
from .from_catalog import manifest_from_catalog, model_from_table
from .models import (
    ColumnLevelAccessControl,
    ColumnLevelOperator,
    Cube,
    Dimension,
    JoinType,
    Manifest,
    Measure,
    NormalizedExpr,
    NormalizedExprType,
    Relationship,
    RowLevelAccessControl,
    SemanticColumn,
    SemanticModel,
    SessionProperty,
    TimeDimension,
    View,
)
from .project import (
    build_manifest,
    load_built_manifest,
    load_manifest_from_project,
    manifest_from_documents,
    write_model_yaml,
    write_relationships_yaml,
)
from .validate import SemanticIssue, Severity, has_errors, validate_manifest

__all__ = [
    # models
    "Manifest",
    "SemanticModel",
    "SemanticColumn",
    "Relationship",
    "JoinType",
    "Cube",
    "Measure",
    "Dimension",
    "TimeDimension",
    "View",
    "RowLevelAccessControl",
    "ColumnLevelAccessControl",
    "ColumnLevelOperator",
    "SessionProperty",
    "NormalizedExpr",
    "NormalizedExprType",
    # project io
    "load_manifest_from_project",
    "manifest_from_documents",
    "build_manifest",
    "load_built_manifest",
    "write_model_yaml",
    "write_relationships_yaml",
    # generation
    "manifest_from_catalog",
    "model_from_table",
    # validation
    "validate_manifest",
    "SemanticIssue",
    "Severity",
    "has_errors",
    # description
    "describe_manifest",
    "describe_model",
    "describe_cube",
    "SEMANTIC_FULL_TEXT_THRESHOLD",
]
