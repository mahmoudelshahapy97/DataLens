"""The semantic manifest: what a deployment *declares* about its data.

This is the layer above the schema catalog. The catalog records what a scan
found -- tables, columns, types, observed values. The manifest records what
somebody decided those things *mean*: that ``amount_cents / 100.0`` is called
revenue, that orders join to customers this way and not that way, that a
salary column is off-limits below a certain seniority.

Two provenances, deliberately kept apart. A scan can be re-run and will
overwrite itself; a manifest is written by hand, reviewed, and lives in git.
Merging them into one model would mean a rescan could silently discard a
business definition.

Wire format is camelCase, Python is snake_case. That is not decoration: it
makes ``target/mdl.json`` byte-compatible with WrenAI's manifests, so existing
MDL files and WrenAI's own dbt exporters load here unchanged. Retrofitting that
later would be a breaking change to every stored manifest.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


class _MdlModel(BaseModel):
    """Base: camelCase on the wire, snake_case in Python, either on input."""

    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="allow",  # forward compatibility: unknown keys survive a round trip
    )


class JoinType(str, Enum):
    """Cardinality of a relationship.

    Not decoration. ``ONE_TO_MANY`` is what tells the compiler that an aggregate
    reached across this edge will double-count the parent side, which is the
    single most common way a semantic layer returns a confidently wrong number.
    """

    ONE_TO_ONE = "ONE_TO_ONE"
    ONE_TO_MANY = "ONE_TO_MANY"
    MANY_TO_ONE = "MANY_TO_ONE"
    MANY_TO_MANY = "MANY_TO_MANY"

    @property
    def fans_out(self) -> bool:
        """Whether traversing this edge can multiply rows on the left."""
        return self in (JoinType.ONE_TO_MANY, JoinType.MANY_TO_MANY)


class ColumnLevelOperator(str, Enum):
    EQUALS = "EQUALS"
    NOT_EQUALS = "NOT_EQUALS"
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN = "LESS_THAN"
    GREATER_THAN_OR_EQUALS = "GREATER_THAN_OR_EQUALS"
    LESS_THAN_OR_EQUALS = "LESS_THAN_OR_EQUALS"

    @property
    def sql(self) -> str:
        return {
            ColumnLevelOperator.EQUALS: "=",
            ColumnLevelOperator.NOT_EQUALS: "!=",
            ColumnLevelOperator.GREATER_THAN: ">",
            ColumnLevelOperator.LESS_THAN: "<",
            ColumnLevelOperator.GREATER_THAN_OR_EQUALS: ">=",
            ColumnLevelOperator.LESS_THAN_OR_EQUALS: "<=",
        }[self]


class NormalizedExprType(str, Enum):
    NUMERIC = "NUMERIC"
    STRING = "STRING"


class SessionProperty(_MdlModel):
    """A value supplied per request that an access rule reads.

    ``@session_tenant`` in a rule condition resolves to one of these. They are
    populated from the resolved :class:`~vanna.core.user.User`, never from the
    request body -- a session property a caller could set is not a control.
    """

    name: str
    required: bool = True
    default_expr: Optional[str] = None


class NormalizedExpr(_MdlModel):
    """A literal in a column rule, with the type needed to render it safely."""

    value: str
    data_type: NormalizedExprType = NormalizedExprType.STRING

    def to_sql(self) -> str:
        if self.data_type is NormalizedExprType.NUMERIC:
            return self.value
        return "'" + self.value.replace("'", "''") + "'"


class RowLevelAccessControl(_MdlModel):
    """A predicate every query against this model must carry.

    ``condition`` is SQL over the model's own columns, with ``@name`` standing
    in for a session property: ``region = @session_region``. The compiler
    injects it *inside* the model's CTE, where a subquery or a UNION in the
    user's SQL cannot get underneath it.
    """

    name: str
    condition: str
    required_properties: List[SessionProperty] = Field(default_factory=list)


class ColumnLevelAccessControl(_MdlModel):
    """A threshold a session property must meet for a column to be visible.

    When it is not met the column is not rendered NULL -- it is removed from the
    schema entirely, so referencing it is an unknown-column error. A NULLed
    column silently corrupts ``AVG`` and ``SUM`` and the reader never finds out;
    an error is something the agent can report and route around.
    """

    name: str
    operator: ColumnLevelOperator
    threshold: NormalizedExpr
    required_properties: List[SessionProperty] = Field(default_factory=list)


class SemanticColumn(_MdlModel):
    """One column of a model: physical, renamed, or computed.

    Three shapes, distinguished by what is set:

    * plain -- ``name`` matches the physical column
    * renamed -- ``expression`` names a different physical column
    * calculated -- ``is_calculated`` with an ``expression`` over other columns
    * relationship handle -- ``relationship`` set, making ``orders.customer.name``
      resolvable; ``type`` is then the related model's name, not a SQL type
    """

    name: str
    type: str = "VARCHAR"
    description: Optional[str] = None

    expression: Optional[str] = None
    is_calculated: bool = False

    relationship: Optional[str] = None

    not_null: bool = False
    is_primary_key: bool = False
    is_hidden: bool = False
    """Excluded from what the model is shown. For join keys and surrogate ids
    that are needed to compile but only clutter a prompt."""

    column_level_access_control: Optional[ColumnLevelAccessControl] = None

    # Carried over from the physical scan, not authored by hand. Keeping them
    # here means the semantic catalog can present real enum values without
    # re-profiling the database -- `scanner.py` already did that work.
    categories: Optional[List[str]] = None
    sample_values: Optional[List[str]] = None

    @property
    def is_relationship(self) -> bool:
        return bool(self.relationship)

    @property
    def source_expression(self) -> str:
        """SQL for this column inside its model's CTE."""
        return self.expression or self.name


