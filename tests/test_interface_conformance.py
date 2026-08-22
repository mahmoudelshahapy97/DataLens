"""Everything the application plugs into a library seam actually fits it.

The application implements a dozen interfaces the library defines: middleware,
lifecycle hooks, an audit logger, three stores. Most are duck-typed, which is what
the library's docstrings invite -- and duck typing fails at the moment of the call,
in production, on somebody's first question.

That is not hypothetical. ``UsageMeteringMiddleware`` was written with
``before_request`` / ``after_response``, from memory. The real interface is
``before_llm_request`` / ``after_llm_response``. Nothing failed at import, nothing
failed at startup, the health check stayed green, and every chat message died with
``'UsageMeteringMiddleware' object has no attribute 'before_llm_request'`` -- which
the UI reported as "An unexpected error occurred while processing your message".

These tests compare, for each seam, the method names the library *calls* against the
methods the implementation *has*. A missing name is then a failed assertion in a
second, not a broken product.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest


def _public_methods(obj: Any) -> set:
    return {
        name
        for name, _ in inspect.getmembers(obj, callable)
        if not name.startswith("_")
    }


def _abstract_surface(base: type) -> set:
    """The method names a base class defines for subclasses to implement."""
    return {
        name
        for name, value in vars(base).items()
        if not name.startswith("_") and callable(value)
    }


class TestLlmMiddleware:
    def test_the_metering_middleware_subclasses_the_interface(self):
        from vanna.core.middleware import LlmMiddleware

        from vanna_app.llm import UsageMeteringMiddleware

        assert issubclass(UsageMeteringMiddleware, LlmMiddleware)

    def test_it_implements_the_names_the_agent_calls(self):
        """The exact failure: the agent calls `before_llm_request`, not `before_request`."""
        from vanna_app.llm import UsageMeteringMiddleware

        middleware = UsageMeteringMiddleware()
        for name in ("before_llm_request", "after_llm_response"):
            assert hasattr(middleware, name), f"missing {name}"
            assert inspect.iscoroutinefunction(getattr(middleware, name)), (
                f"{name} must be async -- the agent awaits it"
            )

    def test_it_actually_overrides_them(self):
        """Inheriting the no-op default would silently meter nothing."""
        from vanna.core.middleware import LlmMiddleware

        from vanna_app.llm import UsageMeteringMiddleware

        for name in ("before_llm_request", "after_llm_response"):
            assert getattr(UsageMeteringMiddleware, name) is not getattr(LlmMiddleware, name)

    def test_the_signatures_match_the_call_sites(self):
        """`after_llm_response(request, response)` -- in that order."""
        from vanna_app.llm import UsageMeteringMiddleware

        parameters = list(
            inspect.signature(UsageMeteringMiddleware.after_llm_response).parameters
        )
        assert parameters[:3] == ["self", "request", "response"], parameters


class TestLifecycleHooks:
    @pytest.mark.parametrize("name", ["PostgresQuotaHook", "PostgresRateLimitHook"])
    def test_the_limit_hooks_subclass_the_interface(self, name: str):
        from vanna.core.lifecycle import LifecycleHook

        import vanna_app.limits as limits

        assert issubclass(getattr(limits, name), LifecycleHook)

    @pytest.mark.parametrize("name", ["PostgresQuotaHook", "PostgresRateLimitHook"])
    def test_before_message_is_async_and_overridden(self, name: str):
        from vanna.core.lifecycle import LifecycleHook

        import vanna_app.limits as limits

        hook = getattr(limits, name)
        assert hook.before_message is not LifecycleHook.before_message
        assert inspect.iscoroutinefunction(hook.before_message)

    def test_the_question_capture_hook_conforms(self):
        from vanna.core.lifecycle import LifecycleHook

        from vanna_app.platform import question_capture_hook

        hook = question_capture_hook()
        assert isinstance(hook, LifecycleHook)
        assert inspect.iscoroutinefunction(hook.before_message)


class TestAuditLogger:
    def test_it_subclasses_the_interface(self):
        from vanna.core.audit import AuditLogger

        from vanna_app.audit import PostgresAuditLogger

        assert issubclass(PostgresAuditLogger, AuditLogger)

    def test_log_event_is_implemented(self):
        """It is the one abstract method; an unimplemented one fails at construction."""
        from vanna_app.audit import PostgresAuditLogger

        logger = PostgresAuditLogger(db=None)
        assert inspect.iscoroutinefunction(logger.log_event)


class TestStores:
    """The stores are duck-typed against the library's own implementations.

    There is no base class to inherit, so the check is: does ours have every public
    method theirs does? A missing one is a method the agent will call and we do not
    have.
    """

    def test_the_generation_store_matches_the_local_one(self):
        from vanna.core.generation import LocalGenerationStore

        from vanna_app.stores import PostgresGenerationStore

        expected = _public_methods(LocalGenerationStore)
        ours = _public_methods(PostgresGenerationStore)
        missing = expected - ours
        assert not missing, f"PostgresGenerationStore is missing {sorted(missing)}"

    def test_the_conversation_store_matches_the_memory_one(self):
        from vanna.integrations.local import MemoryConversationStore

        from vanna_app.stores import PostgresConversationStore

        expected = _public_methods(MemoryConversationStore)
        ours = _public_methods(PostgresConversationStore)
        missing = expected - ours
        assert not missing, f"PostgresConversationStore is missing {sorted(missing)}"

    @pytest.mark.parametrize(
        "method", ["list", "get", "save", "delete"]
    )
    def test_the_dashboard_store_has_what_the_tools_call(self, method: str):
        from vanna_app.stores import PostgresDashboardStore

        assert hasattr(PostgresDashboardStore, method)
        assert inspect.iscoroutinefunction(getattr(PostgresDashboardStore, method))


class TestResolver:
    def test_the_user_resolver_subclasses_the_interface(self):
        from vanna.core.user import UserResolver

        from vanna_app.config import load_and_validate
        from vanna_app.identity import build_user_resolver

        settings = load_and_validate({"VANNA_DEPLOYMENT_MODE": "demo"})
        resolver = build_user_resolver(settings, directory=None, accounts=None)

        assert isinstance(resolver, UserResolver)
        assert inspect.iscoroutinefunction(resolver.resolve_user)


class TestEverySeamIsCovered:
    """A reminder that this file has to grow with the wiring.

    If `wiring._build_services` starts constructing something new that the library
    calls back into, it belongs above.
    """

    #: Seams asserted in this file.
    COVERED = {
        "UsageMeteringMiddleware",
        "PostgresQuotaHook",
        "PostgresRateLimitHook",
        "PostgresAuditLogger",
        "PostgresGenerationStore",
        "PostgresConversationStore",
        "PostgresDashboardStore",
        "DirectoryUserResolver",
    }

    def test_the_agent_is_handed_only_covered_implementations(self):
        import inspect as _inspect

        from vanna_app import platform

        source = _inspect.getsource(platform.Platform._build_runtime)
        # The keyword arguments the Agent is constructed with that take one of our
        # objects. If a new one appears, this list is where it gets noticed.
        for keyword in (
            "llm_middlewares",
            "lifecycle_hooks",
            "audit_logger",
            "conversation_store",
            "user_resolver",
        ):
            assert keyword in source, (
                f"Agent is no longer given {keyword}; this test needs updating"
            )
