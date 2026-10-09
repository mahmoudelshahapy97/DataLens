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
    endpoint, extended to chat -- with one difference worth calling out: chat
    does not reject with an HTTP error status the way every other routed
    endpoint in `test_tenant_isolation.py` does. `Agent.send_message` (used by
    all three chat transports) catches every exception raised while resolving
    the user or handling the message and turns it into an in-band error
    component, so a permission failure comes back as HTTP 200 with an error
    chunk in the body, not a 403/404. That is a real behavioural asymmetry
    with the rest of the API, not a gap in this test -- callers relying on
    status codes elsewhere in the app cannot rely on one here."""

    @pytest.mark.parametrize("path", ["/api/vanna/v2/chat_poll", "/api/vanna/v2/chat_sse"])
    async def test_a_forged_workspace_header_does_not_grant_a_chat_in_it(
        self, world, path
    ):
        client = world["clients"]["acme.viewer"]
        response = await client.post(
            path,
            json={"message": "hello"},
            headers={"X-Tenant-Id": "globex"},
        )
        assert response.status_code == 200
        # No real assistant content, no leak of globex's workspace -- just the
        # generic failure surface `Agent.send_message` produces for an
        # exception it did not expect (here, resolve_user's PermissionError).
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
