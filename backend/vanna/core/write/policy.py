"""What the caller may change, projected from grants and the catalog.

A deliberate sibling of the read path rather than an extension of it. Read
authorization is load-bearing for every request the system serves, and widening
it by accident while adding writes would be the worst regression available here.
Two projections over the same inputs, each answering one question, means a
mistake in this module can only ever make writes *more* restricted -- it cannot
make reads less so.

The projection is narrower than the read one in three ways, all of which exist so
that an authorized plan is also an **executable** one:

* A table needs at least one writable column, or there is nothing to assign.
* A table needs at least one key column, or UPDATE and DELETE cannot name the
  rows they mean and would fall back to touching everything.
* Generated columns are dropped from assignability, because the database refuses
  to assign them -- but kept when they are keys, because a generated surrogate
  key is exactly what UPDATE and DELETE address rows *by*, and exactly what a
  later step references.

Anything this module leaves out is refused by :func:`validate_write_plan`.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..grants import EffectiveGrants, normalize_identifier, normalize_table
from .errors import WriteCode, WriteRefusal
from .models import MAX_TRANSACTION_STEPS

#: Dialects whose INSERT can hand back a generated key. Only these can carry a
#: multi-step plan whose child row references its parent's key.
RETURNING_DIALECTS = frozenset({"postgres", "postgresql", "sqlite", "duckdb"})


class WritableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: Assignable in an INSERT or UPDATE.
    can_write: bool
    #: Usable in an UPDATE or DELETE predicate, and as a reference target.
    is_primary_key: bool
    #: An INSERT that omits this column produces a row the database refuses.
    required_on_insert: bool


class WritableTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Qualified name in its original case -- what gets rendered into SQL.
    name: str
    schema_name: Optional[str] = None
    can_insert: bool
    can_update: bool
    can_delete: bool
    #: Keyed on the normalized column name.
    columns: Dict[str, WritableColumn] = Field(default_factory=dict)

    def permits(self, operation: str) -> bool:
        return {
            "insert": self.can_insert,
            "update": self.can_update,
            "delete": self.can_delete,
        }.get(operation, False)

    def column(self, name: str) -> Optional[WritableColumn]:
        return self.columns.get(normalize_identifier(name))

    @property
    def key_columns(self) -> set:
        return {
            key for key, column in self.columns.items() if column.is_primary_key
        }

    @property
    def required_insert_columns(self) -> set:
        return {
            key
            for key, column in self.columns.items()
            if column.required_on_insert
        }


class WritableReference(BaseModel):
    """One foreign key a cross-step reference may be honoured across.

    Everything is normalized, matching how tables and columns are keyed
    everywhere else in this module.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str


