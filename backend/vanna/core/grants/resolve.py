"""Turning stored grant rows into one caller's effective view.

Pure functions over data already fetched. Kept separate from the store so the
resolution rules -- which are the security-relevant part -- can be read and
tested without a database, and so every backend necessarily agrees about them.

The rules, in the order they bite:

1. **Union across roles.** Each flag is OR-ed over every role the caller holds.
2. **Columns fail closed.** No grant row, or ``can_read=False``, and the column
   is dropped. Not masked -- dropped, so it cannot be named at all. A *mask* is a
   separate, weaker thing that applies only to a column that survives this step;
   see ``_MASK_REVEALS`` for why it unions by taking the most revealing rather
   than by OR-ing.
3. **Tables fail closed.** No ``can_select``, or no surviving columns, and the
   table is dropped.
4. **Write verbs need a target.** ``can_insert``/``can_update`` survive only if
   some column is writable; ``can_update``/``can_delete`` survive only if some
   column can address a row. A verb with nothing to act on is not a permission,
   it is a statement that cannot be built.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

from .models import (
    ColumnGrant,
    EffectiveColumn,
    EffectiveGrants,
    EffectiveTable,
    TableGrant,
    normalize_identifier,
    normalize_table,
)


def resolve_grants(
    *,
    data_source_id: str,
    roles: Sequence[str],
    table_grants: Iterable[TableGrant],
    column_grants: Iterable[ColumnGrant],
    version: int = 0,
    tenant_id: str = "default",
    key_columns: Optional[Dict[str, Sequence[str]]] = None,
    unassignable_columns: Optional[Dict[str, Sequence[str]]] = None,
) -> EffectiveGrants:
    """Resolve stored grants into one caller's effective view.

    ``key_columns`` and ``unassignable_columns`` are the catalog's contribution,
    keyed on the normalized table name: which columns can address a row, and
    which the database will refuse an assignment to (generated and computed
    ones). They are optional because the read side does not need them; omitting
    them costs only the write-verb narrowing in step 4, never any widening.
    """
    held = {role.casefold() for role in roles if role}
    key_columns = key_columns or {}
    unassignable_columns = unassignable_columns or {}

    tables: Dict[str, _TableAccumulator] = {}
    for grant in table_grants:
        if grant.role.casefold() not in held:
            continue
        tables.setdefault(grant.key, _TableAccumulator(grant.table)).merge_table(grant)

    columns: Dict[str, Dict[str, _ColumnAccumulator]] = {}
    for grant in column_grants:
        if grant.role.casefold() not in held:
            continue
        table_key = grant.table_key
        if table_key not in tables:
            # A column grant for a table the caller cannot select is not an
            # error and not a partial grant -- it is inert.
            continue
        bucket = columns.setdefault(table_key, {})
        bucket.setdefault(grant.key, _ColumnAccumulator(grant.column)).merge_column(grant)

    resolved: Dict[str, EffectiveTable] = {}
    for table_key, accumulator in tables.items():
        if not accumulator.can_select:
            continue

        unassignable = {
            normalize_identifier(name)
            for name in unassignable_columns.get(table_key, ())
        }
        keys = {normalize_identifier(name) for name in key_columns.get(table_key, ())}

        effective_columns = {
            column_key: column.build(assignable=column_key not in unassignable)
            for column_key, column in columns.get(table_key, {}).items()
            if column.can_read
        }
        if not effective_columns:
            # Every column withheld means the caller does not have this table,
            # rather than having a table with no columns.
            continue

        has_assignable = any(column.can_write for column in effective_columns.values())
        # A row is addressable when the caller can see a key column. Without
        # one, UPDATE and DELETE cannot name the rows they mean.
        addressable = (not keys) or any(
            column_key in keys for column_key in effective_columns
        )

        resolved[table_key] = EffectiveTable(
            name=accumulator.name,
            can_select=True,
            can_insert=accumulator.can_insert and has_assignable,
            can_update=accumulator.can_update and has_assignable and addressable,
            can_delete=accumulator.can_delete and addressable,
            columns=effective_columns,
        )

    return EffectiveGrants(
        tenant_id=tenant_id,
        data_source_id=data_source_id,
        version=version,
        tables=resolved,
    )


def catalog_write_facts(tables: Iterable) -> tuple:
    """Extract the two catalog inputs :func:`resolve_grants` wants.

    Accepts anything shaped like ``TableMetadata`` -- a qualified name and a
    list of columns carrying ``is_primary_key`` and ``is_generated``. Returns
    ``(key_columns, unassignable_columns)``, both keyed on the normalized table
    name.
    """
    keys: Dict[str, List[str]] = {}
    unassignable: Dict[str, List[str]] = {}
    for table in tables:
        schema = getattr(table, "schema_name", None)
        name = getattr(table, "table_name", "")
        qualified = f"{schema}.{name}" if schema else name
        table_key = normalize_table(qualified)
        for column in getattr(table, "columns", []) or []:
            if getattr(column, "is_primary_key", False):
                keys.setdefault(table_key, []).append(column.name)
            if getattr(column, "is_generated", False):
                unassignable.setdefault(table_key, []).append(column.name)
    return keys, unassignable


class _TableAccumulator:
    """OR-accumulator for one table across the caller's roles."""

    __slots__ = ("name", "can_select", "can_insert", "can_update", "can_delete")

    def __init__(self, name: str) -> None:
        self.name = name
        self.can_select = False
        self.can_insert = False
        self.can_update = False
        self.can_delete = False

    def merge_table(self, grant: TableGrant) -> None:
        self.can_select |= grant.can_select
        self.can_insert |= grant.can_insert
        self.can_update |= grant.can_update
        self.can_delete |= grant.can_delete


