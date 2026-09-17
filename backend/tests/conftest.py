"""Shared fixtures.

Two tiers, and the split is deliberate.

**Unit tests** need no database. Configuration validation, the authorisation
predicates, client-address resolution, credential sealing, CSRF signing and the
window arithmetic in the limiters are all pure functions, and the properties that
matter about them -- "an empty admin list grants nobody", "an untrusted proxy's
X-Forwarded-For is ignored" -- are exactly the ones worth asserting on every commit
in under a second.

**Integration tests** are marked ``integration`` and need PostgreSQL. They cover what
unit tests structurally cannot: that the isolation holds through the real routes,
with real sessions, against the real schema. ``VANNA_TEST_DATABASE_URL`` points at a
throwaway database; without it they skip rather than fail, so ``pytest`` on a laptop
with no Postgres still does something useful.

The isolation matrix in ``test_tenant_isolation.py`` is the reason this file exists.
The system's entire value proposition is that workspace A cannot see workspace B, and
before this there was no automated proof of it at all.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest

ROOT = Path(__file__).resolve().parents[2]
# One entry: `backend/` holds both `vanna` (the library) and `vanna_app` (the
# application) as top-level packages. Matches the pythonpath in pyproject so
# `pytest` works on a fresh checkout with no install -- and neither is installable
# any more, so this is the only way either gets imported.
for path in (ROOT / "backend",):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


@pytest.fixture
def env() -> Dict[str, str]:
    """A minimal environment that validates in multi-tenant mode.

    Tests that care about one variable override that one variable, so the intent of
    each test is visible in its own body rather than spread across a fixture.
    """
    return {
        "VANNA_DEPLOYMENT_MODE": "multi-tenant",
        "VANNA_ADMIN_EMAILS": "root@example.com",
        "VANNA_APP_DATABASE_URL": "postgresql://u:p@localhost:5432/vanna_test",
        "VANNA_SECURE_COOKIES": "true",
        "VANNA_SECRET_KEY": "x" * 48,
        "VANNA_TRUSTED_PROXIES": "10.0.0.0/8",
        "VANNA_CORS_ORIGINS": "https://vanna.example.com",
    }


@pytest.fixture
def settings(env: Dict[str, str]) -> Any:
    from vanna_app.config import load_and_validate

    return load_and_validate(env)


@pytest.fixture
def demo_settings() -> Any:
    from vanna_app.config import load_and_validate

    return load_and_validate({"VANNA_DEPLOYMENT_MODE": "demo"})


# ----------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------


class FakeUser:
    """The shape ``authz`` reads, without needing the library's User model."""

    def __init__(
        self,
        email: str = "ada@example.com",
        tenant_id: str = "acme",
        role: str = "analyst",
        groups: Optional[list] = None,
        session_scope: str = "full",
    ) -> None:
        self.id = email
        self.email = email
        self.tenant_id = tenant_id
        self.group_memberships = groups if groups is not None else (
            ["user", "admin"] if role == "admin" else ["user"]
        )
        self.metadata = {"role": role, "session_scope": session_scope}


@pytest.fixture
def user_factory():
    return FakeUser


@pytest.fixture
def tool_context():
    """Build a ``ToolContext`` for a caller in a workspace.

    Every knowledge store scopes its reads and writes by ``tenant_scope(context)``,
    so a test that shares one context between two workspaces is not testing
    isolation -- it is testing one workspace twice.
    """
    from vanna.core.tool import ToolContext
    from vanna.core.user import User

    from vanna_app.platform import _build_memory

    memory = _build_memory()

    def build(
        tenant_id: str = "acme",
        email: str = "ada@acme.example",
        *,
        role: str = "admin",
        groups: Optional[list] = None,
    ) -> Any:
        held = groups if groups is not None else (
            ["user", "admin"] if role == "admin" else ["user"]
        )
        return ToolContext(
            user=User(
                id=email,
                email=email,
                tenant_id=tenant_id,
                group_memberships=held,
                metadata={"role": role},
            ),
            conversation_id="test",
            request_id=f"test:{uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            agent_memory=memory,
        )

    return build


# ----------------------------------------------------------------------
# PostgreSQL
# ----------------------------------------------------------------------


def _test_database_url() -> Optional[str]:
    return os.getenv("VANNA_TEST_DATABASE_URL", "").strip() or None


requires_postgres = pytest.mark.skipif(
    _test_database_url() is None,
    reason="VANNA_TEST_DATABASE_URL is not set; integration tests need PostgreSQL.",
)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not url:
        pytest.skip("VANNA_TEST_DATABASE_URL is not set")
    return url


