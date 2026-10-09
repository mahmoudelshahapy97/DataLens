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


class TestReasoningEffort:
    def _payload(self, model, monkeypatch, effort=None):
        if effort is None:
            monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
        else:
            monkeypatch.setenv("OPENAI_REASONING_EFFORT", effort)
        service = OpenAILlmService.__new__(OpenAILlmService)
        service.model = model
        return service._build_payload(TestPayload._request())

    def test_reasoning_models_default_to_low(self, monkeypatch):
        assert self._payload("gpt-5.4-mini", monkeypatch)["reasoning_effort"] == "low"

    def test_env_overrides_the_default(self, monkeypatch):
        payload = self._payload("gpt-5", monkeypatch, effort="medium")
        assert payload["reasoning_effort"] == "medium"

    def test_invalid_env_falls_back_to_low(self, monkeypatch):
        payload = self._payload("gpt-5", monkeypatch, effort="minimal")
        assert payload["reasoning_effort"] == "low"

    def test_empty_env_sends_nothing(self, monkeypatch):
        assert "reasoning_effort" not in self._payload("gpt-5", monkeypatch, effort="")

    def test_non_reasoning_models_never_get_it(self, monkeypatch):
        assert "reasoning_effort" not in self._payload("gpt-4o", monkeypatch)


class TestReasoningEffortOverride:
    def _payload(self, model="gpt-5.4-mini"):
        service = OpenAILlmService.__new__(OpenAILlmService)
        service.model = model
        return service._build_payload(TestPayload._request())

    def test_caller_choice_beats_the_env_default(self, monkeypatch):
        from vanna.integrations.openai.llm import (
            release_reasoning_effort,
            use_reasoning_effort,
        )

        monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
        token = use_reasoning_effort("HIGH")
        try:
            assert self._payload()["reasoning_effort"] == "high"
        finally:
            release_reasoning_effort(token)
        assert self._payload()["reasoning_effort"] == "low"

    def test_unknown_value_is_ignored(self):
        from vanna.integrations.openai.llm import use_reasoning_effort

        assert use_reasoning_effort("extreme") is None
        assert use_reasoning_effort("minimal") is None
        assert use_reasoning_effort("") is None