class WritePolicy(BaseModel):
    """What the caller may modify, and how much of it at a time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dialect: str
    max_rows: int = Field(ge=1)
    #: Keyed on the normalized qualified table name.
    tables: Dict[str, WritableTable] = Field(default_factory=dict)
    #: Declared foreign keys between two writable tables. Empty is normal.
    references: List[WritableReference] = Field(default_factory=list)
    max_steps: int = Field(default=MAX_TRANSACTION_STEPS, ge=1)
    #: The grant version these permissions were read under. Carried so an
    #: approved plan can prove the world has not moved beneath it.
    grants_version: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.tables

    @property
    def supports_returning(self) -> bool:
        return self.dialect.lower() in RETURNING_DIALECTS

    def resolve_table(self, name: str) -> Optional[WritableTable]:
        """Find a permitted table by any spelling, or None.

        A bare name resolves only when exactly one permitted table matches.
        Ambiguity returns None, which the validator turns into a refusal --
        never a guess. Picking one of two tables called ``orders`` and writing
        to it is the kind of help nobody wants.
        """
        key = normalize_table(name)
        if key in self.tables:
            return self.tables[key]
        if "." in key:
            return None
        matches = [
            table
            for candidate, table in self.tables.items()
            if candidate.rsplit(".", 1)[-1] == key
        ]
        return matches[0] if len(matches) == 1 else None

    def permits_reference(
        self,
        *,
        child_table: str,
        child_column: str,
        parent_table: str,
        parent_column: str,
    ) -> bool:
        """Whether a declared foreign key links these columns, in this direction.

        Direction matters and is not symmetric: ``order_items.order_id`` may take
        its value from ``orders.order_id``, never the reverse. Without this
        check an authorized plan could put an arbitrary generated key into an
        arbitrary writable column -- syntactically valid, semantically nonsense,
        and a corruption the row-count assertion cannot catch, because the count
        would be exactly right.
        """
        child_t, child_c = normalize_table(child_table), normalize_identifier(child_column)
        parent_t = normalize_table(parent_table)
        parent_c = normalize_identifier(parent_column)
        return any(
            reference.child_table == child_t
            and reference.child_column == child_c
            and reference.parent_table == parent_t
            and reference.parent_column == parent_c
            for reference in self.references
        )


def build_write_policy(
    grants: EffectiveGrants,
    tables: Iterable,
    *,
    dialect: str,
    max_rows: int,
    max_steps: int = MAX_TRANSACTION_STEPS,
) -> WritePolicy:
    """Project grants and catalog metadata into what may actually be written.

    ``tables`` is anything shaped like ``TableMetadata`` -- a name, a schema, and
    columns carrying ``is_primary_key``, ``is_generated``, ``nullable`` and
    ``has_default``. Both inputs are required: grants alone cannot tell you
    which column addresses a row, and the catalog alone says nothing about
    permission.

    Unlike the read policy, an empty result is **not** an error. Having nothing
    writable is the normal state of almost every deployment, and raising here
    would turn "you may not change anything" into a fault report.
    """
    catalog = _index_catalog(tables)
    permitted: Dict[str, WritableTable] = {}

    for table_key, granted in grants.tables.items():
        if not (granted.can_insert or granted.can_update or granted.can_delete):
            continue
        facts = catalog.get(table_key)
        if facts is None:
            # Granted but unknown to the catalog. Refusing is the only safe
            # answer: without column facts there is no way to know what
            # addresses a row or what the database will refuse to assign.
            continue

        columns: Dict[str, WritableColumn] = {}
        for column_key, fact in facts.columns.items():
            granted_column = granted.columns.get(column_key)
            if granted_column is None or not granted_column.can_read:
                continue  # withheld columns do not exist for this caller
            assignable = granted_column.can_write and not fact.is_generated
            if not (assignable or fact.is_primary_key):
                # Keep only what can be assigned or what can address a row.
                # A column that is neither cannot appear in any statement this
                # module builds, so carrying it would only widen the surface a
                # plan can name.
                continue
            columns[column_key] = WritableColumn(
                name=fact.name,
                can_write=assignable,
                is_primary_key=fact.is_primary_key,
                required_on_insert=fact.required_on_insert,
            )

        has_assignable = any(column.can_write for column in columns.values())
        addressable = any(column.is_primary_key for column in columns.values())

        can_insert = granted.can_insert and has_assignable
        can_update = granted.can_update and has_assignable and addressable
        can_delete = granted.can_delete and addressable
        if not (can_insert or can_update or can_delete):
            continue

        permitted[table_key] = WritableTable(
            name=facts.name,
            schema_name=facts.schema_name,
            can_insert=can_insert,
            can_update=can_update,
            can_delete=can_delete,
            columns=columns,
        )

    return WritePolicy(
        dialect=dialect,
        max_rows=max_rows,
        max_steps=max_steps,
        tables=permitted,
        references=_writable_references(catalog, permitted),
        grants_version=grants.version,
    )


# ----------------------------------------------------------------------
# Catalog indexing
# ----------------------------------------------------------------------


class _ColumnFacts:
    __slots__ = ("name", "is_primary_key", "is_generated", "required_on_insert")

    def __init__(self, column) -> None:
        self.name = column.name
        self.is_primary_key = bool(getattr(column, "is_primary_key", False))
        self.is_generated = bool(getattr(column, "is_generated", False))
        required = getattr(column, "required_on_insert", None)
        if required is None:
            # Older catalog objects predate the derived property. Reconstruct
            # it from the parts, defaulting to "not required" so a missing fact
            # never turns into a demand the caller cannot satisfy.
            required = not (
                getattr(column, "nullable", True)
                or self.is_generated
                or getattr(column, "has_default", False)
            )
        self.required_on_insert = bool(required)


class _TableFacts:
    __slots__ = ("name", "schema_name", "columns", "foreign_keys")

    def __init__(self, table) -> None:
        self.schema_name = getattr(table, "schema_name", None)
        raw = getattr(table, "table_name", "")
        self.name = f"{self.schema_name}.{raw}" if self.schema_name else raw
        self.columns: Dict[str, _ColumnFacts] = {}
        self.foreign_keys: List[tuple] = []
        for column in getattr(table, "columns", []) or []:
            key = normalize_identifier(column.name)
            if key in self.columns:
                # Two columns whose names differ only by case. Everything here
                # is keyed on the normalized form, so honouring one would mean
                # silently picking which. Refuse the table instead.
                raise WriteRefusal(
                    WriteCode.TABLE_NOT_ALLOWED,
                    f"{self.name} has two columns whose names differ only by "
                    "case, so a write plan cannot name one unambiguously",
                    detail=self.name,
                )
            self.columns[key] = _ColumnFacts(column)
            foreign_key = getattr(column, "foreign_key", None)
            if foreign_key is not None:
                self.foreign_keys.append(
                    (
                        key,
                        normalize_table(getattr(foreign_key, "references_table", "")),
                        normalize_identifier(
                            getattr(foreign_key, "references_column", "")
                        ),
                    )
                )


def _index_catalog(tables: Iterable) -> Dict[str, _TableFacts]:
    indexed: Dict[str, _TableFacts] = {}
    for table in tables or []:
        facts = _TableFacts(table)
        key = normalize_table(facts.name)
        if key in indexed:
            raise WriteRefusal(
                WriteCode.TABLE_NOT_ALLOWED,
                f"the catalog holds two tables named {facts.name} once "
                "case is disregarded",
                detail=facts.name,
            )
        indexed[key] = facts
    return indexed


def _writable_references(
    catalog: Dict[str, _TableFacts],
    permitted: Dict[str, WritableTable],
) -> List[WritableReference]:
    """Foreign keys where both ends are writable by this caller.

    A reference whose parent the caller cannot write is not a reference this
    plan could ever use, since the parent row would have to be inserted by an
    earlier step.
    """
    references: List[WritableReference] = []
    for table_key, table in permitted.items():
        facts = catalog.get(table_key)
        if facts is None:
            continue
        for child_column, parent_table, parent_column in facts.foreign_keys:
            if not parent_table or not parent_column:
                continue
            if parent_table == table_key:
                continue  # self-reference: the parent row is this row
            parent = permitted.get(parent_table)
            if parent is None or parent_column not in parent.columns:
                continue
            if child_column not in table.columns:
                continue
            references.append(
                WritableReference(
                    child_table=table_key,
                    child_column=child_column,
                    parent_table=parent_table,
                    parent_column=parent_column,
                )
            )
    return references


def describe_write_policy(policy: WritePolicy) -> str:
    """A prompt-ready summary of what may be written.

    Given to the model alongside the schema, so it proposes plans that can
    actually be authorized rather than discovering the boundary by refusal.
    """
    if policy.is_empty:
        return "No tables are writable. Answer change requests by explaining that."

    lines: List[str] = []
    for table in sorted(policy.tables.values(), key=lambda t: t.name):
        verbs = [
            verb
            for verb, allowed in (
                ("insert", table.can_insert),
                ("update", table.can_update),
                ("delete", table.can_delete),
            )
            if allowed
        ]
        keys = sorted(
            column.name for column in table.columns.values() if column.is_primary_key
        )
        assignable = sorted(
            column.name for column in table.columns.values() if column.can_write
        )
        required = sorted(
            column.name
            for column in table.columns.values()
            if column.required_on_insert
        )
        line = f"- {table.name}: may {', '.join(verbs)}"
        if keys:
            line += f"; address rows by {', '.join(keys)}"
        if assignable:
            line += f"; may assign {', '.join(assignable)}"
        if required and table.can_insert:
            line += f"; an insert must supply {', '.join(required)}"
        lines.append(line)

    if policy.references:
        lines.append(
            "Values may be carried between steps along these foreign keys: "
            + "; ".join(
                f"{r.child_table}.{r.child_column} from "
                f"{r.parent_table}.{r.parent_column}"
                for r in policy.references
            )
        )
    lines.append(
        f"At most {policy.max_rows} row(s) across at most {policy.max_steps} step(s)."
    )
    return "\n".join(lines)
