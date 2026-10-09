"""
Framework-agnostic chat handling logic.
"""

import uuid
from typing import AsyncGenerator, Dict, List

from ...core import Agent
from .models import ChatRequest, ChatResponse, ChatStreamChunk


class ChatHandler:
    """Core chat handling logic - framework agnostic."""

    def __init__(
        self,
        agent: Agent,
    ):
        """Initialize chat handler.

        Args:
            agent: The agent to handle chat requests
        """
        self.agent = agent

    async def handle_stream(
        self, request: ChatRequest
    ) -> AsyncGenerator[ChatStreamChunk, None]:
        """Stream chat responses.

        Args:
            request: Chat request

        Yields:
            Chat stream chunks
        """
        conversation_id = request.conversation_id or self._generate_conversation_id()
        # Use request_id from client for tracking, or use the one generated internally
        request_id = request.request_id or str(uuid.uuid4())

        async for component in self.agent.send_message(
            request_context=request.request_context,
            message=request.message,
            conversation_id=conversation_id,
        ):
            yield ChatStreamChunk.from_component(component, conversation_id, request_id)

    async def handle_poll(self, request: ChatRequest) -> ChatResponse:
        """Handle polling-based chat.

        Args:
            request: Chat request

        Returns:
            Complete chat response
        """
        chunks = []
        async for chunk in self.handle_stream(request):
            chunks.append(chunk)

        return ChatResponse.from_chunks(self._coalesce(chunks))

    @staticmethod
    def _coalesce(chunks: List[ChatStreamChunk]) -> List[ChatStreamChunk]:
        """Drop superseded delta frames from a non-streaming response.

        A streamed answer arrives as one ``create`` plus many ``update``s to the
        same component id. That is the point over a live connection, but a poll
        client receives the whole turn at once and replays it into the same
        renderer -- so every intermediate frame is a prefix of the next one, and
        doubles the payload to show text that is overwritten before it is seen.

        Only intermediate ``update``s are dropped. The ``create`` stays (it is
        what the component is built from) and so does the last frame for each
        id, whether that is the full text or a ``remove``.
        """
        last_update_index: Dict[str, int] = {}
        for index, chunk in enumerate(chunks):
            component_id = chunk.rich.get("id")
            if component_id and chunk.rich.get("lifecycle") == "update":
                last_update_index[component_id] = index

        kept = []
        for index, chunk in enumerate(chunks):
            component_id = chunk.rich.get("id")
            superseded = (
                chunk.rich.get("lifecycle") == "update"
                and component_id is not None
                and last_update_index.get(component_id) != index
            )
            if not superseded:
                kept.append(chunk)
        return kept

    def _generate_conversation_id(self) -> str:
        """Generate new conversation ID."""
        return f"conv_{uuid.uuid4().hex[:8]}"
