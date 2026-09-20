"""gpt-5 and the o-series renamed `max_tokens`, and reject the old spelling.

`AgentConfig.max_tokens` defaults to 4096 and is forwarded on every request, so
sending the wrong spelling is not a degraded experience -- it is a 400 on every
single call, and the chat cannot answer anything at all. The default model in
`OpenAILlmService` is `gpt-5`, so the default configuration was the broken one.

These are cheap tests for an outage-class bug, which is exactly when they are
worth writing.
"""

from __future__ import annotations

import pytest

from vanna.core.llm.models import LlmMessage, LlmRequest
from vanna.core.user import User
from vanna.integrations.openai.llm import (
    OpenAILlmService,
    _token_limit_parameter,
)


class TestTokenLimitParameter:
    @pytest.mark.parametrize(
        "model",
        ["gpt-5", "gpt-5-mini", "gpt-5.1", "o1", "o1-preview", "o3-mini", "o4-mini"],
    )
    def test_new_families_use_max_completion_tokens(self, model):
        assert _token_limit_parameter(model) == "max_completion_tokens"

    @pytest.mark.parametrize(
        "model", ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]
    )
    def test_older_families_keep_max_tokens(self, model):
        assert _token_limit_parameter(model) == "max_tokens"

    def test_matching_ignores_case(self):
        assert _token_limit_parameter("GPT-5") == "max_completion_tokens"

    def test_an_unknown_model_keeps_the_older_spelling(self):
        """A wrong guess here should degrade, not break every request."""
        assert _token_limit_parameter("some-future-model") == "max_tokens"

    @pytest.mark.parametrize("model", ["", None])
    def test_missing_model_does_not_raise(self, model):
        assert _token_limit_parameter(model) == "max_tokens"


class TestPayload:
    @staticmethod
    def _request(max_tokens=4096):
        return LlmRequest(
            messages=[LlmMessage(role="user", content="hi")],
            user=User(id="u1", email="u1@acme.test", tenant_id="acme"),
            max_tokens=max_tokens,
        )

    def _payload(self, model, **kw):
        service = OpenAILlmService.__new__(OpenAILlmService)
        service.model = model
        return service._build_payload(self._request(**kw))

    def test_gpt5_payload_carries_the_new_name_only(self):
        payload = self._payload("gpt-5")

        assert payload["max_completion_tokens"] == 4096
        # Sending both is also a 400.
        assert "max_tokens" not in payload

    def test_gpt4o_payload_carries_the_old_name_only(self):
        payload = self._payload("gpt-4o")

        assert payload["max_tokens"] == 4096
        assert "max_completion_tokens" not in payload

    def test_no_cap_sends_neither(self):
        payload = self._payload("gpt-5", max_tokens=None)

        assert "max_tokens" not in payload
        assert "max_completion_tokens" not in payload
