"""Library store interfaces, backed by the control plane.

The library's stores are file-backed -- markdown knowledge, a JSON catalog, a JSONL
generation log. That is the right default for one container playing with a demo
database. It stops being right the moment there is more than one replica: two
workers writing the same JSONL interleave, and a conversation held in memory is lost
on every deploy.

Each class here implements the same contract as its local counterpart, so the agent
is unchanged and the swap happens once, in ``wiring``.

Writes on the request path never propagate a failure. An analytics write that can
kill a user's answer has inverted its own cost/benefit -- so every method degrades
to a no-op and logs.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .db import SCHEMA
from .tenancy import _iso

logger = logging.getLogger("vanna.stores")


# ----------------------------------------------------------------------
# Generations
# ----------------------------------------------------------------------


class PostgresGenerationStore:
    """``GenerationStore`` over the control plane.

    Shared across replicas and queryable, which is what turns "we log generations"
    into an actual history view, a feedback loop, and per-tenant cost.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _tenant(context: Any) -> str:
        return getattr(context, "tenant_id", None) or "default"

    @staticmethod
    def _to_model(row: Dict[str, Any]) -> Any:
        from vanna.core.generation import SqlGeneration

        return SqlGeneration(
            id=row["id"],
            tenant_id=row["tenant_id"],
            data_source_id=row["data_source_id"],
            user_id=row["user_id"],
            conversation_id=row["conversation_id"],
            request_id=row["request_id"],
            question=row["question"],
            sql=row["sql"],
            status=row["status"],
            error=row["error"],
            error_kind=row["error_kind"],
            row_count=row["row_count"],
            truncated=row["truncated"],
            execution_ms=row["execution_ms"],
            repair_attempts=row["repair_attempts"],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cost_usd=row["cost_usd"],
            retrieved_example_ids=row["retrieved_example_ids"] or [],
            retrieved_table_names=row["retrieved_table_names"] or [],
            retrieval_strategy=row["retrieval_strategy"],
            feedback=row["feedback"],
            feedback_comment=row["feedback_comment"],
            created_at=row["created_at"],
            metadata=row["metadata"] or {},
        )

    async def record(self, context: Any, generation: Any) -> Any:
        generation.tenant_id = self._tenant(context)
        if not generation.user_id:
            generation.user_id = getattr(getattr(context, "user", None), "id", "") or ""
        if not generation.conversation_id:
            generation.conversation_id = getattr(context, "conversation_id", "") or ""
        if not generation.request_id:
            generation.request_id = getattr(context, "request_id", "") or ""

        try:
            await self.db.execute(
                f"""INSERT INTO {SCHEMA}.generations (
                        id, tenant_id, data_source_id, user_id, conversation_id,
                        request_id, question, sql, status, error, error_kind,
                        row_count, truncated, execution_ms, repair_attempts,
                        model, prompt_tokens, completion_tokens, cost_usd,
                        retrieved_example_ids, retrieved_table_names,
                        retrieval_strategy, feedback, feedback_comment,
                        created_at, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING""",
                (
                    generation.id,
                    generation.tenant_id,
                    generation.data_source_id,
                    generation.user_id,
                    generation.conversation_id,
                    generation.request_id,
                    generation.question,
                    generation.sql,
                    str(getattr(generation.status, "value", generation.status)),
                    generation.error,
                    generation.error_kind,
                    generation.row_count,
                    generation.truncated,
                    generation.execution_ms,
                    generation.repair_attempts,
                    generation.model,
                    generation.prompt_tokens,
                    generation.completion_tokens,
                    generation.cost_usd,
                    json.dumps(list(generation.retrieved_example_ids or [])),
                    json.dumps(list(generation.retrieved_table_names or [])),
                    generation.retrieval_strategy,
                    getattr(generation.feedback, "value", generation.feedback),
                    generation.feedback_comment,
                    generation.created_at,
                    json.dumps(generation.metadata or {}, default=str),
                ),
            )
        except Exception as exc:
            logger.warning("Could not record generation: %s", exc)
        return generation

    async def attach_usage(
        self,
        request_id: str,
        tenant_id: str,
        *,
        model: Optional[str],
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        cost_usd: Optional[float],
    ) -> int:
        """Fill in the model and token counts once the LLM turn is complete.

        The generation row is written by the ``run_sql`` tool, which knows the SQL
        and its outcome but not what the model cost -- that is only known after the
        response completes, several layers away. This closes the loop, and is why
        the four columns are no longer always NULL.
        """
        try:
            return await self.db.execute(
                f"""UPDATE {SCHEMA}.generations
                       SET model             = coalesce(%s, model),
                           prompt_tokens     = coalesce(%s, prompt_tokens),
                           completion_tokens = coalesce(%s, completion_tokens),
                           cost_usd          = coalesce(%s, cost_usd)
                     WHERE tenant_id = %s AND request_id = %s""",
                (model, prompt_tokens, completion_tokens, cost_usd, tenant_id, request_id),
            )
        except Exception as exc:
            logger.debug("Could not attach usage to %s: %s", request_id, exc)
            return 0

    async def get(self, context: Any, generation_id: str) -> Any:
        row = await self.db.fetch_one(
            f"SELECT * FROM {SCHEMA}.generations WHERE tenant_id = %s AND id = %s",
            (self._tenant(context), generation_id),
        )
        return self._to_model(row) if row else None

    async def find_by_request(self, context: Any, request_id: str) -> List[Any]:
        rows = await self.db.fetch_all(
            f"""SELECT * FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND request_id = %s
                 ORDER BY created_at""",
            (self._tenant(context), request_id),
        )
        return [self._to_model(row) for row in rows]

    async def set_feedback(
        self, context: Any, request_id: str, feedback: Any, comment: Optional[str] = None
    ) -> int:
        """Rate a turn -- only the turn's own author may.

        Scoped to the caller, not just the workspace. ``GET /history`` returns every
        member's ``request_id`` to every member, so a tenant-only predicate let any
        member rate anybody's turn -- and a rating is not inert: a positive one
        makes the turn a candidate example, which then steers the model for the
        whole workspace.
        """
        owner = getattr(getattr(context, "user", None), "id", None)
        try:
            return await self.db.execute(
                f"""UPDATE {SCHEMA}.generations
                       SET feedback = %s, feedback_comment = %s
                     WHERE tenant_id = %s AND request_id = %s
                       AND (%s IS NULL OR user_id = %s)""",
                (
                    getattr(feedback, "value", feedback),
                    comment,
                    self._tenant(context),
                    request_id,
                    owner,
                    owner,
                ),
            )
        except Exception as exc:
            logger.warning("Could not record feedback: %s", exc)
            return 0

    async def list_recent(
        self,
        context: Any,
        *,
        limit: int = 100,
        status: Any = None,
        since: Optional[datetime] = None,
    ) -> List[Any]:
        sql = f"SELECT * FROM {SCHEMA}.generations WHERE tenant_id = %s"
        params: List[Any] = [self._tenant(context)]
        if status is not None:
            sql += " AND status = %s"
            params.append(getattr(status, "value", status))
        if since is not None:
            sql += " AND created_at >= %s"
            params.append(since)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        return [self._to_model(row) for row in await self.db.fetch_all(sql, params)]

    async def stats(self, context: Any, *, since: Optional[datetime] = None) -> Any:
        from vanna.core.generation import GenerationStats

        sql = f"""
            SELECT count(*)                                              AS total,
                   count(*) FILTER (WHERE status = 'valid')              AS valid,
                   count(*) FILTER (WHERE status = 'invalid')            AS invalid,
                   count(*) FILTER (WHERE status = 'empty')              AS empty,
                   count(*) FILTER (WHERE status = 'rejected_by_policy') AS rejected,
                   count(*) FILTER (WHERE status = 'timeout')            AS timed_out,
                   count(*) FILTER (WHERE feedback = 'positive')         AS positive,
                   count(*) FILTER (WHERE feedback = 'negative')         AS negative,
                   count(*) FILTER (WHERE repair_attempts > 0)           AS repaired,
                   coalesce(sum(cost_usd), 0)                            AS cost,
                   coalesce(avg(execution_ms), 0)                        AS avg_ms
              FROM {SCHEMA}.generations
             WHERE tenant_id = %s"""
        params: List[Any] = [self._tenant(context)]
        if since is not None:
            sql += " AND created_at >= %s"
            params.append(since)

        row = await self.db.fetch_one(sql, params) or {}
        total = int(row.get("total") or 0)
        return GenerationStats(
            total=total,
            valid=int(row.get("valid") or 0),
            invalid=int(row.get("invalid") or 0),
            empty=int(row.get("empty") or 0),
            rejected_by_policy=int(row.get("rejected") or 0),
            timeout=int(row.get("timed_out") or 0),
            positive_feedback=int(row.get("positive") or 0),
            negative_feedback=int(row.get("negative") or 0),
            total_cost_usd=float(row.get("cost") or 0.0),
            avg_execution_ms=float(row.get("avg_ms") or 0.0),
            repair_rate=(int(row.get("repaired") or 0) / total) if total else 0.0,
        )

    async def promotable(self, context: Any, *, limit: int = 50) -> List[Any]:
        rows = await self.db.fetch_all(
            f"""SELECT * FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND status = 'valid'
                   AND feedback = 'positive' AND length(btrim(sql)) > 0
                 ORDER BY created_at DESC
                 LIMIT %s""",
            (self._tenant(context), limit),
        )
        return [self._to_model(row) for row in rows]

    # -- portal helpers ------------------------------------------------

    async def history(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """History rows shaped for the UI, with server-side filtering.

        Every filter is applied here rather than in the browser: the page can only
        narrow what it has already fetched, and "the failures from last Tuesday" is
        not reachable by paging through a workspace's whole history a page at a
        time.

        The ILIKE predicates are backed by trigram indexes (migration 0005); without
        them this was a sequential scan of the largest table in the system on every
        keystroke.
        """
        sql = f"""SELECT id, question, sql, status, error, row_count, execution_ms,
                         feedback, user_id, conversation_id, request_id, model,
                         cost_usd, created_at
                    FROM {SCHEMA}.generations WHERE tenant_id = %s"""
        params: List[Any] = [tenant_id]
        if user_id:
            sql += " AND user_id = %s"
            params.append(user_id)
        if search:
            sql += " AND (question ILIKE %s OR sql ILIKE %s)"
            params.extend([f"%{search}%", f"%{search}%"])
        if status:
            sql += " AND status = %s"
            params.append(status)
        if since is not None:
            sql += " AND created_at >= %s"
            params.append(since)
        if until is not None:
            sql += " AND created_at < %s"
            params.append(until)
        sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.append(min(max(limit, 1), 500))
        params.append(max(offset, 0))

        rows = await self.db.fetch_all(sql, params)
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
        return rows

    async def delete_history_row(
        self, tenant_id: str, user_id: str, generation_id: str
    ) -> int:
        """Forget one of the caller's own turns.

        Scoped to ``user_id`` as well as the tenant on purpose. History is shared
        for reading -- seeing what a colleague already asked is the point of it --
        but that does not make it shared for deleting, and the ids of everyone
        else's rows are in the same response the page renders.
        """
        return await self.db.execute(
            f"""DELETE FROM {SCHEMA}.generations
                 WHERE tenant_id = %s AND user_id = %s AND id = %s""",
            (tenant_id, user_id, generation_id),
        )

    async def delete_history(self, tenant_id: str, user_id: str) -> int:
        """Forget all of the caller's own turns. Returns how many went.

        Deliberately not an admin-wide purge: that is what
        ``VANNA_GENERATION_RETENTION_DAYS`` is for. The audit trail
        (``PostgresAuditLogger``) is a separate table and is untouched, so what an
        admin needs for accountability survives a user clearing their own history.
        """
        return await self.db.execute(
            f"DELETE FROM {SCHEMA}.generations WHERE tenant_id = %s AND user_id = %s",
            (tenant_id, user_id),
        )

    async def purge_older_than(self, days: int) -> int:
        """Retention. Zero means keep everything.

        Question text is customer data and this table never stopped growing, so a
        deployment that has been running a year holds every question anybody asked
        with no policy saying it should.
        """
        if days <= 0:
            return 0
        removed = await self.db.execute(
            f"DELETE FROM {SCHEMA}.generations WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        )
        if removed:
            logger.info("Retention: removed %d generation(s) older than %d days", removed, days)
        return removed


# ----------------------------------------------------------------------
# Conversations
# ----------------------------------------------------------------------

#: Longest auto-generated thread title -- long enough to tell two questions apart in
#: a sidebar, short enough not to wrap.
_TITLE_LENGTH = 60

#: What an untitled thread is called. Recognised on write so a conversation saved
#: before its first question still gets a real title later.
PLACEHOLDER_TITLE = "New conversation"

#: Messages kept in a stored thread. The whole document is rewritten on every turn,
#: so an unbounded transcript is quadratic write amplification -- 500 messages means
#: re-serialising 500 messages to append one.
MAX_STORED_MESSAGES = 200


def title_from(text: str) -> str:
    """A thread title derived from its first question."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return PLACEHOLDER_TITLE
    if len(cleaned) <= _TITLE_LENGTH:
        return cleaned
    return cleaned[: _TITLE_LENGTH - 1].rstrip() + "…"


class PostgresConversationStore:
    """``ConversationStore`` over the control plane.

    Threads survive a restart, which the in-memory store it replaces was the only
    reason they did not. Ownership is enforced on **read** as well as write, so one
    person's thread id is useless to another even inside the same workspace.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _tenant(user: Any) -> str:
        return getattr(user, "tenant_id", None) or "default"

    @staticmethod
    def _to_model(row: Dict[str, Any]) -> Any:
        from vanna.core.storage import Conversation

        return Conversation.model_validate(row["document"])

    async def _write(self, conversation: Any) -> None:
        # The agent writes the conversation once before the user's message is
        # attached, so latching the title on the first write would leave every
        # thread called "New conversation" forever. The placeholder is treated as
        # "not titled yet" and recomputed until a real question exists; a title
        # somebody typed is never overwritten.
        existing = (conversation.metadata or {}).get("title") or ""
        first_question = next(
            (m.content for m in conversation.messages if m.role == "user" and m.content), ""
        )
        title = (
            existing
            if existing and existing != PLACEHOLDER_TITLE
            else title_from(first_question)
        )
        conversation.metadata["title"] = title

        document = conversation.model_dump(mode="json")
        document["metadata"] = conversation.metadata

        messages = document.get("messages") or []
        if len(messages) > MAX_STORED_MESSAGES:
            # Keep the head, which carries the original question and any early
            # context, and the tail, which is the live conversation. Dropping the
            # middle is visible and bounded; dropping the head loses the thread's
            # subject and dropping the tail loses what the person is doing now.
            head, tail = messages[:20], messages[-(MAX_STORED_MESSAGES - 21):]
            document["messages"] = head + [
                {
                    "role": "system",
                    "content": f"[{len(messages) - len(head) - len(tail)} earlier "
                               "messages were trimmed from this stored transcript]",
                    "timestamp": messages[len(head)].get("timestamp"),
                }
            ] + tail

        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.conversations (id, tenant_id, user_id, title, document)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET title = EXCLUDED.title,
                        document = EXCLUDED.document,
                        updated_at = now(),
                        -- Claims a placeholder row for its real owner.
                        user_id = EXCLUDED.user_id
                 WHERE {SCHEMA}.conversations.tenant_id = EXCLUDED.tenant_id
                   -- '' is the placeholder `bind_data_source` used to write before
                   -- it recorded an owner. Adopting such a row rather than
                   -- rejecting it lets a thread orphaned by that bug recover on
                   -- its next turn instead of staying unreadable forever.
                   AND {SCHEMA}.conversations.user_id IN ('', EXCLUDED.user_id)""",
            (
                conversation.id,
                self._tenant(conversation.user),
                conversation.user.id,
                title,
                json.dumps(document, default=str),
            ),
        )

    async def create_conversation(self, conversation_id: str, user: Any, initial_message: str) -> Any:
        from vanna.core.storage import Conversation, Message

        conversation = Conversation(
            id=conversation_id,
            user=user,
            messages=[Message(role="user", content=initial_message)],
            metadata={"title": title_from(initial_message)},
        )
        await self._write(conversation)
        return conversation

    async def get_conversation(self, conversation_id: str, user: Any) -> Any:
        row = await self.db.fetch_one(
            f"""SELECT document FROM {SCHEMA}.conversations
                 WHERE id = %s AND tenant_id = %s AND user_id = %s""",
            (conversation_id, self._tenant(user), user.id),
        )
        if not row:
            return None

        # A row `bind_data_source` created but the agent has not written yet holds
        # `{}`, which is not a `Conversation`. Report it as absent -- which is what
        # it is: the thread is pinned to a database and has no transcript.
        #
        # Not defensive decoration. The agent calls this at the start of every turn
        # (`agent/agent.py:446`) and validates the result, so returning the
        # placeholder raised a ValidationError *inside the chat request* and the
        # answer failed. Returning None makes the agent start a fresh conversation,
        # and `_write` then adopts this row.
        document = row.get("document") or {}
        if not document.get("id"):
            return None
        return self._to_model(row)

    async def update_conversation(self, conversation: Any) -> None:
        # The chat widget requests its starter UI with an empty message on every
        # mount, which reaches the agent as a turn and would persist a conversation
        # nobody started -- one untitled empty thread per page load.
        if not any(
            m.role == "user" and (m.content or "").strip() for m in conversation.messages
        ):
            return
        try:
            await self._write(conversation)
        except Exception as exc:
            # On the request path. Losing a transcript is bad; failing the answer
            # the person is waiting for is worse.
            logger.warning("Could not persist conversation %s: %s", conversation.id, exc)

    async def delete_conversation(self, conversation_id: str, user: Any) -> bool:
        return bool(
            await self.db.execute(
                f"""DELETE FROM {SCHEMA}.conversations
                     WHERE id = %s AND tenant_id = %s AND user_id = %s""",
                (conversation_id, self._tenant(user), user.id),
            )
        )

    async def list_conversations(self, user: Any, limit: int = 50, offset: int = 0) -> List[Any]:
        rows = await self.db.fetch_all(
            f"""SELECT document FROM {SCHEMA}.conversations
                 WHERE tenant_id = %s AND user_id = %s
                 ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
            (self._tenant(user), user.id, limit, offset),
        )
        return [self._to_model(row) for row in rows]

    # -- portal helpers ------------------------------------------------

    async def bind_data_source(
        self, tenant_id: str, conversation_id: str, data_source_id: str, user_id: str
    ) -> str:
        """Pin a thread to one database, first writer wins. Returns the binding.

        The chat component generates its own conversation id and there is no
        "create conversation" call to hang this on, so the binding is claimed by
        the first message of a thread and is immutable afterwards. That is what
        makes it a binding rather than a per-request parameter: a later message
        naming a different database gets the one the thread already has, and the
        client cannot move a conversation between schemas by editing a field.
        (It also cannot *widen* anything -- the caller was authorized against the
        registry before this is reached.)

        Returns whatever the thread is bound to, which is not necessarily what was
        asked for. Callers should use the return value, not their own argument.

        ``user_id`` is not optional bookkeeping. This inserts the row before the
        agent has written anything, and it used to insert ``user_id = ''`` -- so
        ``_write``'s conflict predicate (which requires the owner to match) was
        false for the rest of the thread's life and every transcript update was
        silently discarded. The row was also invisible to ``summaries``,
        ``get_conversation`` and ``delete_conversation``, all of which filter on
        the owner: a conversation nobody could read, list or delete. Since the
        chat client sends a data source on the first message of every new thread
        once a workspace has more than one database, that was every conversation.
        """
        row = await self.db.fetch_one(
            f"""
            INSERT INTO {SCHEMA}.conversations
                (id, tenant_id, user_id, title, document, data_source_id)
            VALUES (%s, %s, %s, '', '{{}}'::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET
                -- COALESCE, not EXCLUDED: an existing binding wins, and a thread
                -- that predates this column adopts the first one it is given.
                data_source_id = COALESCE(
                    {SCHEMA}.conversations.data_source_id, EXCLUDED.data_source_id
                )
            RETURNING data_source_id
            """,
            (conversation_id, tenant_id, user_id, data_source_id),
        )
        return (row or {}).get("data_source_id") or data_source_id

    async def data_source_of(
        self, tenant_id: str, conversation_id: str
    ) -> Optional[str]:
        """What this thread is bound to, or None for the workspace default."""
        row = await self.db.fetch_one(
            f"SELECT data_source_id FROM {SCHEMA}.conversations "
            "WHERE id = %s AND tenant_id = %s",
            (conversation_id, tenant_id),
        )
        return (row or {}).get("data_source_id")

    async def summaries(
        self, tenant_id: str, user_id: str, limit: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Titles and timestamps only -- what the sidebar needs.

        Deliberately does not deserialise the documents: a thread list should not
        pay for every message in every thread.
        """
        rows = await self.db.fetch_all(
            f"""SELECT id, title, created_at, updated_at, data_source_id,
                       jsonb_array_length(document->'messages') AS message_count
                  FROM {SCHEMA}.conversations
                 WHERE tenant_id = %s AND user_id = %s
                   -- Skip a thread pinned to a database but never written. It has
                   -- nothing to show, and `update_conversation` already refuses to
                   -- persist an empty transcript for the same reason: the chat
                   -- widget requests its starter UI on every mount, which would
                   -- otherwise leave one untitled empty thread per page load.
                   AND document ? 'id'
                 ORDER BY updated_at DESC LIMIT %s OFFSET %s""",
            (tenant_id, user_id, limit, offset),
        )
        for row in rows:
            row["created_at"] = _iso(row["created_at"])
            row["updated_at"] = _iso(row["updated_at"])
        return rows

    async def rename(
        self, tenant_id: str, user_id: str, conversation_id: str, title: str
    ) -> bool:
        clean = title_from(title)
        return bool(
            await self.db.execute(
                f"""UPDATE {SCHEMA}.conversations
                       SET title = %s,
                           document = jsonb_set(
                               document, '{{metadata,title}}', to_jsonb(%s::text), true
                           ),
                           updated_at = now()
                     WHERE id = %s AND tenant_id = %s AND user_id = %s""",
                (clean, clean, conversation_id, tenant_id, user_id),
            )
        )


# ----------------------------------------------------------------------
# Dashboards
# ----------------------------------------------------------------------


class PostgresDashboardStore:
    """``DashboardStore`` over the control plane.

    The agent's ``save_dashboard`` tool and the portal's Dashboards tab must write
    and read the *same* rows -- otherwise a dashboard the agent reports creating
    never appears in the list, which is exactly what happened when the tool was
    registered against a store nobody was reading.
    """

    def __init__(self, directory: Any) -> None:
        self.directory = directory

    async def list(self, tenant_id: str) -> List[Any]:
        from vanna.dashboards import Dashboard

        rows = await self.directory.list_dashboards(tenant_id)
        return [Dashboard.model_validate(row["document"]) for row in rows]

    async def get(self, tenant_id: str, dashboard_id: str) -> Any:
        from vanna.dashboards import Dashboard

        row = await self.directory.get_dashboard(tenant_id, dashboard_id)
        return Dashboard.model_validate(row["document"]) if row else None

    async def save(self, dashboard: Any) -> Any:
        from vanna.dashboards.store import validated

        dashboard = validated(dashboard)
        await self.directory.save_dashboard(
            dashboard.tenant_id,
            dashboard.to_json_dict(),
            created_by=dashboard.created_by,
        )
        return dashboard

    async def delete(self, tenant_id: str, dashboard_id: str) -> bool:
        return await self.directory.delete_dashboard(tenant_id, dashboard_id)