class SemanticModel(_MdlModel):
    """A business object, mapped onto a physical table or a query.

    Exactly one of ``table_reference`` and ``ref_sql`` must be set. Both would be
    ambiguous and neither leaves nothing to select from, so both are rejected at
    validation rather than producing a confusing compile error later.
    """

    name: str
    description: Optional[str] = None

    table_reference: Optional[str] = None
    ref_sql: Optional[str] = None

    columns: List[SemanticColumn] = Field(default_factory=list)
    primary_key: Optional[str] = None

    row_level_access_controls: List[RowLevelAccessControl] = Field(default_factory=list)

    @field_validator("columns")
    @classmethod
    def _reject_duplicate_columns(cls, columns: List[SemanticColumn]):
        seen = set()
        for column in columns:
            key = column.name.lower()
            if key in seen:
                raise ValueError(f"duplicate column {column.name!r}")
            seen.add(key)
        return columns

    def column(self, name: str) -> Optional[SemanticColumn]:
        """Case-insensitive lookup, matching how SQL resolves unquoted names."""
        lowered = name.lower()
        return next((c for c in self.columns if c.name.lower() == lowered), None)

    @property
    def visible_columns(self) -> List[SemanticColumn]:
        return [c for c in self.columns if not c.is_hidden and not c.is_relationship]

    @property
    def relationship_columns(self) -> List[SemanticColumn]:
        return [c for c in self.columns if c.is_relationship]

    @property
    def source(self) -> str:
        """The FROM clause body for this model's CTE."""
        if self.ref_sql:
            return f"({self.ref_sql})"
        return self.table_reference or self.name


class Relationship(_MdlModel):
    """How two models join.

    ``condition`` is free SQL over model names, which is what lets it express a
    composite key -- something the single column-pair ``RelationshipMetadata``
    produced by a foreign-key scan cannot.
    """

    name: str
    models: List[str]
    join_type: JoinType = JoinType.MANY_TO_ONE
    condition: str
    description: Optional[str] = None

    @field_validator("models")
    @classmethod
    def _exactly_two(cls, models: List[str]) -> List[str]:
        if len(models) != 2:
            raise ValueError("a relationship joins exactly two models")
        return models

    def other(self, model_name: str) -> Optional[str]:
        """The far side of this edge, from ``model_name``."""
        lowered = model_name.lower()
        matches = [m for m in self.models if m.lower() != lowered]
        return matches[0] if len(matches) == 1 else None

    def direction_from(self, model_name: str) -> JoinType:
        """Cardinality as seen from ``model_name``.

        ``ONE_TO_MANY`` declared on [a, b] is ``MANY_TO_ONE`` when traversed
        from b. Getting this backwards would put the fan-out warning on exactly
        the wrong queries.
        """
        if self.models and self.models[0].lower() == model_name.lower():
            return self.join_type
        return {
            JoinType.ONE_TO_MANY: JoinType.MANY_TO_ONE,
            JoinType.MANY_TO_ONE: JoinType.ONE_TO_MANY,
        }.get(self.join_type, self.join_type)


