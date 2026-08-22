"""The baseline layer: what a workspace may and may not do to a platform rule.

The product ships rules that apply to every workspace. Some of them are the only
thing standing between a plausible answer and an honest one -- "only reference
tables that appear in the schema context" is not a preference -- and a workspace
that can delete those can quietly switch off the thing keeping its answers
truthful, with no error and no log line.

So the property under test is ownership, and it has two halves that are easy to
get half-right:

* a platform rule reaches every workspace, including one that has never been
  configured, and cannot be edited or deleted from inside a workspace;
* a workspace's own rules are entirely its own, and a rule it was *given* -- a
  copy from a starter pack -- becomes its own the moment it arrives.

The subtle one is :meth:`TestBaseline.test_editing_the_baseline_needs_no_backfill`.
Copying baseline rules into each tenant at provision time is the obvious
implementation, passes every other test here, and is wrong: editing a platform
rule would then leave a stale duplicate in every workspace that already had it.
That test fails the moment somebody "simplifies" the design that way.
"""

from __future__ import annotations

from typing import List

import pytest

from vanna.capabilities.knowledge import (
    Instruction,
    InstructionOrigin,
    InstructionScope,
    LayeredInstructionStore,
    LockedInstructionError,
    MemoryBaselineOverrideStore,
)
from vanna.integrations.local import LocalInstructionStore

SAFETY = "platform.schema-only"
STYLE = "platform.explicit-columns"


def _baseline() -> List[Instruction]:
    return [
        Instruction(
            id=SAFETY,
            text="Only reference tables in the schema context.",
            priority=1000,
            origin=InstructionOrigin.PLATFORM,
            locked=True,
        ),
        Instruction(
            id=STYLE,
            text="Prefer an explicit column list to SELECT *.",
            priority=500,
            origin=InstructionOrigin.PLATFORM,
            locked=True,
        ),
    ]


@pytest.fixture
def context(tool_context):
    return tool_context("acme", "ada@acme.example")


@pytest.fixture
def other_context(tool_context):
    return tool_context("globex", "bob@globex.example")


@pytest.fixture
def store(tmp_path):
    """A layered store over a real (JSON) tenant store, sharing one baseline."""
    rules = _baseline()
    return LayeredInstructionStore(
        LocalInstructionStore(str(tmp_path / "rules.json")),
        baseline=lambda: rules,
        disableable_ids=lambda: {STYLE},
        overrides=MemoryBaselineOverrideStore(),
    )


class TestBaseline:
    async def test_it_applies_to_a_workspace_that_has_nothing(self, store, context):
        """The point of a baseline: no configuration, still governed."""
        resolved = await store.resolve(context)
        assert [r.id for r in resolved] == [SAFETY, STYLE]

    async def test_it_outranks_a_workspace_rule(self, store, context):
        await store.add(context, Instruction(text="Ours.", priority=10))
        resolved = await store.resolve(context)
        assert resolved[0].id == SAFETY, "the highest-priority rule must come first"
        assert resolved[-1].text == "Ours."

    async def test_a_platform_rule_cannot_be_deleted(self, store, context):
        with pytest.raises(LockedInstructionError) as caught:
            await store.delete(context, SAFETY)
        assert caught.value.reason == "locked"

        # And it is still in force, which is the half a refusal alone would not prove.
        assert SAFETY in [r.id for r in await store.resolve(context)]

    async def test_a_platform_rule_cannot_be_edited(self, store, context):
        with pytest.raises(LockedInstructionError):
            await store.update(context, SAFETY, text="Reference whatever you like.")

        resolved = {r.id: r for r in await store.resolve(context)}
        assert resolved[SAFETY].text == "Only reference tables in the schema context."

    async def test_a_workspace_rule_cannot_claim_a_platform_id(self, store, context):
        with pytest.raises(ValueError):
            await store.add(context, Instruction(id=SAFETY, text="Impostor."))

    async def test_editing_the_baseline_needs_no_backfill(self, tmp_path, context):
        """A platform rule is merged at read time, never copied into a tenant.

        This is the test that fails if anybody re-implements the baseline by
        writing rows per workspace: a copy would leave the old wording behind in
        every workspace that had already taken it.
        """
        rules = _baseline()
        store = LayeredInstructionStore(
            LocalInstructionStore(str(tmp_path / "r.json")), baseline=lambda: rules
        )
        assert "schema context" in (await store.resolve(context))[0].text

        rules[0] = rules[0].model_copy(update={"text": "Reworded by the platform."})
        assert (await store.resolve(context))[0].text == "Reworded by the platform."


