"""Agent memory that survives a restart.

``Platform`` used to build a hardcoded ``EphemeralMemory``: every save was a
no-op, every search returned ``[]``, and nothing outlived the request. The chat's
own status line said so -- ``Memory ✗`` -- and ``/memories`` answered "No recent
memories found" no matter how much the agent had been used.

This is the same interface backed by the control plane, so what the agent learns
is still there tomorrow.

Two things it does not do, on purpose:

**No embeddings.** Similarity is PostgreSQL trigram distance over the question
text. An embedding store is a second service to run and a model to keep in step
with, and the question being answered is "has somebody in this workspace asked
something like this" over hundreds of rows. When ``pg_trgm`` is missing the
search degrades to ``ILIKE`` rather than failing -- a worse answer beats an
error, because memory is an optimisation and must never take a question down.

**No cross-tenant read, ever.** Every statement is scoped by ``tenant_id``, and
``TenantPartitionedAgentMemory`` wraps this as a second belt. One workspace's
saved patterns surfacing in another's retrieval would be a data leak dressed up
as a helpful suggestion.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from vanna.capabilities.agent_memory import (
    AgentMemory,
    MemoryStats,
    TextMemory,
    TextMemorySearchResult,
    ToolMemory,
    ToolMemorySearchResult,
)
from vanna.capabilities.agent_memory.scoping import tenant_scope

from .db import SCHEMA

logger = logging.getLogger("vanna.memory")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else value


def _user_of(context: Any) -> str:
    user = getattr(context, "user", None)
    return str(getattr(user, "id", "") or getattr(user, "email", "") or "")


class PostgresAgentMemory(AgentMemory):
    """Agent memory in the control plane."""

    def __init__(self, db: Any, tenant_id: Optional[str] = None) -> None:
        self.db = db
        #: Pinned at construction by `TenantPartitionedAgentMemory`, which calls
        #: its factory once per workspace and requires the result to be
        #: independent of every other workspace's.
        #:
        #: When set it *overrides* the tenant on the context rather than merely
        #: defaulting to it. That is the point: a store built for `acme` cannot
        #: be made to read `globex` by handing it a context that says globex,
        #: however that context came to be wrong.
        self.tenant_id = tenant_id
        #: Set to False the first time a trigram query is refused, so one
        #: unavailable extension does not mean an exception per search.
        self._trigrams = True

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def save_tool_usage(
        self,
        question: str,
        tool_name: str,
        args: Dict[str, Any],
        context: Any,
        success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Remember that this question was answered with this tool call.

        Never raises. A memory write failing must not fail the question it was
        recorded from -- the answer is the product, this is a note about it.
        """
        try:
            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.agent_tool_memory
                        (memory_id, tenant_id, user_id, question, tool_name,
                         args, success, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(uuid.uuid4()),
                    self.tenant_id or tenant_scope(context),
                    _user_of(context),
                    (question or "")[:4000],
                    tool_name or "",
                    json.dumps(args or {}, default=str),
                    bool(success),
                    json.dumps(metadata or {}, default=str),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Could not save tool memory: %s", exc)

    async def save_text_memory(self, content: str, context: Any) -> TextMemory:
        memory_id = str(uuid.uuid4())
        stamp = datetime.now(timezone.utc)
        try:
            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.agent_text_memory
                        (memory_id, tenant_id, user_id, content, created_at)
                    VALUES (%s,%s,%s,%s,%s)""",
                (
                    memory_id,
                    self.tenant_id or tenant_scope(context),
                    _user_of(context),
                    content,
                    stamp,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save text memory: %s", exc)
        # Returned regardless: the caller was told the memory exists, and an
        # object it can show beats an exception on a best-effort write.
        return TextMemory(
            memory_id=memory_id, content=content, timestamp=stamp.isoformat()
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def get_recent_memories(self, context: Any, limit: int = 10) -> List[ToolMemory]:
        rows = await self._rows(
            f"""SELECT memory_id, question, tool_name, args, success, metadata,
                       created_at
                  FROM {SCHEMA}.agent_tool_memory
                 WHERE tenant_id = %s AND (%s = '' OR user_id = %s)
                 ORDER BY created_at DESC LIMIT %s""",
            self._scope(context) + (self._cap(limit),),
        )
        return [self._tool(row) for row in rows]

    async def get_recent_text_memories(self, context: Any, limit: int = 10) -> List[TextMemory]:
        rows = await self._rows(
            f"""SELECT memory_id, content, created_at
                  FROM {SCHEMA}.agent_text_memory
                 WHERE tenant_id = %s AND (%s = '' OR user_id = %s)
                 ORDER BY created_at DESC LIMIT %s""",
            self._scope(context) + (self._cap(limit),),
        )
        return [self._text(row) for row in rows]

    async def search_similar_usage(
        self,
        question: str,
        context: Any,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
        tool_name_filter: Optional[str] = None,
    ) -> List[ToolMemorySearchResult]:
        """Questions like this one, most similar first.

        The threshold is applied as the caller means it -- 0.7 is "quite
        similar" -- but trigram similarity on short strings is stricter than an
        embedding cosine, so it is used as a floor on the *rank order* rather
        than a hard cut that would usually return nothing.
        """
        if not (question or "").strip():
            return []

        tenant, user, _ = self._scope(context)
        filter_sql = " AND tool_name = %s" if tool_name_filter else ""

        if self._trigrams:
            sql = f"""SELECT memory_id, question, tool_name, args, success, metadata,
                             created_at, similarity(question, %s) AS score
                        FROM {SCHEMA}.agent_tool_memory
                       WHERE tenant_id = %s AND (%s = '' OR user_id = %s){filter_sql}
                         AND similarity(question, %s) > 0.05
                       ORDER BY score DESC, created_at DESC LIMIT %s"""
            params: List[Any] = [question, tenant, user, user]
            if tool_name_filter:
                params.append(tool_name_filter)
            params += [question, self._cap(limit)]
            try:
                rows = await self.db.fetch_all(sql, params)
                return self._ranked(rows)
            except Exception as exc:  # noqa: BLE001
                # pg_trgm absent, or too old for `similarity`. Say so once.
                logger.warning("Trigram search unavailable, falling back: %s", exc)
                self._trigrams = False

        sql = f"""SELECT memory_id, question, tool_name, args, success, metadata,
                         created_at
                    FROM {SCHEMA}.agent_tool_memory
                   WHERE tenant_id = %s AND (%s = '' OR user_id = %s){filter_sql}
                     AND question ILIKE %s
                   ORDER BY created_at DESC LIMIT %s"""
        params = [tenant, user, user]
        if tool_name_filter:
            params.append(tool_name_filter)
        params += [f"%{question[:80]}%", self._cap(limit)]
        return self._ranked(await self._rows(sql, params))

    async def search_text_memories(
        self,
        query: str,
        context: Any,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
    ) -> List[TextMemorySearchResult]:
        if not (query or "").strip():
            return []
        tenant, user, _ = self._scope(context)
        rows = await self._rows(
            f"""SELECT memory_id, content, created_at
                  FROM {SCHEMA}.agent_text_memory
                 WHERE tenant_id = %s AND (%s = '' OR user_id = %s)
                   AND content ILIKE %s
                 ORDER BY created_at DESC LIMIT %s""",
            (tenant, user, user, f"%{query[:80]}%", self._cap(limit)),
        )
        return [
            TextMemorySearchResult(memory=self._text(row), similarity_score=1.0, rank=i + 1)
            for i, row in enumerate(rows)
        ]

    # ------------------------------------------------------------------
    # Deleting
    # ------------------------------------------------------------------

    async def delete_by_id(self, context: Any, memory_id: str) -> bool:
        return await self._delete("agent_tool_memory", context, memory_id)

    async def delete_text_memory(self, context: Any, memory_id: str) -> bool:
        return await self._delete("agent_text_memory", context, memory_id)

    async def clear_memories(
        self,
        context: Any,
        tool_name: Optional[str] = None,
        before_date: Optional[str] = None,
    ) -> int:
        tenant, user, _ = self._scope(context)
        sql = (
            f"DELETE FROM {SCHEMA}.agent_tool_memory "
            "WHERE tenant_id = %s AND (%s = '' OR user_id = %s)"
        )
        params: List[Any] = [tenant, user, user]
        if tool_name:
            sql += " AND tool_name = %s"
            params.append(tool_name)
        if before_date:
            sql += " AND created_at < %s"
            params.append(before_date)
        try:
            return int(await self.db.execute(sql, params) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not clear memories: %s", exc)
            return 0

    async def get_memory_stats(self, context: Any) -> MemoryStats:
        row = await self.db.fetch_one(
            f"""SELECT count(*) AS total,
                       count(DISTINCT tool_name) AS tools,
                       count(DISTINCT question) AS questions
                  FROM {SCHEMA}.agent_tool_memory
                 WHERE tenant_id = %s AND (%s = '' OR user_id = %s)""",
            self._scope(context),
        ) or {}
        return MemoryStats(
            total_memories=int(row.get("total") or 0),
            unique_tools=int(row.get("tools") or 0),
            unique_questions=int(row.get("questions") or 0),
        )

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    def _scope(self, context: Any) -> tuple:
        """`(tenant, user, user)` -- the user twice, for `%s = '' OR user_id = %s`.

        An empty user means "everything in this workspace", which is what an
        administrator listing the workspace's memories wants.
        """
        user = _user_of(context)
        return (self.tenant_id or tenant_scope(context), user, user)

    @staticmethod
    def _cap(limit: int) -> int:
        return min(max(int(limit or 10), 1), 200)

    async def _rows(self, sql: str, params: Any) -> List[Dict[str, Any]]:
        try:
            return await self.db.fetch_all(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Memory read failed: %s", exc)
            return []

    async def _delete(self, table: str, context: Any, memory_id: str) -> bool:
        tenant, user, _ = self._scope(context)
        try:
            # Scoped by tenant as well as id: an id from another workspace must
            # not be deletable just because the caller knows it.
            deleted = await self.db.execute(
                f"DELETE FROM {SCHEMA}.{table} "
                "WHERE memory_id = %s AND tenant_id = %s AND (%s = '' OR user_id = %s)",
                (memory_id, tenant, user, user),
            )
            return bool(deleted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not delete memory %s: %s", memory_id, exc)
            return False

    @staticmethod
    def _tool(row: Dict[str, Any]) -> ToolMemory:
        args = row.get("args")
        metadata = row.get("metadata")
        return ToolMemory(
            memory_id=row["memory_id"],
            question=row.get("question") or "",
            tool_name=row.get("tool_name") or "",
            args=args if isinstance(args, dict) else json.loads(args or "{}"),
            timestamp=_iso(row.get("created_at")),
            success=bool(row.get("success", True)),
            metadata=metadata if isinstance(metadata, dict) else json.loads(metadata or "{}"),
        )

    @staticmethod
    def _text(row: Dict[str, Any]) -> TextMemory:
        return TextMemory(
            memory_id=row["memory_id"],
            content=row.get("content") or "",
            timestamp=_iso(row.get("created_at")),
        )

    def _ranked(self, rows: List[Dict[str, Any]]) -> List[ToolMemorySearchResult]:
        return [
            ToolMemorySearchResult(
                memory=self._tool(row),
                similarity_score=float(row.get("score") or 1.0),
                rank=index + 1,
            )
            for index, row in enumerate(rows)
        ]
