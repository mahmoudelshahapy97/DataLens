"""Per-table and per-column grants: what a caller may see, and what they may change.

This answers a different question from :mod:`vanna.core.access`, and the two must
not be conflated. Access rules decide **which rows** a caller sees, by pushing a
predicate into the query. Grants decide **which tables and columns exist at all**
for that caller, and **which verbs** are permitted on them. A deployment can use
either alone; together, grants pick the surface and access rules narrow the rows.

Three properties are load-bearing:

**Fail closed.** A column with no grant row is not masked, not nulled -- it is
dropped from the caller's world entirely, so naming it is an unknown-column error
rather than a permission error. There is nothing to probe.

**Writes imply reads.** Every write flag requires its read flag. Granting the
ability to change a row you cannot see is not a coherent permission, and the one
place it could be expressed is the one place it must be refused. The invariant is
declared here as a validator, and again as a CHECK constraint in any SQL backend;
that redundancy is deliberate.

**Union across roles.** A caller holding two roles gets the union of their grants,
never the intersection. Adding a role can only ever widen.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


#: Marks a column grant that autofill created rather than a person.
AUTOFILL = "autofill"

#: Prefix for any other machine-generated provenance, e.g. ``system:preset``.
MACHINE_PREFIX = "system:"


#: How a readable column is obscured, if at all.
#:
#: Masking is deliberately the *weakest* of the three things that can happen to a
#: column, and the order below is the order of decreasing protection:
#:
#:   no grant / can_read=False   the column is dropped from the caller's world.
#:                               Naming it is an unknown-column error. There is
#:                               nothing to probe. This is the default and the
#:                               recommendation.
#:   "null"                      projected as NULL. Honest about hiding, and it
#:                               corrupts AVG and SUM exactly as dropping avoids.
#:   "partial"                   first two characters, then ***. Leaks a prefix by
#:                               construction -- that is what it is for.
#:   "hash"                      a stable pseudonym. Equality and distinct-counts
#:                               still work, which is the use and also the leak.
#:   "none"                      the column as itself.
#:
#: A mask is not a substitute for withholding a column. It exists because the
#: alternative people reach for is granting the column outright and hoping.
MASK_STRATEGIES = ("none", "hash", "partial", "null")


def normalize_identifier(name: str) -> str:
    """The key form of a table or column name.

    Casefolded and stripped of the quoting a caller may have typed, so that
    ``Orders``, ``orders`` and ``"orders"`` are one entry rather than three.
    Every dict in this module is keyed on the result, and the write policy uses
    the same function, so a grant written one way matches a plan written another.
    """
    cleaned = (name or "").strip()
    for quote in ('"', "`", "'"):
        if len(cleaned) >= 2 and cleaned.startswith(quote) and cleaned.endswith(quote):
            cleaned = cleaned[1:-1]
            break
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return cleaned.casefold()


def normalize_table(name: str) -> str:
    """The key form of a possibly-qualified table name (``Sales.Orders`` -> ``sales.orders``)."""
    parts = [p for p in (name or "").split(".") if p.strip()]
    return ".".join(normalize_identifier(part) for part in parts)


class TableGrant(BaseModel):
    """What one role may do to one table.

    ``can_select`` is the gate: without it the table is invisible, and the three
    write flags are refused by the validator below rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = "default"
    data_source_id: str
    role: str = Field(
        description="Matches an entry in User.group_memberships. A caller holding "
        "several roles receives the union of their grants."
    )
    table: str = Field(description="Table name, optionally schema-qualified.")

    can_select: bool = False
    can_insert: bool = False
    can_update: bool = False
    can_delete: bool = False

    @model_validator(mode="after")
    def write_requires_select(self) -> "TableGrant":
        if (self.can_insert or self.can_update or self.can_delete) and not self.can_select:
            raise ValueError(
                "a write grant requires can_select: changing rows you cannot read "
                "is not a permission this model can express"
            )
        return self

    @property
    def key(self) -> str:
        return normalize_table(self.table)


