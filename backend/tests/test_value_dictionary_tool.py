"""list_known_values: the approved value dictionary, read for free.

Three properties, each with a direct failure mode if it regresses:

* A `PENDING` sample must never appear -- that is the entire reason the
  review store exists (`ValueStore.dictionary_for` assembles from
  `ReviewStatus.APPROVED` only; this tool must never call `list_samples`
  without a status filter).
* A revoked column's dictionary must be withheld even though it was sampled
  and approved before the revocation.
* Cross-tenant isolation, like every other store-backed tool here.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from vanna.capabilities.values import ReviewStatus, SampledValue, ValueSynonym
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.values import MemoryValueStore
from vanna.tools.value_dictionary import ListKnownValuesArgs, ListKnownValuesTool


class _NoColumnUsesCatalog:
    """No `column_uses` at all -- unenforced, everything must stay visible."""


class _EnforcingCatalog:
    """`column_uses` returns a fixed grant map; anything absent is denied."""

    def __init__(self, uses: Dict[str, Dict[str, set]]):
        self._uses = uses

    async def column_uses(self, context: Any) -> Optional[Dict[str, Dict[str, set]]]:
        return self._uses


def _context(tenant: str = "acme", user_id: str = "u1") -> ToolContext:
    return ToolContext(
        user=User(id=user_id, email=f"{user_id}@{tenant}.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


class TestPendingNeverAppears:
    async def test_pending_value_is_absent_from_the_column_view(self):
        store = MemoryValueStore()
        ctx = _context()
        await store.record_samples(
            ctx,
            [
                SampledValue(table="orders", column="status", value="C"),
                SampledValue(table="orders", column="status", value="A"),
            ],
        )
        await store.set_status(
            ctx, table="orders", column="status", values=["A"],
            status=ReviewStatus.APPROVED,
        )
        # "C" stays PENDING.
        tool = ListKnownValuesTool(store, _NoColumnUsesCatalog())

        result = await tool.execute(
            ctx, ListKnownValuesArgs(table="orders", column="status")
        )

        assert "'A'" in result.result_for_llm
        assert "'C'" not in result.result_for_llm

    async def test_pending_only_column_is_absent_from_the_inventory(self):
        store = MemoryValueStore()
        ctx = _context()
        await store.record_samples(
            ctx, [SampledValue(table="orders", column="status", value="C")]
        )
        tool = ListKnownValuesTool(store, _NoColumnUsesCatalog())

        result = await tool.execute(ctx, ListKnownValuesArgs())

        assert "orders.status" not in result.result_for_llm


class TestLabelsAndSynonyms:
    async def test_a_coded_value_shows_its_label(self):
        store = MemoryValueStore(auto_approve=True)
        ctx = _context()
        await store.record_samples(
            ctx, [SampledValue(table="orders", column="status", value="C")]
        )
        await store.set_synonym(
            ctx,
            ValueSynonym(
                table="orders", column="status", value="C", label="Cancelled"
            ),
        )
        tool = ListKnownValuesTool(store, _NoColumnUsesCatalog())

        result = await tool.execute(
            ctx, ListKnownValuesArgs(table="orders", column="status")
        )

        assert "Cancelled" in result.result_for_llm


class TestGrantAwareness:
    async def test_revoked_column_withholds_its_dictionary(self):
        store = MemoryValueStore(auto_approve=True)
        ctx = _context()
        await store.record_samples(
            ctx, [SampledValue(table="orders", column="status", value="A")]
        )
        # Enforced, and `orders.status` is not in the grant -- revoked.
        catalog = _EnforcingCatalog({"orders": {"total": {"read"}}})
        tool = ListKnownValuesTool(store, catalog)

        result = await tool.execute(
            ctx, ListKnownValuesArgs(table="orders", column="status")
        )

        assert "'A'" not in result.result_for_llm
        assert "no longer have access" in result.result_for_llm

    async def test_unenforced_caller_sees_everything(self):
        """A catalog with no `column_uses` at all (the common case) must not
        withhold anything -- `None` means unenforced, not denied."""
        store = MemoryValueStore(auto_approve=True)
        ctx = _context()
        await store.record_samples(
            ctx, [SampledValue(table="orders", column="status", value="A")]
        )
        tool = ListKnownValuesTool(store, _NoColumnUsesCatalog())

        result = await tool.execute(
            ctx, ListKnownValuesArgs(table="orders", column="status")
        )

        assert "'A'" in result.result_for_llm


class TestCrossTenantIsolation:
    async def test_a_dictionary_never_crosses_tenants(self):
        store = MemoryValueStore(auto_approve=True)
        acme = _context(tenant="acme")
        globex = _context(tenant="globex")
        await store.record_samples(
            acme, [SampledValue(table="orders", column="status", value="ACME-ONLY")]
        )
        tool = ListKnownValuesTool(store, _NoColumnUsesCatalog())

        result = await tool.execute(
            globex, ListKnownValuesArgs(table="orders", column="status")
        )

        assert "ACME-ONLY" not in result.result_for_llm
