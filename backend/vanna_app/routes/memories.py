"""What the assistant remembers, as an API the person it remembers can use.

The memory store landed with no way in from outside the agent. It could be
written by the agent's own tools and read back by ``/memories`` typed into the
chat, and that was the whole surface: nothing could list what was stored, add a
fact deliberately, or take one back. A store that only a model can write and only
a chat message can read is not something a person can be asked to trust with
"info about the user".

So: three routes over the text side of the store, scoped to the caller.

**Text memories only.** ``agent_tool_memory`` is the agent's working record --
which tool answered which question -- and it is written automatically, at machine
pace, in a vocabulary nobody chose. Listing it here would bury the four facts
somebody wrote about themselves under four hundred rows they did not write. The
tool side stays reachable through ``/memories`` in the chat, where its audience
is somebody debugging an answer.

**The caller's own scope, always.** ``PostgresAgentMemory`` derives
``(tenant, user)`` from the ``ToolContext`` it is handed, and the context here is
built from the authenticated caller -- so there is no parameter by which one
person could read another's. That is not a check that can be forgotten; it is the
absence of a way to express the question.

**404, not 403, for a memory that is not yours.** The house rule everywhere in
this application: a refusal that distinguishes "does not exist" from "exists, not
yours" tells a stranger which ids are real. ``delete_text_memory`` returns False
in both cases and this returns 404 for both.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from . import Deps

logger = logging.getLogger("vanna.routes.memories")

# A memory is a sentence, not a document. The cap is generous enough for a real
# preference ("when I say revenue I mean net of refunds, excluding internal
# orders") and small enough that this cannot become a file store.
MAX_LENGTH = 2000


class MemoryPayload(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_LENGTH)


def register(app: Any, deps: Deps) -> None:
    def _store() -> Any:
        """The memory, or a 503 that names what is missing.

        503 rather than 500: on a deployment with no application database the
        memory is an ephemeral stub, and "this feature needs a database" is
        something the reader can act on.
        """
        memory = deps.agent_memory
        if memory is None:
            raise HTTPException(
                status_code=503,
                detail="Agent memory is not configured on this deployment.",
            )
        return memory

    @app.get("/api/vanna/v2/memories")
    async def list_memories(request: Request, limit: int = 50) -> Dict[str, Any]:
        """Everything this caller has had remembered about them.

        No admin check. These are the caller's own memories in their own
        workspace, and a person is entitled to read what has been recorded about
        them without being an administrator of anything.
        """
        user = await deps.caller(request)
        context = await deps.tool_context(user)
        memories: List[Any] = await _store().get_recent_text_memories(
            context, limit=limit
        )
        return {
            "memories": [
                {
                    "memory_id": memory.memory_id,
                    "content": memory.content,
                    "created_at": memory.timestamp,
                }
                for memory in memories
            ]
        }

    @app.post("/api/vanna/v2/memories", status_code=201)
    async def add_memory(payload: MemoryPayload, request: Request) -> Dict[str, Any]:
        """Write down a fact about yourself, without going through the agent.

        The agent can already save one when a conversation produces it, but that
        requires the model to decide a sentence was worth keeping. This is the
        deliberate path: the person says what to remember and it is remembered.
        """
        user = await deps.caller(request)
        context = await deps.tool_context(user)
        content = payload.content.strip()
        if not content:
            # Pydantic's min_length passes on "   ", which would store a blank
            # row that renders as an empty line nobody can identify to delete.
            raise HTTPException(status_code=422, detail="A memory cannot be blank.")
        memory = await _store().save_text_memory(content, context)
        return {
            "memory_id": memory.memory_id,
            "content": memory.content,
            "created_at": memory.timestamp,
        }

    @app.delete("/api/vanna/v2/memories/{memory_id}")
    async def forget(memory_id: str, request: Request) -> Dict[str, bool]:
        """Take one back.

        The counterpart to the route above, and the reason the pair is safe to
        offer: a store you can add to but not correct accumulates every mistaken
        inference it ever made about you.
        """
        user = await deps.caller(request)
        context = await deps.tool_context(user)
        if not await _store().delete_text_memory(context, memory_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}