@pytest.fixture
def app_db(database_url: str) -> Iterator[Any]:
    """A migrated control plane in a throwaway database.

    A fresh database per test rather than truncating between tests: truncation
    leaves sequences, extensions and any schema drift in place, so a migration bug
    hides behind the first test that happened to run. Creating and dropping costs
    tens of milliseconds and tests the migrations on every run for free.
    """
    import psycopg2
    from urllib.parse import urlsplit, urlunsplit

    from vanna_app.db import AppDatabase
    from vanna_app.migrate import upgrade

    name = f"vanna_test_{uuid.uuid4().hex[:12]}"
    parts = urlsplit(database_url)
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    target_url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", "", ""))

    connection = psycopg2.connect(admin_url, connect_timeout=10)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(f'CREATE DATABASE "{name}"')
    connection.close()

    db = AppDatabase(target_url, minconn=1, maxconn=8, create_if_missing=False)
    upgrade(db)
    try:
        yield db
    finally:
        db.close()
        connection = psycopg2.connect(admin_url, connect_timeout=10)
        connection.autocommit = True
        with connection.cursor() as cursor:
            # Terminate stragglers first: a leaked connection makes DROP hang, and a
            # hanging teardown is indistinguishable from a hanging test.
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
        connection.close()


@pytest.fixture
def directory(app_db: Any, settings: Any) -> Any:
    from vanna_app.secrets import Cipher
    from vanna_app.tenancy import Directory

    return Directory(app_db, Cipher(settings.secret_key))


@pytest.fixture
def accounts(app_db: Any) -> Any:
    from vanna_app.accounts import Accounts

    return Accounts(app_db)


@pytest.fixture
def billing(app_db: Any) -> Any:
    from vanna_app.billing import Billing

    return Billing(app_db)


@pytest.fixture
def counters(app_db: Any) -> Any:
    from vanna_app.limits import Counters

    return Counters(app_db)


# ----------------------------------------------------------------------
# Two workspaces, which is the minimum that can prove isolation
# ----------------------------------------------------------------------



# ----------------------------------------------------------------------
# Chat / Agent pipeline
# ----------------------------------------------------------------------
#
# These build a *real* ``Agent`` (not the ``_FakeAgent`` used by
# ``test_chat_commands.py``, which only exercises ``WorkflowHandler``) wired to
# ``MockLlmService`` so tests can drive the actual tool-call loop in
# ``Agent._send_message`` without needing a live LLM or Postgres.


@pytest.fixture
def chat_user_factory():
    """Build a real ``vanna.core.user.User`` for driving the Agent directly.

    Distinct from ``user_factory``/``FakeUser`` above, which only carries the
    attributes ``authz`` reads and is not a real ``User`` model instance.
    """
    from vanna.core.user import User

    def build(
        email: str = "viewer@acme.test",
        *,
        tenant_id: str = "acme",
        admin: bool = False,
    ) -> Any:
        return User(
            id=email,
            email=email,
            tenant_id=tenant_id,
            group_memberships=["admin"] if admin else [],
        )

    return build


@pytest.fixture
def mock_llm():
    from vanna.integrations.mock.llm import MockLlmService

    return MockLlmService()


@pytest.fixture
def make_agent(chat_user_factory):
    """Build a minimally-wired real ``Agent`` for chat-pipeline tests.

    Mirrors the construction in ``vanna_app/platform.py`` but without the
    tenant/catalog/write-service machinery that production wiring adds -- only
    the four required constructor arguments plus whatever a test overrides.
    """
    from vanna.core.agent.agent import Agent
    from vanna.core.agent.config import AgentConfig
    from vanna.core.registry import ToolRegistry
    from vanna.core.user.request_context import RequestContext
    from vanna.core.user.resolver import UserResolver
    from vanna.integrations.local import MemoryConversationStore
    from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory

    class _FixedUserResolver(UserResolver):
        def __init__(self, user):
            self._user = user

        async def resolve_user(self, request_context):
            return self._user

    def build(
        *,
        llm=None,
        tools=(),
        user=None,
        config=None,
        workflow_handler=None,
        lifecycle_hooks=(),
        conversation_store=None,
    ):
        user = user or chat_user_factory()
        registry = ToolRegistry()
        for tool, access_groups in tools:
            registry.register_local_tool(tool, access_groups)

        agent = Agent(
            llm_service=llm,
            tool_registry=registry,
            user_resolver=_FixedUserResolver(user),
            agent_memory=DemoAgentMemory(),
            conversation_store=conversation_store or MemoryConversationStore(),
            config=config or AgentConfig(stream_responses=False),
            workflow_handler=workflow_handler,
            lifecycle_hooks=list(lifecycle_hooks),
        )
        return agent, user

    return build


@pytest.fixture
def request_context():
    from vanna.core.user.request_context import RequestContext

    return RequestContext()


@pytest.fixture
async def two_workspaces(directory: Any, accounts: Any) -> Dict[str, Any]:
    """Two workspaces with a full cast in each.

    A single workspace cannot demonstrate isolation, and a matrix built inside each
    test would make the tests about the setup rather than the property.
    """
    people = {}
    for tenant in ("acme", "globex"):
        await directory.create_tenant(tenant, name=tenant.title())
        for role in ("admin", "analyst", "viewer"):
            email = f"{role}@{tenant}.example.com"
            await accounts.create(email, "correct-horse-battery", full_name=role.title())
            await directory.add_user(tenant, email, role=role, full_name=role.title())
            people[f"{tenant}.{role}"] = email

    await accounts.create("root@example.com", "correct-horse-battery")
    people["platform"] = "root@example.com"
    return people
