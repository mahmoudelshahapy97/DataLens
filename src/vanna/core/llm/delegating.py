"""An LLM service that can be redirected for the duration of one request.

The problem this solves: an ``Agent`` binds its ``LlmService`` once, at construction, and
in a multi-tenant deployment agents are cached and reused across every request for a
workspace. So there is no moment at which "use *this* caller's API key instead" can be
expressed by handing the agent a different service -- by the time the caller is known, the
agent already exists.

``DelegatingLlmService`` is the seam. It is what the agent is built with, and on each call
it asks a :class:`~contextvars.ContextVar` which service to actually use, falling back to
the shared one. A context variable rather than a parameter because the decision has to
travel through ``Agent``, the middleware chain and the tool loop, none of which have any
business knowing about API keys.

Usage::

    shared = OpenAILlmService()
    llm = DelegatingLlmService(shared)

    # per request, once the caller is known:
    token = use_llm_service(OpenAILlmService(api_key=their_key))
    try:
        ...                      # everything in this task now uses their key
    finally:
        release_llm_service(token)

The scoping rule worth knowing: a context variable set in a task is visible to that task
and to tasks it *later* creates, and to nothing else. That is exactly the boundary of one
request, including its streaming response -- and it is why two concurrent requests with
different keys cannot see each other's.
"""

from __future__ import annotations

import contextvars
from typing import Any, AsyncGenerator, List, Optional

from .base import LlmService
from .models import LlmRequest, LlmResponse, LlmStreamChunk

#: The service in force for the current request, if any.
_current: contextvars.ContextVar[Optional[LlmService]] = contextvars.ContextVar(
    "vanna_current_llm_service", default=None
)


def use_llm_service(service: Optional[LlmService]) -> contextvars.Token:
    """Redirect LLM calls in this context. Returns a token for :func:`release_llm_service`."""
    return _current.set(service)


def release_llm_service(token: contextvars.Token) -> None:
    """Undo :func:`use_llm_service`.

    Always in a ``finally``. A leaked override would otherwise persist for whatever the
    worker handles next -- which is one caller's key being used to answer another
    caller's question, the single worst outcome this module could have.
    """
    _current.reset(token)


def current_llm_service() -> Optional[LlmService]:
    """The override in force, or None."""
    return _current.get()


class DelegatingLlmService(LlmService):
    """Routes each call to the per-request service, or to the shared default."""

    def __init__(self, default: LlmService) -> None:
        self.default = default

    @property
    def active(self) -> LlmService:
        return _current.get() or self.default

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        return await self.active.send_request(request)

    async def stream_request(
        self, request: LlmRequest
    ) -> AsyncGenerator[LlmStreamChunk, None]:
        # Resolved once, before the first yield, so a stream cannot switch services
        # halfway through if the context changes underneath it.
        service = self.active
        async for chunk in service.stream_request(request):
            yield chunk

    async def validate_tools(self, tools: List[Any]) -> List[str]:
        return await self.active.validate_tools(tools)

    def __getattr__(self, name: str) -> Any:
        """Forward anything else to the default service.

        Callers reach past the interface for `model`, `dialect` and similar. Forwarding
        to the *default* rather than the active service is deliberate: those reads happen
        at setup time, outside any request, where there is no override to consult.
        """
        # `default` itself must never be forwarded. __getattr__ runs only when normal
        # lookup fails, so on an instance built without __init__ -- copy, pickle, or a
        # subclass that forgets super() -- this line would look up `self.default`, fail
        # again, re-enter, and recurse until the stack dies with a RecursionError that
        # names nothing useful.
        if name == "default":
            raise AttributeError(
                "DelegatingLlmService has no `default`: it was constructed without "
                "__init__ (copy, pickle, or a subclass that skipped super().__init__)."
            )
        return getattr(self.default, name)