class TestSwitchingOffABaselineRule:
    async def test_a_disableable_rule_can_be_switched_off(self, store, context):
        assert await store.set_enabled(context, STYLE, False) is True

        resolved = [r.id for r in await store.resolve(context)]
        assert STYLE not in resolved
        assert SAFETY in resolved, "switching off one rule must not affect another"

    async def test_switching_it_off_is_reversible(self, store, context):
        await store.set_enabled(context, STYLE, False)
        await store.set_enabled(context, STYLE, True)
        assert STYLE in [r.id for r in await store.resolve(context)]

    async def test_it_stays_on_for_every_other_workspace(
        self, store, context, other_context
    ):
        """The assertion that catches an override written to the wrong scope.

        Without it, an override stored globally would pass every other test here
        while silently switching a rule off for every customer.
        """
        await store.set_enabled(context, STYLE, False)

        assert STYLE not in [r.id for r in await store.resolve(context)]
        assert STYLE in [r.id for r in await store.resolve(other_context)]

    async def test_a_safety_rule_refuses_to_be_switched_off(self, store, context):
        with pytest.raises(LockedInstructionError) as caught:
            await store.set_enabled(context, SAFETY, False)
        assert caught.value.reason == "not_disableable"
        assert SAFETY in [r.id for r in await store.resolve(context)]

    async def test_nothing_is_disableable_without_somewhere_to_record_it(
        self, tmp_path, context
    ):
        """A choice that cannot be persisted must not be offered.

        Otherwise the rule comes back on the next restart and the workspace is
        told, once, that it was switched off.
        """
        store = LayeredInstructionStore(
            LocalInstructionStore(str(tmp_path / "r.json")),
            baseline=_baseline,
            disableable_ids=lambda: {STYLE},
            overrides=None,
        )
        with pytest.raises(LockedInstructionError):
            await store.set_enabled(context, STYLE, False)

    async def test_a_switched_off_rule_is_still_listed(self, store, context):
        """`list_all` drives the admin screen, which has to show the off switch."""
        await store.set_enabled(context, STYLE, False)

        listed = {r.id: r for r in await store.list_all(context)}
        assert STYLE in listed
        assert listed[STYLE].enabled is False


class TestWorkspaceRules:
    async def test_they_are_created_edited_and_deleted_normally(self, store, context):
        created = await store.add(context, Instruction(text="Ours.", priority=5))

        edited = await store.update(context, created.id, text="Ours, reworded.")
        assert edited is not None and edited.text == "Ours, reworded."
        assert edited.id == created.id, "editing must not mint a new rule"

        assert await store.delete(context, created.id) is True
        assert created.id not in [r.id for r in await store.list_all(context)]

    async def test_editing_something_absent_reports_not_found(self, store, context):
        assert await store.update(context, "no-such-rule", text="x") is None

    async def test_one_workspace_cannot_see_another_s_rules(
        self, store, context, other_context
    ):
        await store.add(context, Instruction(text="Acme only."))
        assert "Acme only." not in [r.text for r in await store.list_all(other_context)]

    async def test_a_library_copy_is_owned_by_the_workspace(self, store, context):
        """A pack is a starting point, not a subscription."""
        copied = await store.add(
            context,
            Instruction(
                text="From a pack.",
                origin=InstructionOrigin.LIBRARY,
                source_pack="finance-conventions",
            ),
        )
        edited = await store.update(context, copied.id, text="From a pack, adjusted.")
        assert edited is not None
        assert await store.delete(context, copied.id) is True


class TestScope:
    async def test_a_table_scoped_baseline_rule_waits_for_its_table(
        self, tmp_path, context
    ):
        rules = [
            Instruction(
                id="platform.orders",
                text="Orders are immutable once shipped.",
                scope=InstructionScope.TABLE,
                scope_ref="orders",
                origin=InstructionOrigin.PLATFORM,
                locked=True,
            )
        ]
        store = LayeredInstructionStore(
            LocalInstructionStore(str(tmp_path / "r.json")), baseline=lambda: rules
        )

        assert await store.resolve(context, tables=["customers"]) == []
        # Qualified names match too, or the rule fires only on some callers.
        assert len(await store.resolve(context, tables=["public.orders"])) == 1
