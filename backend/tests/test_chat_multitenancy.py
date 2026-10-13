"""Chat endpoints, added to the tenant-isolation property.

`test_tenant_isolation.py` proves workspace A cannot reach workspace B through
every route that takes a workspace in its path -- except the chat endpoints,
which were never in that matrix (`chat_sse`/`chat_poll`/`chat_websocket` do
not appear anywhere in it). Chat is the route surface that actually touches
the database on the tenant's behalf via SQL-generation tools, so it is not a
minor omission. This file reuses the same `world` fixture (real app, real
Postgres-backed sessions, `VANNA_LLM_PROVIDER=mock` already set by
`app_env`) and adds the chat routes to the same property.
"""

from __future__ import annotations

import pytest

from test_tenant_isolation import app_env, world  # noqa: F401 -- reused fixtures

pytestmark = pytest.mark.integration


class TestChatSameWorkspaceWorks:
    """The control: a same-tenant chat call must succeed before a cross-tenant
    one can mean anything."""

    async def test_a_viewer_can_chat_in_their_own_workspace(self, world):
        client = world["clients"]["acme.viewer"]
        response = await client.post(
            "/api/vanna/v2/chat_poll", json={"message": "hello"}
        )
        assert response.status_code == 200
        assert response.json()["total_chunks"] > 0

    async def test_chat_sse_streams_in_their_own_workspace(self, world):
        client = world["clients"]["acme.viewer"]
        response = await client.post(
            "/api/vanna/v2/chat_sse", json={"message": "hello"}
        )
        assert response.status_code == 200
        assert "data:" in response.text


class TestChatCrossWorkspaceAccess:
    """The same claim `test_tenant_isolation.py` makes for every other routed
    endpoint, extended to chat.

    The caller is resolved by `TenantDispatchChatHandler` before any agent is
    involved, so a refusal is a `PermissionError` raised out of the handler --
    not an in-band error from `Agent.send_message`, as this docstring used to
    claim. How it surfaces depends on the transport:

    * `chat_poll` answers 403, like every other routed endpoint. It used to
      answer 500, treating the refusal as a crash.
    * `chat_sse` has already sent its 200 and headers when the handler runs,
      so the refusal arrives as an error event inside the stream."""

    async def test_a_forged_workspace_header_is_refused_by_poll(self, world):
        client = world["clients"]["acme.viewer"]
        response = await client.post(
            "/api/vanna/v2/chat_poll",
            json={"message": "hello"},
            headers={"X-Tenant-Id": "globex"},
        )
        assert response.status_code == 403
        assert "not a member" in response.json()["detail"]

    async def test_a_forged_workspace_header_is_refused_in_the_sse_stream(self, world):
        client = world["clients"]["acme.viewer"]
        response = await client.post(
            "/api/vanna/v2/chat_sse",
            json={"message": "hello"},
            headers={"X-Tenant-Id": "globex"},
        )
        assert response.status_code == 200
        # No assistant content and no leak of globex's workspace -- the
        # stream's generic error event.
        assert "error" in response.text.lower()

    async def test_conversation_store_scopes_reads_by_user_not_just_id(
        self, world
    ):
        """The property the endpoint test above cannot assert on directly:
        the same conversation_id resolves to nothing for a different
        workspace's user, because `ConversationStore.get_conversation` scopes
        by (conversation_id, user), not conversation_id alone."""
        acme_client = world["clients"]["acme.viewer"]
        created = await acme_client.post(
            "/api/vanna/v2/chat_poll", json={"message": "hello"}
        )
        conversation_id = created.json()["conversation_id"]

        me = await world["clients"]["globex.viewer"].get("/api/vanna/v2/me")
        assert me.status_code == 200
        globex_email = me.json()["user"]["email"]

        app = world["app"]
        # Reach the same conversation store the app itself uses, and prove a
        # globex user cannot read acme's conversation through it.
        from vanna.core.user import User

        conversation_store = app.state.services["conversations"]
        globex_user = User(
            id=globex_email, email=globex_email, tenant_id="globex"
        )
        leaked = await conversation_store.get_conversation(conversation_id, globex_user)
        assert leaked is None
