"""Role presets, and the change that makes a role mean anything.

Two independent problems had to be solved for a permission matrix to do
something, and either one alone leaves it decorative.

**Nobody matched a role.** Grants resolve against ``user.group_memberships``, and
``identity.py`` filled that with ``["user"]`` for everybody who was not an admin.
A grant written for ``analyst`` was accepted by the API, stored happily, resolved
for nobody, and looked exactly like one that worked. The fix puts the workspace
role into the group list; :class:`TestTheRoleIsAGroup` pins both that it is there
and the consumers that were checked before it was.

**Nothing was ever granted.** A fresh workspace had no rows, no screen, and no
starting point, which in practice ends with somebody granting everything. A
preset is the shape of a reasonable answer, expanded over the scanned catalog
into ordinary grant rows -- so nothing here is a new authorization concept, and
:func:`resolve_grants` is untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vanna.core.grants import (
    BUILTIN_PRESETS,
    ColumnGrant,
    TableGrant,
    get_preset,
    preset_grants,
    resolve_grants,
)


def column(name: str, *, key: bool = False, generated: bool = False):
    return SimpleNamespace(name=name, is_primary_key=key, is_generated=generated)


CATALOG = [
    SimpleNamespace(
        schema_name="erp",
        table_name="orders",
        columns=[
            column("order_id", key=True),
            column("total"),
            column("total_with_tax", generated=True),
        ],
    ),
    SimpleNamespace(
        schema_name="erp",
        table_name="customers",
        columns=[column("customer_id", key=True), column("email")],
    ),
]


class TestPresetDefinitions:
    @pytest.mark.parametrize("name", sorted(BUILTIN_PRESETS))
    def test_every_preset_produces_storable_rows(self, name: str):
        """The models refuse a write verb without select, and a column flag
        without read. A preset that cannot be stored is a bug at import time."""
        preset = BUILTIN_PRESETS[name]
        tables, columns = preset_grants(
            preset, data_source_id="wh", role="r", tables=CATALOG
        )
        for grant in tables:
            TableGrant(**grant.model_dump())
        for grant in columns:
            ColumnGrant(**grant.model_dump())

    def test_none_grants_nothing(self):
        tables, columns = preset_grants(
            get_preset("none"), data_source_id="wh", role="viewer", tables=CATALOG
        )
        assert (tables, columns) == ([], [])

    def test_a_viewer_may_use_every_column_it_is_given(self):
        """A preset grants columns; it is not how a column is withheld.

        This used to assert `can_filter` was False for a viewer, on the reasoning
        in `ColumnGrant`'s docstring -- that a column you may filter on but not
        read answers questions one predicate at a time. That reasoning is sound
        and this was the wrong place for it: a preset only ever writes rows for
        columns it is *granting*, all of which are readable, so the flag withheld
        filtering on columns the viewer could already read in full. It protected
        nothing and, once the AST check went live, stopped a viewer writing WHERE
        at all.

        Withholding is done by not granting the column -- see
        `test_a_withheld_column_is_withheld_for_every_use`.
        """
        _, columns = preset_grants(
            get_preset("viewer"), data_source_id="wh", role="viewer", tables=CATALOG
        )
        assert all(c.can_read for c in columns)
        assert all(c.can_filter for c in columns)
        assert not any(c.can_write for c in columns)

    def test_a_table_left_out_of_a_preset_is_granted_nothing(self):
        """The invariant the viewer preset was reaching for.

        Withholding is done by not granting, not by clearing a flag. A table
        outside `only=` gets no row at all -- no table grant and no column grants
        -- and resolution is fail-closed, so nothing about it can be read,
        filtered on or aggregated. There is no state in which a column is
        filterable but invisible.
        """
        keep = CATALOG[0]
        kept = f"{keep.schema_name}.{keep.table_name}"
        tables, columns = preset_grants(
            get_preset("viewer"),
            data_source_id="wh",
            role="viewer",
            tables=CATALOG,
            only=[kept],
        )

        assert {t.table for t in tables} == {kept}
        assert {c.table for c in columns} == {kept}
        # Every other table in the catalog contributed nothing at all -- no table
        # row, and so no column rows either.
        assert len(CATALOG) > 1
        for other in CATALOG[1:]:
            excluded = f"{other.schema_name}.{other.table_name}"
            assert all(t.table != excluded for t in tables)
            assert all(c.table != excluded for c in columns)

    def test_an_analyst_may_filter(self):
        _, columns = preset_grants(
            get_preset("analyst"), data_source_id="wh", role="analyst", tables=CATALOG
        )
        assert all(c.can_filter and c.can_aggregate for c in columns)
        assert not any(c.can_write for c in columns)

    def test_only_the_admin_preset_grants_writes(self):
        for name in ("viewer", "analyst"):
            tables, _ = preset_grants(
                get_preset(name), data_source_id="wh", role=name, tables=CATALOG
            )
            assert not any(
                t.can_insert or t.can_update or t.can_delete for t in tables
            ), f"{name} must not grant a write verb"

        tables, _ = preset_grants(
            get_preset("admin"), data_source_id="wh", role="admin", tables=CATALOG
        )
        assert all(t.can_insert and t.can_update and t.can_delete for t in tables)

    def test_a_generated_column_is_never_writable(self):
        """The database would refuse the assignment; a grant for it is noise."""
        _, columns = preset_grants(
            get_preset("admin"), data_source_id="wh", role="admin", tables=CATALOG
        )
        generated = next(c for c in columns if c.column == "total_with_tax")
        assert generated.can_write is False
        assert generated.can_read is True

    def test_catalog_facts_are_not_frozen_into_table_verbs(self):
        """A table with no primary key still gets `can_delete` stored.

        `resolve_grants` narrows verbs from the *current* catalog on every
        resolution. Baking the fact into the row would leave the verb switched
        off long after the table gained a key.
        """
        keyless = [
            SimpleNamespace(
                schema_name="erp", table_name="events", columns=[column("payload")]
            )
        ]
        tables, _ = preset_grants(
            get_preset("admin"), data_source_id="wh", role="admin", tables=keyless
        )
        assert tables[0].can_delete is True

    def test_only_restricts_the_expansion(self):
        tables, columns = preset_grants(
            get_preset("analyst"),
            data_source_id="wh",
            role="analyst",
            tables=CATALOG,
            only=["erp.orders"],
        )
        assert [t.table for t in tables] == ["erp.orders"]
        assert {c.table for c in columns} == {"erp.orders"}


class TestPresetsResolve:
    """The point of the whole exercise: rows a preset wrote grant real access."""

    def test_an_applied_preset_resolves_for_that_role(self):
        tables, columns = preset_grants(
            get_preset("analyst"), data_source_id="wh", role="analyst", tables=CATALOG
        )
        effective = resolve_grants(
            data_source_id="wh",
            roles=["analyst"],
            table_grants=tables,
            column_grants=columns,
        )
        assert effective.table("erp.orders") is not None
        assert effective.table("erp.orders").column("total") is not None

    def test_it_does_not_resolve_for_a_role_the_caller_lacks(self):
        tables, columns = preset_grants(
            get_preset("analyst"), data_source_id="wh", role="analyst", tables=CATALOG
        )
        effective = resolve_grants(
            data_source_id="wh",
            roles=["viewer"],
            table_grants=tables,
            column_grants=columns,
        )
        assert effective.is_empty

    def test_a_viewer_sees_the_table_but_cannot_write_it(self):
        tables, columns = preset_grants(
            get_preset("viewer"), data_source_id="wh", role="viewer", tables=CATALOG
        )
        effective = resolve_grants(
            data_source_id="wh",
            roles=["viewer"],
            table_grants=tables,
            column_grants=columns,
        )
        orders = effective.table("erp.orders")
        assert orders is not None and orders.can_select
        assert not orders.can_update and not orders.can_delete


class TestApplyingThroughAStore:
    @pytest.fixture
    def store(self):
        from vanna.integrations.local import MemoryGrantStore

        return MemoryGrantStore()

    @pytest.fixture
    def context(self, tool_context):
        return tool_context("acme", "ada@acme.example")

    async def _apply(self, store, context, *, role="analyst", mode="fill"):
        tables, columns = preset_grants(
            get_preset(role), data_source_id="wh", role=role, tables=CATALOG
        )
        return await store.apply_preset(
            context,
            data_source_id="wh",
            role=role,
            table_grants=tables,
            column_grants=columns,
            mode=mode,
        )

    async def test_applying_twice_adds_nothing_the_second_time(self, store, context):
        first = await self._apply(store, context)
        assert first["tables"] == 2

        second = await self._apply(store, context)
        assert second["tables"] == 0 and second["columns"] == 0

    async def test_fill_leaves_a_withheld_column_alone(self, store, context):
        """The property that makes re-applying safe.

        An administrator who withheld a column does not get it back because
        somebody re-ran the preset.
        """
        await store.set_column_grant(
            context,
            ColumnGrant(
                data_source_id="wh",
                role="analyst",
                table="erp.customers",
                column="email",
                can_read=False,
            ),
        )
        await self._apply(store, context)

        effective = await store.resolve(context, data_source_id="wh", roles=["analyst"])
        customers = effective.table("erp.customers")
        assert customers is None or customers.column("email") is None, (
            "a deliberately withheld column came back"
        )

    async def test_applying_moves_the_version_once(self, store, context):
        before = await store.version(context, data_source_id="wh")
        await self._apply(store, context)
        after = await store.version(context, data_source_id="wh")
        assert after > before, (
            "an approved-but-unexecuted write is re-checked against this version"
        )


class TestTheRoleIsAGroup:
    """`identity._groups_for`, and the consumers checked before changing it."""

    def _groups(self, role: str, platform_admin: bool = False):
        from vanna_app.identity import _groups_for

        return _groups_for(role, platform_admin)

    @pytest.mark.parametrize("role", ["admin", "analyst", "viewer"])
    def test_the_workspace_role_is_present(self, role: str):
        assert role in self._groups(role)

    def test_user_is_always_present(self):
        """Grants written before this existed used `user`; dropping it would
        revoke them."""
        for role in ("admin", "analyst", "viewer"):
            assert "user" in self._groups(role)

    def test_admin_is_not_duplicated(self):
        assert self._groups("admin") == ["user", "admin"]
        assert self._groups("admin", True) == ["user", "admin"]

    def test_a_platform_admin_is_an_admin_in_any_workspace(self):
        assert "admin" in self._groups("viewer", True)

    def test_a_viewer_is_not_an_admin(self):
        assert "admin" not in self._groups("viewer")
        assert "admin" not in self._groups("analyst")

    def test_a_grant_for_analyst_now_reaches_an_analyst(self):
        """The bug, stated as the behaviour it broke."""
        tables, columns = preset_grants(
            get_preset("analyst"), data_source_id="wh", role="analyst", tables=CATALOG
        )
        effective = resolve_grants(
            data_source_id="wh",
            roles=self._groups("analyst"),
            table_grants=tables,
            column_grants=columns,
        )
        assert not effective.is_empty, (
            "an analyst's groups must match a grant written for 'analyst'"
        )

    def test_quota_exemptions_are_unaffected(self):
        """One of the four consumers of `group_memberships` that had to be checked.

        Both limit hooks are constructed with `exempt_groups=()`, so adding a
        role name to the group list cannot exempt anybody from a quota.
        """
        import inspect

        from vanna_app import limits

        source = inspect.getsource(limits.build_limit_hooks)
        assert source.count("exempt_groups=()") == 2, (
            "a limit hook gained an exemption list; adding the role to "
            "group_memberships could now grant free quota"
        )

    def test_write_tools_are_still_admin_only(self):
        """The consumer where widening would matter most."""
        import inspect

        from vanna_app import platform

        source = inspect.getsource(platform.Platform._build_runtime)
        assert 'register_local_tool(tool, ["admin"])' in source, (
            "write tools are no longer admin-only; an analyst can now reach them"
        )
