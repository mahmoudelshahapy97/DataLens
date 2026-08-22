"""Making grants govern reading, not only writing.

The grant model has always described a read surface -- ``can_select`` on a table,
``can_read`` on a column -- and until now nothing consulted it for reads. The
only caller of ``GrantStore.resolve`` was ``WriteService``, so the permission
matrix an administrator filled in decided what could be *changed* and nothing at
all about what could be *seen*. A viewer with no grants could still ask about
every table the workspace connection reached.

Closing that is two layers, which is the same shape the write path already uses:

1. **Before retrieval.** :class:`GrantFilteredCatalog` drops ungranted tables and
   columns from the catalog the prompt is built from. A table the caller may not
   read is not described to the model at all, so it cannot be referenced by
   accident, and the schema section stays within budget for callers with narrow
   access.
2. **Before execution.** The SQL policy is given the filtered catalog and told to
   require it, so a query naming a table the caller cannot read is refused even
   if the model produced the name from the conversation rather than the schema.

Neither layer is sufficient alone. The first is about what the model is told; the
second is about what it is allowed to do, and a model that has seen a table name
earlier in a conversation will happily use it.

**Off unless a workspace turns it on.** Every deployment that exists today grants
nothing, so switching this on globally would deny every table to everybody. It is
per role, via ``grant_policies.enforce_reads``, and the API refuses to enable it
for a role with no grants.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from vanna.core.grants import normalize_identifier, normalize_table

logger = logging.getLogger("vanna.grants.reads")


class GrantFilteredCatalog:
    """A :class:`SchemaCatalog` narrowed to what the caller may read.

    Wraps rather than replaces, and resolves per call rather than per workspace:
    the catalog is built once for a workspace and shared by every user in it, so
    the filtering has to happen against the caller in the context.

    Two different failures, two different answers, and the distinction is
    deliberate:

    * **Grants cannot be resolved.** The exception propagates and the request
      fails. Returning the unfiltered catalog instead would turn a database blip
      into a disclosure.
    * **The policy cannot be read.** Enforcement is skipped. That decides only
      *whether* to filter, and being unable to tell is not evidence that a
      workspace opted in -- treating it as such would take every table away from
      every user of every workspace at once. The grants themselves are still
      checked before any statement executes.
    """

    def __init__(
        self,
        inner: Any,
        *,
        grants: Any,
        data_source_id: str,
        enforced_roles: Any,
    ) -> None:
        self.inner = inner
        self.grants = grants
        self.data_source_id = data_source_id
        # Callable returning the roles for which enforcement is switched on, so
        # a policy change reaches a cached runtime without rebuilding it.
        self._enforced_roles = enforced_roles

    # -- decisions -----------------------------------------------------

    @staticmethod
    def _roles(context: Any) -> List[str]:
        user = getattr(context, "user", None)
        return list(getattr(user, "group_memberships", None) or [])

    async def _effective(self, context: Any) -> Optional[Any]:
        """The caller's grants, or None when this caller is not enforced."""
        roles = self._roles(context)
        if not roles:
            return None

        try:
            enforced = set(await self._enforced_roles())
        except Exception as exc:
            logger.warning("Could not read the read-enforcement policy: %s", exc)
            return None

        if not enforced or not (enforced & {r.lower() for r in roles}):
            return None

        # A system context does the schema scan and the seeding. Filtering it
        # would mean the catalog could never be populated in the first place.
        if getattr(getattr(context, "user", None), "id", "") == "system":
            return None

        return await self.grants.resolve(
            context, data_source_id=self.data_source_id, roles=roles
        )

    def _visible_columns(self, table: Any, effective: Any) -> Optional[Any]:
        """``table`` narrowed to the caller's columns, or None if it is invisible."""
        schema = getattr(table, "schema_name", None)
        name = getattr(table, "table_name", "")
        qualified = f"{schema}.{name}" if schema else name

        granted = effective.table(qualified) or effective.table(name)
        if granted is None or not granted.can_select:
            return None

        allowed: Set[str] = {
            normalize_identifier(c) for c in getattr(granted, "columns", {})
        }
        kept = [
            column
            for column in getattr(table, "columns", []) or []
            if normalize_identifier(column.name) in allowed
        ]
        if not kept:
            # A table with no readable column is not a narrower table, it is an
            # unreadable one -- and describing it would invite a SELECT that the
            # policy then refuses for reasons the model cannot see.
            #
            # This also catches a model that exposes nothing of its own: a pure
            # bridge like `playlist_tracks`, whose only columns are hidden join
            # keys. Such a model disappears from the prompt under enforcement
            # even though nobody revoked it, which looks alarming until you
            # notice it had nothing to show. Joins through it still compile --
            # the semantic compiler reads the manifest, not this catalog.
            return None

        return table.model_copy(update={"columns": kept})

    async def column_uses(self, context: Any) -> Optional[Dict[str, Dict[str, Set[str]]]]:
        """What the caller may *do* with each column, for the SQL validator.

        ``{table_key: {column_key: {"read", "filter", "aggregate"}}}``, or None when
        this caller is not enforced.

        The point of this method is that it resolves grants exactly once, through
        the same :meth:`_effective` the prompt filtering uses. The prompt and the
        validator therefore describe the same world by construction: the planner
        can never be shown a column the validator will subsequently refuse, and --
        more importantly -- the validator can never be handed a wider set than the
        planner saw. Deriving one from the catalog and the other from a second
        lookup is how those two drift apart, and a drift in that direction is a
        bypass.

        Note what the filtered catalog alone cannot tell you: it drops columns the
        caller may not read, so presence implies ``can_read`` -- but ``can_filter``
        and ``can_aggregate`` leave no trace in ``TableMetadata``. Without this the
        two flags could only ever be decorative.
        """
        effective = await self._effective(context)
        if effective is None:
            return None

        uses: Dict[str, Dict[str, Set[str]]] = {}
        for table in getattr(effective, "tables", {}).values():
            if not getattr(table, "can_select", False):
                continue
            columns: Dict[str, Set[str]] = {}
            for column in getattr(table, "columns", {}).values():
                if not column.can_read:
                    # Already dropped from the catalog; saying so twice would let
                    # the two descriptions disagree later.
                    continue
                allowed = {"read"}
                if column.can_filter:
                    allowed.add("filter")
                if column.can_aggregate:
                    allowed.add("aggregate")
                columns[normalize_identifier(column.name)] = allowed
            if columns:
                uses[normalize_table(table.name)] = columns
        return uses

    # -- SchemaCatalog -------------------------------------------------

    async def get_tables(self, context: Any, **kwargs: Any) -> List[Any]:
        tables = await self.inner.get_tables(context, **kwargs)
        effective = await self._effective(context)
        if effective is None:
            return tables

        visible = []
        for table in tables or []:
            narrowed = self._visible_columns(table, effective)
            if narrowed is not None:
                visible.append(narrowed)

        if len(visible) != len(tables or []):
            logger.debug(
                "Read enforcement: %d of %d tables visible to %s",
                len(visible), len(tables or []),
                getattr(getattr(context, "user", None), "email", "?"),
            )
        return visible

    async def get_table(self, context: Any, name: str, **kwargs: Any) -> Optional[Any]:
        table = await self.inner.get_table(context, name, **kwargs)
        if table is None:
            return None
        effective = await self._effective(context)
        if effective is None:
            return table
        return self._visible_columns(table, effective)

    async def search_tables(self, context: Any, query: str, **kwargs: Any) -> List[Any]:
        found = await self.inner.search_tables(context, query, **kwargs)
        effective = await self._effective(context)
        if effective is None:
            return found

        visible = []
        for table in found or []:
            narrowed = self._visible_columns(table, effective)
            if narrowed is not None:
                visible.append(narrowed)
        return visible

    async def get_relationships(self, context: Any, **kwargs: Any) -> List[Any]:
        relationships = await self.inner.get_relationships(context, **kwargs)
        effective = await self._effective(context)
        if effective is None:
            return relationships

        # A relationship naming a table the caller cannot read describes a join
        # they cannot make, and names the table while doing it.
        visible = []
        for edge in relationships or []:
            names = [
                getattr(edge, "from_table", ""),
                getattr(edge, "to_table", ""),
            ]
            if all(
                (effective.table(n) or effective.table(normalize_table(n))) is not None
                for n in names
                if n
            ):
                visible.append(edge)
        return visible

    # -- pass-through --------------------------------------------------
    #
    # Writes to the catalog come from the scanner on a system context, never
    # from a user request, so they are not filtered.

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def unfiltered(catalog: Any) -> Any:
    """The catalog behind any read guard.

    For the administration screens, which have to show every table there is --
    including the ones nobody has been granted yet. Without this an administrator
    whose own role had read enforcement switched on and no grants would open an
    empty permission matrix and have nothing to grant from, which is a lockout
    with no way back through the UI.

    Safe because reaching it requires ``require_tenant_admin`` at the route.
    """
    return getattr(catalog, "inner", catalog)


async def readable_table_names(
    grants: Any, context: Any, *, data_source_id: str
) -> Optional[Set[str]]:
    """Normalized names the caller may select from, or None when unenforced.

    Handed to the SQL policy as ``catalog_tables`` so a query naming anything
    else is refused before it reaches the database.
    """
    roles = list(
        getattr(getattr(context, "user", None), "group_memberships", None) or []
    )
    if not roles:
        return None
    effective = await grants.resolve(
        context, data_source_id=data_source_id, roles=roles
    )
    return {normalize_table(name) for name in effective.tables}
