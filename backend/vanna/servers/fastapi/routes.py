"""
FastAPI route implementations for Vanna Agents.
"""

import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, Iterator, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..base import ChatHandler, ChatRequest, ChatResponse, ChatStreamChunk
from ...components import (
    ChatInputUpdateComponent,
    SimpleTextComponent,
    StatusBarUpdateComponent,
    StatusCardComponent,
    UiComponent,
)
from ...core.user.request_context import RequestContext

logger = logging.getLogger(__name__)

#: How long the SSE stream may go without a frame before it sends a comment to
#: keep the connection open. A tool-heavy turn can easily run past nginx's
#: 60-second ``proxy_read_timeout`` with nothing to say.
_SSE_KEEPALIVE_SECONDS = 15.0

#: What the client is told when the stream fails. The exception text stays in
#: the log: it names internal tables, hosts and identifiers.
_STREAM_ERROR_MESSAGE = (
    "Something went wrong while answering that. Please try again."
)


def _error_chunks(conversation_id: str, request_id: str) -> Iterator[str]:
    """The frames a failed stream ends with, in the normal chunk shape."""
    components = [
        UiComponent(
            rich_component=StatusCardComponent(
                title="Error Processing Message",
                status="error",
                description=_STREAM_ERROR_MESSAGE,
                icon="!",
            ),
            simple_component=SimpleTextComponent(text=_STREAM_ERROR_MESSAGE),
        ),
        UiComponent(
            rich_component=StatusBarUpdateComponent(
                status="error",
                message="Error occurred",
                detail=_STREAM_ERROR_MESSAGE,
            )
        ),
        # Without this the composer stays disabled and the user cannot retry.
        UiComponent(
            rich_component=ChatInputUpdateComponent(
                placeholder="Try again...", disabled=False
            )
        ),
    ]
    for component in components:
        chunk = ChatStreamChunk.from_component(
            component, conversation_id, request_id
        )
        yield f"data: {chunk.model_dump_json()}\n\n"


def register_chat_routes(
    app: FastAPI, chat_handler: ChatHandler, config: Optional[Dict[str, Any]] = None
) -> None:
    """Register chat routes on FastAPI app.

    Args:
        app: FastAPI application
        chat_handler: Chat handler instance
        config: Server configuration
    """
    config = config or {}

    @app.post("/api/vanna/v2/chat_sse")
    async def chat_sse(
        chat_request: ChatRequest, http_request: Request
    ) -> StreamingResponse:
        """Server-Sent Events endpoint for streaming chat."""
        # Extract request context for user resolution
        chat_request.request_context = RequestContext(
            cookies=dict(http_request.cookies),
            headers=dict(http_request.headers),
            remote_addr=http_request.client.host if http_request.client else None,
            query_params=dict(http_request.query_params),
            metadata=chat_request.metadata,
        )

        async def generate() -> AsyncGenerator[str, None]:
            """Generate SSE stream."""
            try:
                stream = chat_handler.handle_stream(chat_request).__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            stream.__anext__(), timeout=_SSE_KEEPALIVE_SECONDS
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        # An SSE comment: keeps proxies from reaping an idle
                        # connection, and is ignored by any client that only
                        # reads "data: " lines.
                        yield ": keepalive\n\n"
                        continue

                    yield f"data: {chunk.model_dump_json()}\n\n"
            except Exception:
                logger.exception(
                    "chat_sse stream failed (conversation_id=%s, request_id=%s)",
                    chat_request.conversation_id,
                    chat_request.request_id,
                )
                # Emitted in the normal chunk shape. A bare {"type": "error"}
                # frame has no "rich" key, so the client dropped it on the floor
                # and the user saw the turn stall with no explanation.
                for frame in _error_chunks(
                    chat_request.conversation_id or "",
                    chat_request.request_id or "",
                ):
                    yield frame
            finally:
                # In a finally block so the client's read loop terminates on the
                # error path too, rather than hanging until the socket closes.
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    @app.websocket("/api/vanna/v2/chat_websocket")
    async def chat_websocket(websocket: WebSocket) -> None:
        """WebSocket endpoint for real-time chat."""
        await websocket.accept()

        try:
            while True:
                # Receive message
                try:
                    data = await websocket.receive_json()

                    # Extract request context for user resolution
                    metadata = data.get("metadata", {})
                    data["request_context"] = RequestContext(
                        cookies=dict(websocket.cookies),
                        headers=dict(websocket.headers),
                        remote_addr=websocket.client.host if websocket.client else None,
                        query_params=dict(websocket.query_params),
                        metadata=metadata,
                    )

                    chat_request = ChatRequest(**data)
                except Exception as e:
                    logger.exception("chat_websocket received an invalid request")
                    await websocket.send_json(
                        {
                            "type": "error",
                            "data": {"message": f"Invalid request: {str(e)}"},
                        }
                    )
                    continue

                # Stream response
                try:
                    # Scoped to this turn. The loop variable used to be read
                    # through `"chunk" in locals()`, which stayed true across
                    # iterations -- so a turn that yielded nothing reported the
                    # *previous* turn's ids.
                    last_chunk = None
                    async for chunk in chat_handler.handle_stream(chat_request):
                        last_chunk = chunk
                        await websocket.send_json(chunk.model_dump())

                    # Send completion signal
                    await websocket.send_json(
                        {
                            "type": "completion",
                            "data": {"status": "done"},
                            "conversation_id": (
                                last_chunk.conversation_id
                                if last_chunk
                                else (chat_request.conversation_id or "")
                            ),
                            "request_id": (
                                last_chunk.request_id
                                if last_chunk
                                else (chat_request.request_id or "")
                            ),
                        }
                    )

                except Exception:
                    logger.exception(
                        "chat_websocket stream failed "
                        "(conversation_id=%s, request_id=%s)",
                        chat_request.conversation_id,
                        chat_request.request_id,
                    )
                    await websocket.send_json(
                        {
                            "type": "error",
                            "data": {"message": _STREAM_ERROR_MESSAGE},
                            "conversation_id": chat_request.conversation_id or "",
                            "request_id": chat_request.request_id or "",
                        }
                    )

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("chat_websocket failed")
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "data": {"message": _STREAM_ERROR_MESSAGE},
                    }
                )
            except Exception:
                pass
            finally:
                await websocket.close()

    @app.post("/api/vanna/v2/chat_poll")
    async def chat_poll(
        chat_request: ChatRequest, http_request: Request
    ) -> ChatResponse:
        """Polling endpoint for chat."""
        # Extract request context for user resolution
        chat_request.request_context = RequestContext(
            cookies=dict(http_request.cookies),
            headers=dict(http_request.headers),
            remote_addr=http_request.client.host if http_request.client else None,
            query_params=dict(http_request.query_params),
            metadata=chat_request.metadata,
        )

        try:
            result = await chat_handler.handle_poll(chat_request)
            return result
        except Exception:
            logger.exception(
                "chat_poll failed (conversation_id=%s, request_id=%s)",
                chat_request.conversation_id,
                chat_request.request_id,
            )
            # Poll is the fallback transport, so a non-200 is exactly the signal
            # the client needs -- but the exception text is not for the client.
            # The request id is, so support can find the log line.
            detail = "Chat failed"
            if chat_request.request_id:
                detail = f"{detail} (request_id={chat_request.request_id})"
            raise HTTPException(status_code=500, detail=detail)