#: How much plaintext each mask reveals. Higher is more revealing.
#:
#: A total order is needed because of rule 1 -- adding a role can only ever widen
#: -- and masks are the one flag that is not a boolean to OR. A caller holding
#: `analyst` (email hashed) and `support` (email in the clear) must see it in the
#: clear: the alternative is that *gaining* a role takes something away, which is
#: not a permission model anybody can reason about.
#:
#: `hash` below `partial` is a judgement, and it is the conservative one. They leak
#: different things -- `hash` gives a stable pseudonym and no characters, `partial`
#: gives two characters and a weaker pseudonym -- so they are not comparable on one
#: axis. Ranking by *characters of plaintext revealed* is the reading that never
#: silently widens: choosing the other order would let a `partial` role be masked
#: down to `hash`, which is a narrowing, which rule 1 forbids.
_MASK_REVEALS = {"null": 0, "hash": 1, "partial": 2, "none": 3}


class _ColumnAccumulator:
    """OR-accumulator for one column across the caller's roles."""

    __slots__ = ("name", "can_read", "can_filter", "can_aggregate", "can_write", "_reveals")

    def __init__(self, name: str) -> None:
        self.name = name
        self.can_read = False
        self.can_filter = False
        self.can_aggregate = False
        self.can_write = False
        # Starts fully masked. A column nobody granted read on never reaches
        # `build`, so this only matters as the floor a first grant raises.
        self._reveals = _MASK_REVEALS["null"]

    def merge_column(self, grant: ColumnGrant) -> None:
        self.can_read |= grant.can_read
        self.can_filter |= grant.can_filter
        self.can_aggregate |= grant.can_aggregate
        self.can_write |= grant.can_write

        # Only a role that actually grants read gets a say in the mask. A role
        # with can_read=False and mask="none" is not saying "show it in the
        # clear"; it is saying nothing, and letting it vote would unmask a column
        # for everybody who happens to also hold it.
        if grant.can_read:
            self._reveals = max(
                self._reveals, _MASK_REVEALS.get(grant.mask, _MASK_REVEALS["null"])
            )

    @property
    def mask(self) -> str:
        for name, rank in _MASK_REVEALS.items():
            if rank == self._reveals:
                return name
        return "null"

    def build(self, *, assignable: bool) -> EffectiveColumn:
        return EffectiveColumn(
            name=self.name,
            can_read=self.can_read,
            can_filter=self.can_filter,
            can_aggregate=self.can_aggregate,
            mask=self.mask,
            # A generated column is never assignable however it was granted:
            # the database refuses the assignment, so offering it produces a
            # plan that can only fail at execution.
            can_write=self.can_write and assignable,
        )
