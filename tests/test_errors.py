"""Error classification and redaction.

``VannaError`` decides two things that reach a user: what an error is called, and
what of its context is safe to show. The second one had a hole -- the redaction
matched an exact list of key names, so a metadata dict carrying ``password`` or
``api_key`` was serialised straight into an HTTP response.
"""

from __future__ import annotations

import pytest

from vanna.core.errors import (
    SENSITIVE_METADATA_KEYS,
    ErrorCode,
    ErrorPhase,
    VannaError,
)


class TestRedaction:
    def test_query_text_is_dropped(self):
        """SQL embeds schema structure and filter values -- both are disclosures."""
        error = VannaError(
            ErrorCode.INVALID_SQL,
            "boom",
            metadata={"sql": "SELECT secret FROM t", "row_count": 3},
        )
        assert error.to_dict()["metadata"] == {"row_count": 3}

    @pytest.mark.parametrize("key", sorted(SENSITIVE_METADATA_KEYS))
    def test_every_named_key_is_dropped(self, key):
        error = VannaError(ErrorCode.INVALID_SQL, "boom", metadata={key: "x", "safe": 1})
        assert error.to_dict()["metadata"] == {"safe": 1}

    @pytest.mark.parametrize(
        "key",
        [
            "password", "db_password", "PASSWORD", "passwd", "pwd",
            "secret", "client_secret", "api_key", "apiKey", "APIKEY",
            "credential", "credentials", "authorization", "cookie",
            "private_key", "dsn",
        ],
    )
    def test_credential_shaped_keys_are_dropped(self, key):
        """The hole: a deny-list of exact names fails open on everything else."""
        error = VannaError(ErrorCode.INTERNAL_ERROR, "boom", metadata={key: "x", "rows": 5})
        assert error.to_dict()["metadata"] == {"rows": 5}

    def test_harmless_metadata_survives(self):
        error = VannaError(
            ErrorCode.RESULT_TOO_LARGE,
            "too many rows",
            metadata={"row_count": 10_000, "limit": 1_000, "table": "orders"},
        )
        assert error.to_dict()["metadata"] == {
            "row_count": 10_000, "limit": 1_000, "table": "orders"
        }

    def test_nothing_is_dropped_when_redaction_is_off(self):
        # Logs and explicit debug commands, where the reader is already trusted.
        error = VannaError(ErrorCode.INVALID_SQL, "boom", metadata={"sql": "SELECT 1"})
        assert error.to_dict(redact=False)["metadata"] == {"sql": "SELECT 1"}

    def test_an_empty_metadata_dict_is_omitted_entirely(self):
        assert "metadata" not in VannaError(ErrorCode.INTERNAL_ERROR, "boom").to_dict()

    def test_redaction_that_empties_the_dict_omits_the_field(self):
        error = VannaError(ErrorCode.INVALID_SQL, "boom", metadata={"sql": "SELECT 1"})
        assert "metadata" not in error.to_dict()


class TestShape:
    def test_the_envelope_carries_what_a_client_needs(self):
        error = VannaError(
            ErrorCode.QUOTA_EXCEEDED,
            "out of requests",
            phase=ErrorPhase.SQL_EXECUTION,
            hint="Try again tomorrow.",
        )
        payload = error.to_dict()
        assert payload["code"] == "quota_exceeded"
        assert payload["phase"] == "sql_execution"
        assert payload["message"] == "out of requests"
        assert payload["hint"] == "Try again tomorrow."
        assert "user_error" in payload and "retryable" in payload

    def test_the_message_does_not_duplicate_the_phase(self):
        """`str(error)` prefixes the phase; the dict must not, or they can disagree."""
        error = VannaError(ErrorCode.INVALID_SQL, "boom", phase=ErrorPhase.SQL_EXECUTION)
        assert error.to_dict()["message"] == "boom"
        assert "sql_execution" in str(error)

    def test_a_missing_hint_is_omitted_rather_than_null(self):
        assert "hint" not in VannaError(ErrorCode.INTERNAL_ERROR, "boom").to_dict()


class TestClassification:
    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.INVALID_REQUEST,
            ErrorCode.PERMISSION_DENIED,
            ErrorCode.QUOTA_EXCEEDED,
            ErrorCode.POLICY_VIOLATION,
            ErrorCode.INVALID_SQL,
        ],
    )
    def test_user_errors_are_marked_as_such(self, code):
        # Drives whether the UI shows "you can fix this" or "we broke something".
        assert VannaError(code, "x").is_user_error

    def test_an_internal_error_is_not_the_user_s_fault(self):
        assert not VannaError(ErrorCode.INTERNAL_ERROR, "x").is_user_error

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.INVALID_SQL,
            ErrorCode.OBJECT_NOT_FOUND,
            ErrorCode.COMPILATION_FAILED,
            ErrorCode.RESULT_TOO_LARGE,
        ],
    )
    def test_repairable_failures_are_retryable(self, code):
        """`retryable` means "a *corrected* request could succeed".

        Not "transient". It drives the agent's SQL repair loop, so the question it
        answers is "would rewriting the query help", and the name reads like the
        HTTP sense of retry -- which is why this is pinned by a test.
        """
        assert VannaError(code, "x").retryable

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.PERMISSION_DENIED,
            ErrorCode.QUOTA_EXCEEDED,
            ErrorCode.RATE_LIMITED,
            ErrorCode.LLM_UNAVAILABLE,
            ErrorCode.DATABASE_UNAVAILABLE,
            ErrorCode.INTERNAL_ERROR,
        ],
    )
    def test_nothing_a_rewrite_cannot_fix_is_retryable(self, code):
        # A repair loop that retries a permission denial turns one refusal into a
        # dozen, and looks like an access probe to whoever reads the audit log. A
        # loop that retries a rate limit makes the rate limit worse.
        assert not VannaError(code, "x").retryable


class TestFromException:
    def test_an_unknown_exception_becomes_an_internal_error(self):
        error = VannaError.from_exception(ValueError("boom"))
        assert error.code == ErrorCode.INTERNAL_ERROR
        assert not error.is_user_error

    def test_the_phase_is_carried_through(self):
        error = VannaError.from_exception(
            ValueError("boom"), phase=ErrorPhase.SQL_EXECUTION
        )
        assert error.phase == ErrorPhase.SQL_EXECUTION

    def test_an_explicit_code_wins(self):
        error = VannaError.from_exception(ValueError("boom"), code=ErrorCode.INVALID_SQL)
        assert error.code == ErrorCode.INVALID_SQL

    def test_a_vanna_error_passes_through_unchanged(self):
        original = VannaError(ErrorCode.INVALID_SQL, "boom", hint="fix it")
        assert VannaError.from_exception(original) is original

    def test_the_original_is_kept(self):
        """The traceback has to survive, or an internal error has no diagnosis."""
        cause = ValueError("boom")
        error = VannaError.from_exception(cause)
        assert cause in (getattr(error, "cause", None), error.__cause__)
