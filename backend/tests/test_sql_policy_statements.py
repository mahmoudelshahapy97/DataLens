"""What counts as "a statement" when the policy counts them.

One call carries one statement: ``SELECT 1; DROP TABLE users`` must be refused
whatever the mode, and that rule is not negotiable. The bug was in the counting.

sqlglot emits a bare ``Semicolon`` node when anything follows the final
terminator, so ``SELECT ...;`` with a trailing ``-- note`` parses as
``[Select, Semicolon]``. The policy counted the punctuation and refused the
query twice over:

    The query contains 2 statements. Only one statement may be executed per call.
    SEMICOLON statements are not permitted by the current policy.

Neither sentence describes anything the author did wrong, and a semicolon
followed by a comment is what a language model writes when asked to explain its
SQL. The World workspace failed on it in the browser, having generated perfectly
good SQL.

These tests pin both directions: punctuation is not a statement, and a genuinely
stacked statement is still refused.
"""

from __future__ import annotations

import pytest

from vanna.core.sql_policy import SqlPolicy
from vanna.core.sql_policy.validator import SqlPolicyValidator, ViolationCode


@pytest.fixture
def validator():
    return SqlPolicyValidator()


def _codes(violations) -> set:
    return {v.code for v in violations}


class TestTrailingPunctuation:
    """Each of these is one statement, whatever sqlglot's node list says."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1;",
            "SELECT 1;\n",
            "SELECT 1;  \n\n",
            "SELECT 1;;",
            "SELECT 1; ;",
            # The shape that actually failed: a terminator, then a comment.
            "SELECT name FROM country;\n-- ordered by population",
            "SELECT name FROM country; -- one row per country",
            "SELECT\n  name,  -- country name\n  code\nFROM country;\n-- iso codes",
            "SELECT 1;\n/* a block comment after the end */",
        ],
    )
    def test_a_terminated_query_is_one_statement(self, validator, sql: str):
        violations = validator.validate(sql, dialect="postgres")
        assert ViolationCode.MULTIPLE_STATEMENTS not in _codes(violations), (
            f"{sql!r} was counted as more than one statement"
        )
        assert violations == [], f"{sql!r} was refused: {[v.message for v in violations]}"


class TestStackingIsStillRefused:
    """The rule the counting exists to enforce, unchanged."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; SELECT 2",
            "SELECT 1; DROP TABLE users",
            "SELECT 1;\nDELETE FROM users;",
            "SELECT 1; -- harmless\nSELECT 2",
        ],
    )
    def test_two_real_statements_are_refused(self, validator, sql: str):
        codes = _codes(validator.validate(sql, dialect="postgres"))
        assert ViolationCode.MULTIPLE_STATEMENTS in codes, (
            f"{sql!r} stacks two statements and must be refused"
        )

    def test_the_second_statement_is_still_inspected(self):
        """Refusing on the count alone would stop looking at what was stacked.

        A caller running under a policy that permits writes gets no
        MULTIPLE_STATEMENTS-only pass: the DROP has to be seen too.
        """
        validator = SqlPolicyValidator()
        codes = _codes(
            validator.validate("SELECT 1; DROP TABLE users", dialect="postgres")
        )
        assert ViolationCode.MULTIPLE_STATEMENTS in codes
        assert len(codes) > 1, "the stacked DROP was never examined"


class TestNothingToRun:
    @pytest.mark.parametrize("sql", ["", "   ", ";", ";;", "-- just a comment"])
    def test_a_query_with_no_statement_is_refused(self, validator, sql: str):
        """Dropping the semicolon node must not turn punctuation into a pass.

        With the node filtered out, `;` parses to an empty list. Falling through
        that would index off the end, or worse, return "no violations" for a
        string containing no query.
        """
        codes = _codes(validator.validate(sql, dialect="postgres"))
        assert codes, f"{sql!r} contains no statement and must not validate clean"
        assert ViolationCode.UNPARSEABLE in codes


class TestTheRefusalNamesSomethingReal:
    """A refusal the author cannot act on is barely better than a crash.

    The label came from ``type(func).key``, which is the sqlglot class name run
    together. Asking the Booking workspace about availability produced:

        The function 'explodinggenerateseries' may not be used as a query source.

    There is no such function. It cannot be looked up, grepped for, or connected
    to the ``generate_series`` that was actually written -- and the query is
    refused, so the message is all the author gets.
    """

    @pytest.mark.parametrize(
        "sql,dialect,expected",
        [
            ("SELECT * FROM generate_series(1, 10) g", "postgres", "generate_series"),
            ("SELECT * FROM generate_series(1, 10)", "duckdb", "generate_series"),
            ("SELECT * FROM read_csv('/etc/passwd')", "duckdb", "read_csv"),
        ],
    )
    def test_the_name_is_the_one_that_was_written(self, sql, dialect, expected):
        messages = " ".join(
            v.message for v in SqlPolicyValidator().validate(sql, dialect=dialect)
        )
        assert messages, f"{sql!r} was expected to be refused"
        assert f"'{expected}'" in messages, messages

    def test_no_refusal_ever_prints_a_run_together_class_name(self):
        messages = " ".join(
            v.message
            for v in SqlPolicyValidator().validate(
                "SELECT * FROM generate_series(1, 10) g", dialect="postgres"
            )
        )
        assert "explodinggenerateseries" not in messages.lower()

    def test_an_argument_is_never_leaked_into_the_message(self):
        """`exp.ReadCSV.name` is the file path, so the label must not come from it."""
        violations = SqlPolicyValidator().validate(
            "SELECT * FROM read_csv('/etc/shadow')", dialect="duckdb"
        )
        blob = " ".join(f"{v.message} {v.detail or ''}" for v in violations)
        assert violations
        assert "/etc/shadow" not in blob, blob

    def test_the_opt_in_still_matches_the_function(self):
        """Renaming the label must not quietly detach it from the allow-list."""
        policy = SqlPolicy(allowed_source_functions=frozenset({"generate_series"}))
        assert SqlPolicyValidator().validate(
            "SELECT * FROM generate_series(1, 10) g", dialect="postgres", policy=policy
        ) == []


class TestTheSemanticValidatorAgrees:
    """The semantic layer has its own parse, and had the same bug."""

    def _reject(self, sql: str):
        from vanna.core.sql_policy.semantic import SemanticSqlPolicyToolRegistry

        registry = SemanticSqlPolicyToolRegistry(policy=SqlPolicy(), dialect="postgres")
        return registry._reject_disallowed(sql, policy=SqlPolicy(), tool_name="run_sql")

    def test_a_trailing_comment_is_not_a_second_statement(self):
        rejection = self._reject("SELECT name FROM countries;\n-- by population")
        assert rejection is None, getattr(rejection, "reason", rejection)

    def test_stacking_is_still_refused(self):
        assert self._reject("SELECT 1; DROP TABLE countries") is not None

    def test_punctuation_alone_does_not_crash(self):
        # `statements[0]` on an empty list is an IndexError, which the tool layer
        # would surface as "an unexpected error occurred". Nothing to run is the
        # compiler's error to report, so this stage passes it through.
        assert self._reject(";") is None
