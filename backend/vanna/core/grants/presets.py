"""Named starting points for a role's grants.

A fresh workspace grants nothing, which is the right default and a poor place to
start: an administrator faces an empty matrix, ninety columns per table, and no
indication of what a reasonable answer looks like. In practice that ends one of
two ways -- nobody grants anything and the product does not work, or somebody
grants everything to make it work.

A preset is the shape of a sensible answer, applied in one action and edited
afterwards. It is not a new authorization concept: applying one writes ordinary
:class:`TableGrant` and :class:`ColumnGrant` rows, which
:func:`~vanna.core.grants.resolve.resolve_grants` then reads exactly as it reads
hand-authored ones. Nothing here is consulted when a caller's access is decided.

Defined in code rather than in YAML deliberately. These are security semantics
that have to version alongside the resolution rules they produce rows for, and a
file would need a loader, a schema, a search path and a deployment question for
about forty lines of content. :func:`register_preset` keeps them extensible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import ColumnGrant, TableGrant, normalize_table
from .resolve import catalog_write_facts


@dataclass(frozen=True)
class GrantPreset:
    """What one role may do, before anybody edits it."""

    name: str
    title: str
    description: str

    can_select: bool = False
    can_insert: bool = False
    can_update: bool = False
    can_delete: bool = False

    column_read: bool = False
    column_filter: bool = False
    column_aggregate: bool = False
    column_write: bool = False

    def __post_init__(self) -> None:
        # The same invariants the grant models enforce, checked where the preset
        # is defined rather than on the row it eventually produces. A preset that
        # cannot be stored is a programming error, not a runtime one.
        if (self.can_insert or self.can_update or self.can_delete) and not self.can_select:
            raise ValueError(f"preset {self.name!r}: a write verb requires select")
        for flag in ("column_filter", "column_aggregate", "column_write"):
            if getattr(self, flag) and not self.column_read:
                raise ValueError(f"preset {self.name!r}: {flag} requires column_read")


#: Nothing granted. The current behaviour of every workspace, named so that it
#: can be chosen and displayed rather than being the absence of a choice.
NONE = GrantPreset(
    name="none",
    title="No access",
    description="Grants nothing. Every table stays invisible until it is granted.",
)

#: Read, but not row-by-row interrogation.
#:
#: `column_filter` is deliberately off. `ColumnGrant`'s own docstring makes the
#: point: a column you may filter on but not read still answers questions about
#: individual rows, one predicate at a time -- so a viewer who cannot see
#: `salary` should not be able to ask which employees earn more than 100,000.
VIEWER = GrantPreset(
    name="viewer",
    title="Viewer",
    description=(
        "Read, filter and aggregate the granted tables. A column withheld from "
        "the role stays withheld everywhere -- it cannot be read, filtered on, or "
        "aggregated -- which is what stops row-by-row interrogation."
    ),
    can_select=True,
    column_read=True,
    # `column_filter` was False here, and the description above used to explain
    # that as "cannot filter on a column it cannot read". The two never matched.
    # A preset applies to the columns it *grants*, so the flag did not withhold
    # filtering on unreadable columns -- those are dropped from the catalog long
    # before a predicate is checked -- it withheld filtering on every column the
    # viewer could already see in full.
    #
    # Nothing was protected by that. If you may read a column, you may read every
    # row of it, and narrowing by it afterwards reveals nothing you could not
    # already page through. What it did cost was every ordinary question: with the
    # AST check now live, a viewer could not write WHERE, ORDER BY or JOIN ON at
    # all.
    #
    # `can_filter` still means what it says, and is still enforced -- an
    # administrator who clears it on one sensitive column gets exactly the
    # narrowing they asked for. It is only the blanket default that was wrong.
    column_filter=True,
    column_aggregate=True,
)

ANALYST = GrantPreset(
    name="analyst",
    title="Analyst",
    description="Full read access: select, filter and aggregate every column granted.",
    can_select=True,
    column_read=True,
    column_filter=True,
    column_aggregate=True,
)

ADMIN = GrantPreset(
    name="admin",
    title="Administrator",
    description=(
        "Read and write. Writes still go through the approval flow and are "
        "re-checked against these grants before they run."
    ),
    can_select=True,
    can_insert=True,
    can_update=True,
    can_delete=True,
    column_read=True,
    column_filter=True,
    column_aggregate=True,
    column_write=True,
)

_REGISTRY: Dict[str, GrantPreset] = {
    p.name: p for p in (NONE, VIEWER, ANALYST, ADMIN)
}

#: The presets shipped with the library.
BUILTIN_PRESETS: Mapping[str, GrantPreset] = dict(_REGISTRY)


def get_preset(name: str) -> Optional[GrantPreset]:
    return _REGISTRY.get((name or "").strip().lower())


def register_preset(preset: GrantPreset) -> None:
    """Add a deployment-specific preset. Replaces one of the same name."""
    _REGISTRY[preset.name.strip().lower()] = preset


def preset_names() -> List[str]:
    return sorted(_REGISTRY)


def preset_grants(
    preset: GrantPreset,
    *,
    data_source_id: str,
    role: str,
    tables: Iterable,
    tenant_id: str = "default",
    only: Optional[Sequence[str]] = None,
) -> Tuple[List[TableGrant], List[ColumnGrant]]:
    """Expand a preset over a catalog into the rows it would grant.

    ``tables`` is anything shaped like ``TableMetadata``. It must come from the
    **scanned catalog** rather than a live query: the catalog is what the write
    policy is built from, so granting from anything else grants columns the
    policy cannot see.

    ``only`` restricts the expansion to the named tables, matched on their
    normalized names.

    A generated column never gets ``can_write`` -- the database would refuse the
    assignment anyway, and a grant that cannot be exercised is noise in the
    matrix. Table verbs, by contrast, are **not** pre-narrowed against missing
    primary keys: :func:`resolve_grants` does that on every resolution from the
    current catalog, and freezing the fact into a stored row would leave
    ``can_update`` false long after the table gained a key.
    """
    if preset.name == NONE.name:
        return [], []

    wanted = {normalize_table(t) for t in (only or [])} or None
    _, unassignable = catalog_write_facts(tables)

    table_grants: List[TableGrant] = []
    column_grants: List[ColumnGrant] = []

    for table in tables:
        schema = getattr(table, "schema_name", None)
        name = getattr(table, "table_name", "")
        qualified = f"{schema}.{name}" if schema else name
        if not name:
            continue
        key = normalize_table(qualified)
        if wanted is not None and key not in wanted:
            continue

        table_grants.append(
            TableGrant(
                tenant_id=tenant_id,
                data_source_id=data_source_id,
                role=role,
                table=qualified,
                can_select=preset.can_select,
                can_insert=preset.can_insert,
                can_update=preset.can_update,
                can_delete=preset.can_delete,
            )
        )

        blocked = {c.lower() for c in unassignable.get(key, [])}
        for column in getattr(table, "columns", []) or []:
            column_grants.append(
                ColumnGrant(
                    tenant_id=tenant_id,
                    data_source_id=data_source_id,
                    role=role,
                    table=qualified,
                    column=column.name,
                    can_read=preset.column_read,
                    can_filter=preset.column_filter,
                    can_aggregate=preset.column_aggregate,
                    can_write=(
                        preset.column_write and column.name.lower() not in blocked
                    ),
                )
            )

    return table_grants, column_grants
