"""Tenant isolation for agent memory.

Vanna's ``AgentMemory`` implementations receive a :class:`ToolContext` on every
call but historically ignored it, so a similarity search issued by one tenant
could return another tenant's saved SQL, table names, and filter values.

This module provides two complementary mechanisms:

``TenantPartitionedAgentMemory``
    A wrapper that gives each tenant its *own* backing store instance. Isolation
    comes from physical partitioning rather than a query predicate, so a backend
    that forgets to filter cannot leak: there is nothing to leak *from*. This
    works with every existing implementation without modifying it.

``tenant_scope`` / ``scoped_metadata``
    Helpers for backends that implement filtering natively (see
    ``integrations.chromadb``). Native filtering is cheaper at high tenant
    counts, where one collection per tenant becomes impractical.

Prefer partitioning unless tenant count is high enough that per-tenant
collections strain the vector store. The two can be combined -- partitioning
for isolation, metadata for attribution -- and the chromadb integration does
exactly that.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .base import AgentMemory

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

    from .models import (
        TextMemory,
        TextMemorySearchResult,
        ToolMemory,
        ToolMemorySearchResult,
    )


def tenant_scope(context: "ToolContext") -> str:
    """Return the tenant key for *context*.

    Falls back to the user's tenant when ``ToolContext.tenant_id`` was left at
    its default, which happens when a caller constructs a context by hand rather
    than going through :class:`~vanna.core.agent.Agent`.
    """
    tenant_id = getattr(context, "tenant_id", None)
    if tenant_id and tenant_id != "default":
        return str(tenant_id)
    user_tenant = getattr(context.user, "tenant_id", None)
    return str(user_tenant) if user_tenant else "default"


def scoped_metadata(context: "ToolContext") -> Dict[str, str]:
    """Return the ownership fields every memory record must carry.

    Backends that filter natively should merge this into each record's metadata
    on write and use :func:`tenant_scope` to build the read predicate.
    """
    return {"tenant_id": tenant_scope(context), "user_id": str(context.user.id)}


class TenantPartitionedAgentMemory(AgentMemory):
    """Route every operation to a per-tenant backing store.

    The *factory* is called once per tenant with that tenant's id and must
    return a fully independent ``AgentMemory`` -- a distinct collection, index,
    table, or directory. Returning a shared store defeats the purpose.

    Example::

        memory = TenantPartitionedAgentMemory(
            lambda tenant: ChromaAgentMemory(
                persist_directory="./memory",
                collection_name=f"memories_{tenant}",
            )
        )

    Instances are created lazily on first use and cached for the process
    lifetime. Creation is guarded by a lock so concurrent first requests for the
    same tenant share one instance rather than racing to build several.
    """

    def __init__(
        self,
        factory: Callable[[str], AgentMemory],
        *,
        max_cached_tenants: Optional[int] = None,
    ) -> None:
        """
        Args:
            factory: Builds a dedicated ``AgentMemory`` for a given tenant id.
            max_cached_tenants: Optional cap on cached instances. When exceeded,
                the least recently created instance is dropped. Leave unset for
                deployments with a bounded, known tenant count.
        """
        self._factory = factory
        self._stores: Dict[str, AgentMemory] = {}
        self._lock = asyncio.Lock()
        self._max_cached_tenants = max_cached_tenants

    async def _store_for(self, context: "ToolContext") -> AgentMemory:
        tenant = tenant_scope(context)
        store = self._stores.get(tenant)
        if store is not None:
            return store
        async with self._lock:
            # Re-check: another coroutine may have built it while we waited.
            store = self._stores.get(tenant)
            if store is None:
                store = self._factory(tenant)
                self._stores[tenant] = store
                self._evict_if_needed()
            return store

    def _evict_if_needed(self) -> None:
        if self._max_cached_tenants is None:
            return
        while len(self._stores) > self._max_cached_tenants:
            # dicts preserve insertion order, so the first key is the oldest.
            oldest = next(iter(self._stores))
            del self._stores[oldest]

    # ------------------------------------------------------------------
    # AgentMemory interface -- every method delegates to the tenant's store
    # ------------------------------------------------------------------

    async def save_tool_usage(
        self,
        question: str,
        tool_name: str,
        args: Dict[str, Any],
        context: "ToolContext",
        success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        store = await self._store_for(context)
        await store.save_tool_usage(
            question, tool_name, args, context, success, metadata
        )

    async def save_text_memory(
        self, content: str, context: "ToolContext"
    ) -> "TextMemory":
        store = await self._store_for(context)
        return await store.save_text_memory(content, context)

    async def search_similar_usage(
        self,
        question: str,
        context: "ToolContext",
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
        tool_name_filter: Optional[str] = None,
    ) -> List["ToolMemorySearchResult"]:
        store = await self._store_for(context)
        return await store.search_similar_usage(
            question,
            context,
            limit=limit,
            similarity_threshold=similarity_threshold,
            tool_name_filter=tool_name_filter,
        )

    async def search_text_memories(
        self,
        query: str,
        context: "ToolContext",
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
    ) -> List["TextMemorySearchResult"]:
        store = await self._store_for(context)
        return await store.search_text_memories(
            query, context, limit=limit, similarity_threshold=similarity_threshold
        )

    async def get_recent_memories(
        self, context: "ToolContext", limit: int = 10
    ) -> List["ToolMemory"]:
        store = await self._store_for(context)
        return await store.get_recent_memories(context, limit)

    async def get_recent_text_memories(
        self, context: "ToolContext", limit: int = 10
    ) -> List["TextMemory"]:
        store = await self._store_for(context)
        return await store.get_recent_text_memories(context, limit)

    async def delete_by_id(self, context: "ToolContext", memory_id: str) -> bool:
        store = await self._store_for(context)
        return await store.delete_by_id(context, memory_id)

    async def delete_text_memory(self, context: "ToolContext", memory_id: str) -> bool:
        store = await self._store_for(context)
        return await store.delete_text_memory(context, memory_id)

    async def clear_memories(
        self,
        context: "ToolContext",
        tool_name: Optional[str] = None,
        before_date: Optional[str] = None,
    ) -> int:
        store = await self._store_for(context)
        return await store.clear_memories(context, tool_name, before_date)
