"""The grant store contract.

Shaped after :class:`vanna.core.generation.GenerationStore`: async, scoped by
``ToolContext``, and stamping every row with the caller's tenant. A store that
takes a tenant id from an argument rather than from the context is one call away
from a cross-tenant read.

``version`` is the part that carries weight beyond bookkeeping. Every mutation
must increment it, and it must be readable without resolving anything, because a
write approved under one version and executed under another has to refuse. That
is the whole mechanism by which revoking a grant stops an already-approved write.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional, Sequence

from .models import ColumnGrant, EffectiveGrants, TableGrant

if TYPE_CHECKING:  # pragma: no cover
    from ..tool import ToolContext


class GrantStore(ABC):
    """Persistent per-table and per-column grants for one deployment.

    Implementations must:

    * filter every read by ``context.tenant_id`` and stamp every write with it;
    * increment the data source's version inside the same transaction as any
      mutation, so a reader can never observe a changed grant under an unchanged
      version;
    * return an empty :class:`EffectiveGrants` rather than raising when a caller
      has no grants at all. Having nothing granted is the normal state of a new
      deployment, not an error.
    """

    @abstractmethod
    async def resolve(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        roles: Sequence[str],
    ) -> EffectiveGrants:
        """The caller's effective view, unioned across ``roles`` and fail-closed.

        The returned object carries the version observed during this call. It
        must be the version the returned grants were read under, not a later
        one -- re-reading it afterwards would defeat the check it exists for.
        """

    @abstractmethod
    async def version(self, context: "ToolContext", *, data_source_id: str) -> int:
        """The current grant version, without resolving anything."""

    @abstractmethod
    async def set_table_grant(self, context: "ToolContext", grant: TableGrant) -> None:
        """Upsert one table grant and bump the version."""

    @abstractmethod
    async def set_column_grant(self, context: "ToolContext", grant: ColumnGrant) -> None:
        """Upsert one column grant and bump the version."""

    @abstractmethod
    async def replace_table_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        grants: Sequence[TableGrant],
    ) -> None:
        """Replace every table grant for one role. Atomic, and bumps the version."""

    @abstractmethod
    async def replace_column_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        grants: Sequence[ColumnGrant],
    ) -> None:
        """Replace every column grant for one role. Atomic, and bumps the version."""

    @abstractmethod
    async def list_table_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: Optional[str] = None,
    ) -> List[TableGrant]:
        """Stored table grants, for an admin surface to render."""

    @abstractmethod
    async def list_column_grants(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: Optional[str] = None,
        table: Optional[str] = None,
    ) -> List[ColumnGrant]:
        """Stored column grants, for an admin surface to render."""

    async def auto_grant_columns(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        table: str,
        columns: Sequence[str],
        can_write: bool = False,
        unassignable: Sequence[str] = (),
    ) -> None:
        """Grant every column read, and keep autofill's own rows writable in step.

        Granting a table read and then having to grant each of its ninety
        columns individually is the kind of friction that ends in someone
        granting everything. This fills the gaps -- and then does one more
        thing, which is load-bearing rather than convenient.

        **Autofill owns the rows it created and nothing else.** A row it made
        carries ``AUTOFILL`` in ``granted_by`` and follows its table's access
        level; a row a person set carries their identity and is left exactly as
        they left it.

        Fill-the-gaps alone is not enough, and the failure is silent. A table
        cycling No access -> Read only -> Read & write autofills column rows on
        the read step with ``can_write=False``; a gaps-only pass then leaves
        them that way, so the table ends up holding write verbs with nothing
        assignable -- and :func:`vanna.core.write.build_write_policy` resolves
        that by dropping the verbs. "Read & write" would grant nothing at all,
        and say it had.

        Provided rather than abstract: the default is a correct, if chatty,
        implementation over the other methods. A SQL backend should override it
        with one upsert whose ``DO UPDATE`` is conditional on the stored
        ``granted_by`` still being ``AUTOFILL``.
        """
        from .models import AUTOFILL, normalize_identifier

        existing = {
            grant.key: grant
            for grant in await self.list_column_grants(
                context, data_source_id=data_source_id, role=role, table=table
            )
        }
        blocked = {normalize_identifier(name) for name in unassignable}
        for column in columns:
            key = normalize_identifier(column)
            # A generated column is never assignable, so granting write on it
            # would only produce plans the database rejects.
            writable = can_write and key not in blocked
            current = existing.get(key)

            if current is not None:
                if not current.is_machine_managed or current.can_write == writable:
                    continue
                await self.set_column_grant(
                    context, current.model_copy(update={"can_write": writable})
                )
                continue

            await self.set_column_grant(
                context,
                ColumnGrant(
                    tenant_id=getattr(context, "tenant_id", "default"),
                    data_source_id=data_source_id,
                    role=role,
                    table=table,
                    column=column,
                    can_read=True,
                    can_filter=True,
                    can_aggregate=True,
                    can_write=writable,
                    granted_by=AUTOFILL,
                ),
            )

    # -- revocation and bulk application -------------------------------
    #
    # All three are concrete. `MemoryGrantStore` and `PostgresGrantStore` both
    # implement this interface, and an `@abstractmethod` added here would stop
    # either constructing until it was updated -- along with any store outside
    # this repository. The defaults below are correct over the existing methods;
    # a SQL backend overrides them with one statement each.

    async def delete_table_grant(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        table: str,
    ) -> bool:
        """Remove one table grant. Returns whether a row went.

        Revoking is not the same as granting nothing: `set_table_grant` with
        every flag false leaves a row saying "explicitly denied", which is a
        different statement from "never decided" once presets can fill gaps.
        """
        from .models import normalize_table

        wanted = normalize_table(table)
        current = await self.list_table_grants(
            context, data_source_id=data_source_id, role=role
        )
        remaining = [g for g in current if g.key != wanted]
        if len(remaining) == len(current):
            return False
        await self.replace_table_grants(
            context, data_source_id=data_source_id, role=role, grants=remaining
        )
        return True

    async def delete_column_grant(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        table: str,
        column: str,
    ) -> bool:
        """Remove one column grant. Returns whether a row went."""
        from .models import normalize_identifier, normalize_table

        table_key = normalize_table(table)
        column_key = normalize_identifier(column)
        current = await self.list_column_grants(
            context, data_source_id=data_source_id, role=role
        )
        remaining = [
            g
            for g in current
            if not (g.table_key == table_key and g.key == column_key)
        ]
        if len(remaining) == len(current):
            return False
        await self.replace_column_grants(
            context, data_source_id=data_source_id, role=role, grants=remaining
        )
        return True

    async def apply_preset(
        self,
        context: "ToolContext",
        *,
        data_source_id: str,
        role: str,
        table_grants: Sequence[TableGrant],
        column_grants: Sequence[ColumnGrant],
        mode: str = "fill",
        granted_by: str = "",
    ) -> dict:
        """Write a preset's rows. Returns ``{"tables", "columns", "skipped"}``.

        ``fill`` never overwrites an existing row. An administrator who withheld
        a column does not get it back because somebody re-applied a preset, and
        re-running is therefore a no-op except for tables added since.

        ``replace`` discards the role's grants first. A SQL backend narrows that
        to the rows a preset wrote, using the ``source`` column; this generic
        implementation has no such column and so replaces everything for the
        role. The asymmetry is stated rather than hidden -- a caller relying on
        an administrator's edits surviving a `replace` needs the SQL backend.
        """
        # Stamp provenance the caller supplied, so a preset's rows are
        # recognisably machine-generated. Without it they land with no
        # provenance at all -- and a row nobody owns is a row autofill cannot
        # raise `can_write` on, which is how a preset used to leave a table
        # permanently unwritable however many times an administrator clicked
        # "Read & write".
        if granted_by:
            table_grants = list(table_grants)
            column_grants = [
                g if g.granted_by else g.model_copy(update={"granted_by": granted_by})
                for g in column_grants
            ]

        existing_tables = {
            g.key
            for g in await self.list_table_grants(
                context, data_source_id=data_source_id, role=role
            )
        }
        existing_columns = {
            (g.table_key, g.key)
            for g in await self.list_column_grants(
                context, data_source_id=data_source_id, role=role
            )
        }

        if mode == "replace":
            await self.replace_table_grants(
                context, data_source_id=data_source_id, role=role, grants=table_grants
            )
            await self.replace_column_grants(
                context, data_source_id=data_source_id, role=role, grants=column_grants
            )
            return {
                "tables": len(table_grants),
                "columns": len(column_grants),
                "skipped": 0,
            }

        tables = columns = skipped = 0
        for grant in table_grants:
            if grant.key in existing_tables:
                skipped += 1
                continue
            await self.set_table_grant(context, grant)
            tables += 1
        for grant in column_grants:
            if (grant.table_key, grant.key) in existing_columns:
                skipped += 1
                continue
            await self.set_column_grant(context, grant)
            columns += 1

        return {"tables": tables, "columns": columns, "skipped": skipped}
