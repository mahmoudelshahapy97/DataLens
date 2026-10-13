"""Reopening a thread: the transcript endpoint and its Markdown export."""

from __future__ import annotations

import pytest

from test_tenant_isolation import app_env, world  # noqa: F401 -- reused fixtures

pytestmark = pytest.mark.integration


class TestConversationTranscript:
    async def _start(self, world):
        created = await world["clients"]["acme.viewer"].post(
            "/api/vanna/v2/chat_poll", json={"message": "hello"}
        )
        return created.json()["conversation_id"]

    async def test_owner_gets_messages_back(self, world):
        conversation_id = await self._start(world)
        response = await world["clients"]["acme.viewer"].get(
            f"/api/vanna/v2/conversations/{conversation_id}"
        )
        assert response.status_code == 200
        roles = [m["role"] for m in response.json()["messages"]]
        assert roles[0] == "user"

    async def test_export_is_markdown_with_the_question(self, world):
        conversation_id = await self._start(world)
        response = await world["clients"]["acme.viewer"].get(
            f"/api/vanna/v2/conversations/{conversation_id}/export"
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers["content-disposition"]
        assert "hello" in response.text

    async def test_other_tenant_cannot_export(self, world):
        conversation_id = await self._start(world)
        response = await world["clients"]["globex.viewer"].get(
            f"/api/vanna/v2/conversations/{conversation_id}/export"
        )
        assert response.status_code == 404
