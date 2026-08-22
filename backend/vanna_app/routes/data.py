"""History, saved queries, ad-hoc SQL, cubes and conversation threads.

Everything a signed-in user does with data that is not the chat itself.

The rule holding this file together: **nothing goes to the database except through
the workspace's tool registry.** Not the ad-hoc SQL box, not a cube query, not a
dashboard tile. The registry is where the SQL policy, the per-user policy, semantic
compilation and the row and column rules live, and it is the only path that cannot
drift from the agent's.

An earlier version of ``/run-sql`` validated against the workspace's static policy
and then called the runner itself. That was a hole: the "Run SQL" button applied
neither row-level rules nor the caller's own policy, so two users with different
permissions got the same rows.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..authz import forbid_viewer
from . import Deps

logger = logging.getLogger("vanna.routes.data")


class SavedQueryPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    sql: str = Field(min_length=1, max_length=100_000)
    question: str = Field(default="", max_length=2000)


class RunSqlPayload(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    limit: int = Field(default=200, ge=1, le=5000)
    #: Which of the workspace's databases to run against. Omitted means the
    #: default. Validated against the registry before anything is built from it --
    #: this arrives from a browser and is a preference, not a credential.
    data_source_id: Optional[str] = None


class CubeQueryPayload(BaseModel):
    measures: List[str] = Field(default_factory=list, max_length=20)
    dimensions: List[str] = Field(default_factory=list, max_length=10)
    time_dimension: Optional[str] = None
    granularity: Optional[str] = None
    limit: int = Field(default=200, ge=1, le=1000)


def register(app: Any, deps: Deps) -> None:

    def _manifest_of(runtime: Any) -> Any:
        return getattr(getattr(runtime.agent, "tool_registry", None), "manifest", None)

    async def _runtime_for_source(user: Any, data_source_id: Optional[str]) -> Any:
        """The runtime for a workspace's chosen database.

        Refuses an unregistered id rather than falling back to the default: a
        caller who named a database and silently got a different one would read
        the answer as being about the one they asked for.
        """
        from ..datasources import UnknownDataSource

        try:
            return await deps.runtime_for(user.tenant_id, data_source_id=data_source_id)
        except UnknownDataSource as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _execute(runtime: Any, statement: str, user: Any, *, label: str) -> Dict[str, Any]:
        """Run one statement through the registry and shape the result.

        Shared by ``/run-sql`` and the cube endpoint so both get identical policy
        treatment and identical error shapes.
        """
        from vanna.core.errors import ErrorPhase, VannaError
        from vanna.core.tool import ToolCall, ToolContext

        context = ToolContext(
            user=user,
            conversation_id=label,
            request_id=str(uuid.uuid4()),
            tenant_id=user.tenant_id,
            agent_memory=deps.agent_memory,
        )

        try:
            result = await runtime.agent.tool_registry.execute(
                ToolCall(id=str(uuid.uuid4()), name="run_sql", arguments={"sql": statement}),
                context,
            )
        except VannaError as exc:
            raise HTTPException(status_code=400, detail=exc.to_dict())
        except Exception as exc:
            error = VannaError.from_exception(
                exc, phase=ErrorPhase.SQL_EXECUTION, metadata={"sql": statement}
            )
            logger.warning("%s failed for %s: %s", label, user.tenant_id, error)
            raise HTTPException(status_code=400, detail=error.to_dict())

        if not result.success:
            # A rejection from the policy or the compiler arrives here rather than
            # as an exception, and is the caller's to fix.
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "policy_violation",
                    "phase": "sql_policy_check",
                    "message": result.error or result.result_for_llm or "Rejected.",
                    "requires_confirmation": bool(
                        (result.metadata or {}).get("requires_confirmation")
                    ),
                },
            )

        meta = result.metadata or {}
        rows = meta.get("results") or []
        return {
            "columns": [str(c) for c in (meta.get("columns") or [])],
            "rows": [
                list(row.values()) if isinstance(row, dict) else list(row) for row in rows
            ],
            "row_count": int(
                meta.get("row_count") or meta.get("rows_affected") or len(rows)
            ),
            "rows_affected": meta.get("rows_affected"),
            "truncated": bool(meta.get("truncated")),
            "warnings": list((context.metadata or {}).get("semantic_warnings") or []),
        }

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    #: Statuses the history filter accepts, so a typo cannot become a silent
    #: "matches nothing" that reads as an empty workspace.
    _HISTORY_STATUSES = {
        "valid", "invalid", "empty", "rejected_by_policy", "timeout", "error",
    }

    def _day(value: Optional[str], *, field: str) -> Optional[datetime]:
        """Parse a ``YYYY-MM-DD`` (or full ISO) bound from the query string."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"{field} must be an ISO date"
            ) from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @app.get("/api/vanna/v2/history")
    async def history(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        search: Optional[str] = None,
        mine: bool = False,
        status: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Past questions for the workspace, newest first.

        ``mine=true`` narrows to the caller. The default is the whole workspace
        because the point of shared history is seeing what colleagues already asked
        -- which is also why a viewer sees it: reading someone else's question is
        not a privilege escalation when everyone here belongs to the same workspace.

        ``status``, ``since``/``until`` and ``offset`` are filtered in the database.
        The page used to fetch a hard-coded hundred rows and narrow them in the
        browser, which cannot answer "the failures from last week" on a workspace
        with more history than that.
        """
        user = await deps.caller(request)
        if deps.generation_store is None:
            return {"history": [], "control_plane": False}
        if status and status not in _HISTORY_STATUSES:
            raise HTTPException(status_code=400, detail="Unknown status")
        rows = await deps.generation_store.history(
            user.tenant_id,
            limit=limit,
            offset=offset,
            search=search,
            user_id=user.id if mine else None,
            status=status,
            since=_day(since, field="since"),
            until=_day(until, field="until"),
        )
        return {"history": rows, "control_plane": True}

    @app.delete("/api/vanna/v2/history/{generation_id}")
    async def delete_history_row(generation_id: str, request: Request) -> Dict[str, Any]:
        """Forget one of your own turns.

        A viewer may do this: it is their own question, and forgetting it takes
        nothing away from anybody else. What they may not do is reach a row that is
        not theirs -- which the store enforces with a ``user_id`` predicate rather
        than a check here, so there is no path to the row that skips it.
        """
        user = await deps.caller(request)
        if deps.generation_store is None:
            raise HTTPException(status_code=503, detail="No control plane")
        removed = await deps.generation_store.delete_history_row(
            user.tenant_id, user.id, generation_id
        )
        if not removed:
            # 404 whether the row is absent or someone else's: telling a caller
            # "that exists, but not for you" is itself a disclosure.
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    @app.delete("/api/vanna/v2/history")
    async def clear_history(request: Request) -> Dict[str, Any]:
        """Forget all of your own turns. Nobody else's, and not the audit trail."""
        user = await deps.caller(request)
        if deps.generation_store is None:
            raise HTTPException(status_code=503, detail="No control plane")
        removed = await deps.generation_store.delete_history(user.tenant_id, user.id)
        return {"deleted": removed}

    # ------------------------------------------------------------------
    # Saved queries
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/saved-queries")
    async def list_saved(request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        if deps.directory is None:
            return {"saved": []}
        return {"saved": await deps.directory.list_saved(user.tenant_id)}

    @app.post("/api/vanna/v2/saved-queries")
    async def create_saved(payload: SavedQueryPayload, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        forbid_viewer(user, "save queries")
        title = payload.title.strip()
        statement = payload.sql.strip()
        if not title or not statement:
            raise HTTPException(status_code=400, detail="A title and SQL are required")
        saved = await deps.require_directory().save_query(
            user.tenant_id,
            title=title,
            sql=statement,
            question=payload.question.strip(),
            created_by=user.email or user.id,
        )
        return {"saved": saved}

    @app.delete("/api/vanna/v2/saved-queries/{saved_id}")
    async def delete_saved(saved_id: str, request: Request) -> Dict[str, Any]:
        user = await deps.caller(request)
        forbid_viewer(user, "delete saved queries")
        if not await deps.require_directory().delete_saved(user.tenant_id, saved_id):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}

    # ------------------------------------------------------------------
    # Ad-hoc SQL
    # ------------------------------------------------------------------

    def _has_limit(statement: str) -> bool:
        """Whether the statement already caps its own rows.

        Appending a second LIMIT would be a syntax error, and appending one to a
        write statement would be nonsense.
        """
        from vanna.core.sql_policy import has_row_limit

        try:
            if not statement.lstrip()[:6].lower().startswith(("select", "with")):
                return True  # not a row-returning statement; nothing to cap
            return has_row_limit(statement)
        except Exception:
            return True

    @app.post("/api/vanna/v2/run-sql")
    async def run_sql(payload: RunSqlPayload, request: Request) -> Dict[str, Any]:
        """Run a saved or edited statement, through the tool registry.

        The most direct route from a person to the database in the whole product:
        the SQL is theirs, not a model's. Everything the policy does for a
        generated query it must therefore do for this one -- which it does,
        because both go through ``tool_registry.execute``.
        """
        user = await deps.caller(request)
        runtime = await _runtime_for_source(user, payload.data_source_id)

        statement = payload.sql.strip().rstrip(";")
        if not statement:
            raise HTTPException(status_code=400, detail="No SQL provided")
        if not _has_limit(statement):
            statement = f"{statement} LIMIT {payload.limit}"

        return await _execute(runtime, statement, user, label="run-sql")

    # ------------------------------------------------------------------
    # Cubes
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/cubes")
    async def list_cubes(request: Request) -> Dict[str, Any]:
        """Cubes available to this workspace, with their measures."""
        user = await deps.caller(request)
        runtime = await deps.runtime_for_request(user, request)
        manifest = _manifest_of(runtime)
        if manifest is None:
            return {"cubes": []}

        return {
            "cubes": [
                {
                    "name": cube.name,
                    "base_object": cube.base_object,
                    "description": cube.description,
                    "measures": [
                        {"name": m.name, "expression": m.expression, "description": m.description}
                        for m in cube.measures
                    ],
                    "dimensions": [d.name for d in cube.dimensions],
                    "time_dimensions": [d.name for d in cube.time_dimensions],
                }
                for cube in manifest.cubes
            ]
        }

    @app.post("/api/vanna/v2/cubes/{cube_name}/query")
    async def query_cube(
        cube_name: str, payload: CubeQueryPayload, request: Request
    ) -> Dict[str, Any]:
        """Compute measures grouped by dimensions.

        Takes names, not SQL. A caller cannot express an aggregate at the wrong
        grain through this endpoint, because the aggregation was decided when the
        cube was defined -- which is the whole reason cubes exist.

        Every name is resolved against the cube before it reaches a string: the
        expressions come from the manifest, never from the request.
        """
        user = await deps.caller(request)
        runtime = await deps.runtime_for_request(user, request)
        manifest = _manifest_of(runtime)
        if manifest is None:
            raise HTTPException(status_code=503, detail="No semantic layer configured.")

        cube = manifest.cube(cube_name)
        if cube is None:
            raise HTTPException(status_code=404, detail="Not found")

        measures = [m for m in payload.measures if cube.measure(m)]
        dimensions = [d for d in payload.dimensions if cube.dimension(d)]
        if not measures:
            raise HTTPException(status_code=400, detail="Select at least one measure.")
        if payload.time_dimension and not cube.dimension(payload.time_dimension):
            raise HTTPException(status_code=400, detail="Unknown time dimension.")
        if payload.time_dimension and not payload.granularity:
            raise HTTPException(status_code=400, detail="A time dimension needs a granularity.")

        from vanna.core.errors import VannaError
        from vanna.semantic.compiler import truncate

        selected: List[str] = []
        grouping: List[str] = []

        if payload.time_dimension:
            try:
                # Inside the try: an unsupported granularity or dialect raises
                # VannaError, and outside it that surfaced as a 500 rather than the
                # 400 the compiler intended.
                bucket = truncate(
                    cube.dimension(payload.time_dimension).expression,
                    payload.granularity,
                    runtime.dialect,
                )
            except VannaError as exc:
                raise HTTPException(status_code=400, detail=exc.to_dict())
            selected.append(f"{bucket} AS {payload.time_dimension}")
            grouping.append(bucket)

        for name in dimensions:
            expression = cube.dimension(name).expression
            selected.append(f"{expression} AS {name}")
            grouping.append(expression)

        for name in measures:
            selected.append(f"{cube.measure(name).expression} AS {name}")

        statement = f"SELECT {', '.join(selected)} FROM {cube.base_object}"
        if grouping:
            statement += " GROUP BY " + ", ".join(grouping)
            statement += " ORDER BY " + ", ".join(grouping)
        statement += f" LIMIT {payload.limit}"

        # Through the registry, so the caller's row and column rules apply to a cube
        # exactly as they do to a typed query.
        return await _execute(runtime, statement, user, label="cube")

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    @app.get("/api/vanna/v2/conversations")
    async def list_conversations(
        request: Request, limit: int = 50, offset: int = 0
    ) -> Dict[str, Any]:
        """This user's threads, newest first. Titles and counts only.

        Paged: the sidebar asks for one page and appends. Without ``offset`` a
        long-lived workspace showed only its newest fifty threads with no way to
        reach the rest.
        """
        user = await deps.caller(request)
        if deps.conversation_store is None:
            return {"conversations": [], "persisted": False}
        return {
            "conversations": await deps.conversation_store.summaries(
                user.tenant_id,
                user.id,
                limit=min(max(limit, 1), 200),
                offset=max(offset, 0),
            ),
            "persisted": True,
        }

    @app.get("/api/vanna/v2/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str, request: Request) -> Dict[str, Any]:
        """One thread's messages, for replaying the transcript."""
        user = await deps.caller(request)
        found = await deps.require_conversations().get_conversation(conversation_id, user)
        if found is None:
            # The store filters by owner, so another user's id is a 404 here --
            # which is the right answer, and does not confirm it exists.
            raise HTTPException(status_code=404, detail="Not found")

        return {
            "id": found.id,
            "title": (found.metadata or {}).get("title") or "",
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "timestamp": message.timestamp.isoformat(),
                }
                for message in found.messages
                # Tool traffic is machinery, not transcript. Replaying it would show
                # the reader a conversation they never had.
                if message.role in ("user", "assistant") and message.content
            ],
        }

    @app.patch("/api/vanna/v2/conversations/{conversation_id}")
    async def rename_conversation(
        conversation_id: str, payload: Dict[str, Any], request: Request
    ) -> Dict[str, Any]:
        user = await deps.caller(request)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="A title is required")
        if not await deps.require_conversations().rename(
            user.tenant_id, user.id, conversation_id, title
        ):
            raise HTTPException(status_code=404, detail="Not found")
        return {"renamed": True}

    @app.delete("/api/vanna/v2/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request) -> Dict[str, Any]:
        """Delete a thread and its messages together.

        The messages live inside the row and go with it, so nothing is orphaned.
        """
        user = await deps.caller(request)
        if not await deps.require_conversations().delete_conversation(conversation_id, user):
            raise HTTPException(status_code=404, detail="Not found")
        return {"deleted": True}