class ColumnGrant(BaseModel):
    """What one role may do with one column.

    ``can_filter`` and ``can_aggregate`` are separate from ``can_read`` because
    they leak differently: a column you may filter on but not read still answers
    questions about individual rows one predicate at a time.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = "default"
    data_source_id: str
    role: str
    table: str
    column: str

    can_read: bool = False
    can_filter: bool = False
    can_aggregate: bool = False
    can_write: bool = False

    mask: str = Field(
        default="none",
        description="How the value is obscured when it is read. See MASK_STRATEGIES.",
    )

    granted_by: Optional[str] = Field(
        default=None,
        description="Who set this. A person's identity claims the row and takes "
        "it out of autofill's hands; AUTOFILL, a system: marker, or nothing at "
        "all leaves it machine-managed.",
    )

    @property
    def is_autofilled(self) -> bool:
        """Created by autofill specifically."""
        return self.granted_by == AUTOFILL

    @property
    def is_machine_managed(self) -> bool:
        """Whether autofill may keep this row in step with its table.

        True unless a **person** claimed the row. Autofill's own rows qualify,
        so do preset rows (``system:preset``), and so do rows with no provenance
        at all -- the last because the alternative is worse in a specific way.

        Autofill has to be able to raise ``can_write`` when a table becomes
        writable. If it cannot, the table ends up holding write verbs with
        nothing assignable, and ``build_write_policy`` answers that by dropping
        the verbs: "Read & write" grants nothing and reports success. Treating
        an unclaimed row as claimed reintroduces exactly that. Treating it as
        machine-managed risks re-granting a column withheld before provenance
        existed -- and every path that withholds one now records who did it.
        """
        marker = (self.granted_by or "").strip()
        return (
            not marker
            or marker == AUTOFILL
            or marker.startswith(MACHINE_PREFIX)
        )

    @model_validator(mode="after")
    def dependent_permissions_require_read(self) -> "ColumnGrant":
        if (self.can_filter or self.can_aggregate or self.can_write) and not self.can_read:
            raise ValueError(
                "can_filter, can_aggregate and can_write each require can_read"
            )
        return self

    @property
    def key(self) -> str:
        return normalize_identifier(self.column)

    @property
    def table_key(self) -> str:
        return normalize_table(self.table)


class EffectiveColumn(BaseModel):
    """One column as it appears to a specific caller, after all their roles are unioned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="The column's real name, in its original case.")
    can_read: bool = True
    can_filter: bool = True
    can_aggregate: bool = True
    can_write: bool = False
    #: The *narrowest* mask across the caller's roles -- see the note in
    #: `resolve.py`, where this is the one flag that does not simply OR.
    mask: str = "none"

    @property
    def is_masked(self) -> bool:
        return self.mask != "none"


class EffectiveTable(BaseModel):
    """One table as it appears to a specific caller.

    Only reachable if ``can_select`` held and at least one column survived: a
    table whose every column was withheld is not a table with no columns, it is a
    table the caller does not have.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Qualified name in its original case.")
    can_select: bool = True
    can_insert: bool = False
    can_update: bool = False
    can_delete: bool = False
    #: Keyed on the normalized column name.
    columns: Dict[str, EffectiveColumn] = Field(default_factory=dict)

    def column(self, name: str) -> Optional[EffectiveColumn]:
        return self.columns.get(normalize_identifier(name))

    def permits(self, operation: str) -> bool:
        return {
            "insert": self.can_insert,
            "update": self.can_update,
            "delete": self.can_delete,
        }.get(operation, False)


class EffectiveGrants(BaseModel):
    """Everything one caller may see and change on one data source, resolved.

    ``version`` is the reason this type carries a number at all. It is the store's
    grant version at the moment of resolution, and a write approved under one
    version must not execute under another -- see the re-authorization step in
    :mod:`vanna.core.write.approval`. Comparing versions is how a grant revoked
    between approval and execution refuses the write instead of silently
    honouring permissions that no longer exist.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = "default"
    data_source_id: str
    version: int = Field(
        default=0,
        description="The grant store's version at resolution time. Bumped by "
        "every mutation; re-checked before an approved write executes.",
    )
    #: Keyed on the normalized qualified table name.
    tables: Dict[str, EffectiveTable] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.tables

    def table(self, name: str) -> Optional[EffectiveTable]:
        """Look a table up by any spelling of its name.

        Falls back to a unique unqualified match, so a caller granted
        ``sales.orders`` may write ``orders`` -- but only while that is
        unambiguous. Two schemas holding an ``orders`` each resolve to None,
        which the validator turns into a refusal rather than a guess.
        """
        key = normalize_table(name)
        if key in self.tables:
            return self.tables[key]
        if "." in key:
            return None
        matches = [
            table for candidate, table in self.tables.items()
            if candidate.rsplit(".", 1)[-1] == key
        ]
        return matches[0] if len(matches) == 1 else None

    def writable_tables(self) -> List[EffectiveTable]:
        """Tables carrying at least one write verb. Usually empty, which is fine."""
        return [
            table for table in self.tables.values()
            if table.can_insert or table.can_update or table.can_delete
        ]