class Measure(_MdlModel):
    """A named aggregate. The thing people mean by "a metric"."""

    name: str
    expression: str
    type: str = "DOUBLE"
    description: Optional[str] = None


class Dimension(_MdlModel):
    """Something to group a measure by."""

    name: str
    expression: str
    type: str = "VARCHAR"
    description: Optional[str] = None


class TimeDimension(_MdlModel):
    """A date or timestamp column a cube can be bucketed on.

    The granularity is not declared here -- it is chosen per query, because the
    same column is wanted by day on one screen and by month on another.
    """

    name: str
    expression: str
    type: str = "TIMESTAMP"
    description: Optional[str] = None


class Cube(_MdlModel):
    """Pre-declared measures and dimensions over one model.

    A cube exists so that an aggregate can be *selected* rather than written.
    That is what makes ``QueryCubeTool`` safe: the model picks from a list of
    measures whose aggregation was decided by a human, instead of composing a
    SUM that silently double-counts across a one-to-many join.
    """

    name: str
    base_object: str
    description: Optional[str] = None
    measures: List[Measure] = Field(default_factory=list)
    dimensions: List[Dimension] = Field(default_factory=list)
    time_dimensions: List[TimeDimension] = Field(default_factory=list)

    def measure(self, name: str) -> Optional[Measure]:
        lowered = name.lower()
        return next((m for m in self.measures if m.name.lower() == lowered), None)

    def dimension(self, name: str) -> Optional[Union[Dimension, TimeDimension]]:
        lowered = name.lower()
        for item in (*self.dimensions, *self.time_dimensions):
            if item.name.lower() == lowered:
                return item
        return None


class View(_MdlModel):
    """A saved statement, exposed under a name.

    ``statement`` is native SQL in the project's dialect. The compiler injects
    it verbatim rather than compiling it, so a view is the escape hatch for
    anything the semantic layer cannot express.
    """

    name: str
    statement: str
    description: Optional[str] = None


class Manifest(_MdlModel):
    """Everything declared, in one document."""

    catalog: str = "vanna"
    schema_name: str = Field(default="public", alias="schema")
    data_source: Optional[str] = None

    models: List[SemanticModel] = Field(default_factory=list)
    relationships: List[Relationship] = Field(default_factory=list)
    views: List[View] = Field(default_factory=list)
    cubes: List[Cube] = Field(default_factory=list)

    # -- lookup --------------------------------------------------------

    def model(self, name: str) -> Optional[SemanticModel]:
        lowered = name.lower()
        return next((m for m in self.models if m.name.lower() == lowered), None)

    def view(self, name: str) -> Optional[View]:
        lowered = name.lower()
        return next((v for v in self.views if v.name.lower() == lowered), None)

    def cube(self, name: str) -> Optional[Cube]:
        lowered = name.lower()
        return next((c for c in self.cubes if c.name.lower() == lowered), None)

    def relationship(self, name: str) -> Optional[Relationship]:
        lowered = name.lower()
        return next((r for r in self.relationships if r.name.lower() == lowered), None)

    def relationships_for(self, model_name: str) -> List[Relationship]:
        lowered = model_name.lower()
        return [
            r for r in self.relationships
            if any(m.lower() == lowered for m in r.models)
        ]

    @property
    def model_names(self) -> List[str]:
        return [m.name for m in self.models]

    @property
    def queryable_names(self) -> List[str]:
        """Everything that may appear in a FROM clause."""
        return [*self.model_names, *(v.name for v in self.views)]

    # -- io ------------------------------------------------------------

    def to_json_dict(self) -> Dict[str, Any]:
        """camelCase dict, suitable for ``target/mdl.json``."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")

    @classmethod
    def from_json_dict(cls, raw: Dict[str, Any]) -> "Manifest":
        return cls.model_validate(raw or {})
