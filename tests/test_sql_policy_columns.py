"""Column authority in the SQL policy: the check that makes a grant unbypassable.

Before this existed, a column the caller could not read was *omitted from the
prompt* and nothing more. That is not a control. The model can name a column it
inferred from a question, a repair pass can reintroduce one it saw earlier in the
conversation, and ``/run-sql`` hands the validator SQL a person typed. Every one
of those paths reached the database with the column in it.

So the tests here are written from the attacker's side: none of this SQL came from
a model that was shown a filtered schema. It is what somebody writes when they
already know the column is there and want the value anyway.

Two properties are pinned throughout:

**Fail closed.** Anything the qualifier cannot resolve is refused. A reference we
cannot attribute to a table is not evidence that it was harmless.

**Planner-visible is validator-allowed.** The permissions here come from the same
resolution that filters the prompt, so the two cannot describe different worlds.
:class:`TestTheAllowlistMatchesWhatThePromptShowed` is that guarantee stated
directly -- if it ever fails, a bypass exists.
"""

from __future__ import annotations

import pytest

from vanna.core.sql_policy import SqlPolicy
from vanna.core.sql_policy.models import ViolationCode
from vanna.core.sql_policy.validator import SqlPolicyValidator

# What the caller may do with each column, as `read_guard.column_uses` reports it.
#   id      fully usable
#   total   readable and aggregatable, but not a legal predicate
#   status  readable and filterable, but not aggregatable
#   salary  granted nothing: invisible, and refused wherever it is named
CATALOG_COLUMNS = {
    "sales.orders": {
        "id": {"read", "filter", "aggregate"},
        "total": {"read", "aggregate"},
        "status": {"read", "filter"},
        "salary": set(),
    },
    "sales.customers": {
        "id": {"read", "filter", "aggregate"},
        "name": {"read", "filter"},
    },
}

# Both spellings, exactly as SqlPolicyToolRegistry._catalog_table_names emits them.
CATALOG_TABLES = ["orders", "sales.orders", "customers", "sales.customers"]


@pytest.fixture
def validate():
    validator = SqlPolicyValidator()
    policy = SqlPolicy.read_only()

    def run(sql: str):
        return validator.validate(
            sql,
            dialect="postgres",
            policy=policy,
            catalog_tables=CATALOG_TABLES,
            catalog_columns=CATALOG_COLUMNS,
        )

    return run


def codes(violations):
    return {v.code for v in violations}


class TestTheChecksAreOnByDefault:
    def test_read_only_requires_both_catalogs(self):
        """They defaulted to off, which is why the bypass existed at all."""
        policy = SqlPolicy.read_only()
        assert policy.require_catalog_tables is True
        assert policy.require_catalog_columns is True

    def test_permissive_still_turns_everything_off(self):
        policy = SqlPolicy.permissive()
        assert policy.require_catalog_tables is False
        assert policy.require_catalog_columns is False


class TestAnUngrantedColumnIsRefused:
    def test_selecting_it_directly(self, validate):
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate("SELECT salary FROM sales.orders")
        )

    def test_a_star_does_not_smuggle_it(self, validate):
        """``SELECT *`` is expanded rather than refused.

        Rejecting every star outright would be easy and would make the product
        worse; the qualifier expands it into real columns, so the ungranted one is
        caught and ordinary questions still work.
        """
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate("SELECT * FROM sales.orders")
        )

    def test_a_star_is_rewritten_to_the_permitted_columns(self):
        """The case this suite originally got wrong, and the leak it hid.

        In production a revoked column is *absent* from the catalog, not present
        with an empty permission set -- ``GrantFilteredCatalog`` drops it before
        the policy ever sees it. Model it that way and validation passes, because
        the qualifier expands ``*`` using the filtered catalog and every column it
        produces is permitted.

        The database is not so obliging. Handed the original text it expands the
        same star against the *physical* table and returns the revoked column with
        its values, in a query the policy had just approved. Verified against a
        real workspace before the fix: ``SELECT * FROM chinook.invoice`` came back
        with ``total`` in it seconds after ``can_read`` was revoked.

        So the statement that executes is the expanded one.
        """
        validator = SqlPolicyValidator()
        filtered = {"sales.orders": {"id": {"read"}, "status": {"read", "filter"}}}

        assert (
            validator.validate(
                "SELECT * FROM sales.orders",
                dialect="postgres",
                policy=SqlPolicy.read_only(),
                catalog_tables=["orders", "sales.orders"],
                catalog_columns=filtered,
            )
            == []
        )

        rewritten = validator.expand_stars(
            "SELECT * FROM sales.orders", dialect="postgres", catalog_columns=filtered
        )
        assert rewritten is not None
        assert "*" not in rewritten
        assert "id" in rewritten and "status" in rewritten
        assert "salary" not in rewritten

    def test_a_statement_without_a_star_is_left_alone(self):
        """Only stars are rewritten.

        Putting the qualifier's output in front of users for every query would be
        a far larger behavioural change than this needs, and buys nothing: a
        statement that names its columns already says what the validator checked.
        """
        validator = SqlPolicyValidator()
        assert (
            validator.expand_stars(
                "SELECT id FROM sales.orders",
                dialect="postgres",
                catalog_columns=CATALOG_COLUMNS,
            )
            is None
        )

    def test_an_alias_does_not_hide_it(self, validate):
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate("SELECT o.salary FROM sales.orders o")
        )

    def test_an_unqualified_table_name_does_not_hide_it(self, validate):
        """The bypass this check shipped with, found by testing rather than review.

        The catalog is keyed ``sales.orders``; the query says ``orders``. The
        lookup missed, the code fell through to "the table check will report it",
        and the table check accepts a bare name because the catalog lists one. Net
        effect: deleting one word from the query disabled column enforcement.
        """
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate("SELECT salary FROM orders")
        )

    def test_a_cte_does_not_launder_it(self, validate):
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate("WITH x AS (SELECT salary FROM sales.orders) SELECT * FROM x")
        )

    def test_a_subquery_does_not_launder_it(self, validate):
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate(
                "SELECT (SELECT MAX(salary) FROM sales.orders) AS m "
                "FROM sales.customers"
            )
        )

    def test_a_union_arm_is_checked_too(self, validate):
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate(
                "SELECT id FROM sales.orders UNION SELECT salary FROM sales.orders"
            )
        )

    def test_wrapping_it_in_a_function_does_not_help(self, validate):
        """Ancestors are walked, not just the parent.

        ``UPPER(salary)`` puts a function between the column and the clause, and a
        parent-only check sees the function and concludes nothing.
        """
        assert ViolationCode.COLUMN_NOT_ALLOWED in codes(
            validate("SELECT id FROM sales.orders WHERE UPPER(salary) = 'X'")
        )


class TestFilterAndAggregateAreSeparatePermissions:
    def test_filtering_on_a_readable_column_can_still_be_refused(self, validate):
        """``total`` is readable and aggregatable but not a legal predicate."""
        found = codes(validate("SELECT id FROM sales.orders WHERE total > 100"))
        assert ViolationCode.COLUMN_FILTER_NOT_ALLOWED in found
        assert ViolationCode.COLUMN_NOT_ALLOWED not in found

    def test_order_by_counts_as_filtering(self, validate):
        """Ordering by a column ranks every row by it."""
        assert ViolationCode.COLUMN_FILTER_NOT_ALLOWED in codes(
            validate("SELECT id FROM sales.orders ORDER BY total")
        )

    def test_a_join_predicate_counts_as_filtering(self, validate):
        assert ViolationCode.COLUMN_FILTER_NOT_ALLOWED in codes(
            validate(
                "SELECT o.id FROM sales.orders o "
                "JOIN sales.customers c ON o.total = c.id"
            )
        )

    def test_aggregating_a_filterable_column_can_still_be_refused(self, validate):
        """``status`` is readable and filterable but not aggregatable."""
        found = codes(validate("SELECT COUNT(DISTINCT status) FROM sales.orders"))
        assert ViolationCode.COLUMN_AGGREGATE_NOT_ALLOWED in found

    def test_group_by_counts_as_aggregating(self, validate):
        assert ViolationCode.COLUMN_AGGREGATE_NOT_ALLOWED in codes(
            validate("SELECT status FROM sales.orders GROUP BY status")
        )

    def test_distinct_on_counts_as_aggregating(self, validate):
        """``SELECT DISTINCT ON (x)`` partitions by a column without aggregating it.

        The reference implementation had to add ``Distinct`` for exactly this, and
        a context set holding only AggFunc and Group lets it through.
        """
        assert ViolationCode.COLUMN_AGGREGATE_NOT_ALLOWED in codes(
            validate("SELECT DISTINCT ON (status) id FROM sales.orders")
        )

    def test_a_window_partition_counts_as_aggregating(self, validate):
        """``OVER (PARTITION BY x)`` is the other escape the same set closes."""
        assert ViolationCode.COLUMN_AGGREGATE_NOT_ALLOWED in codes(
            validate("SELECT SUM(total) OVER (PARTITION BY status) FROM sales.orders")
        )

    def test_having_needs_both(self, validate):
        """``HAVING SUM(total) > 5`` aggregates *and* filters."""
        found = codes(
            validate(
                "SELECT id FROM sales.orders GROUP BY id HAVING SUM(total) > 5"
            )
        )
        assert ViolationCode.COLUMN_FILTER_NOT_ALLOWED in found


class TestOrdinaryQueriesStillWork:
    """The check is worthless if it refuses the questions people actually ask."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM sales.orders",
            "SELECT id FROM orders",
            "SELECT o.id FROM sales.orders o",
            "SELECT SUM(total) FROM sales.orders",
            "SELECT id FROM sales.orders WHERE status = 'A'",
            # HAVING on a *filterable* column. `HAVING SUM(total) > 5` is
            # deliberately refused instead -- see the filter tests -- because
            # `total` may be aggregated but not used as a predicate.
            "SELECT id, SUM(total) FROM sales.orders GROUP BY id HAVING COUNT(*) > 5",
            "SELECT o.id, c.name FROM sales.orders o "
            "JOIN sales.customers c ON o.id = c.id",
            "WITH recent AS (SELECT id, status FROM sales.orders) "
            "SELECT status FROM recent",
        ],
    )
    def test_allowed(self, validate, sql):
        assert validate(sql) == []


class TestFailClosed:
    def test_an_unknown_column_is_refused(self, validate):
        found = codes(validate("SELECT nonexistent FROM sales.orders"))
        assert found & {
            ViolationCode.COLUMN_NOT_IN_CATALOG,
            ViolationCode.COLUMN_UNRESOLVED,
        }

    def test_an_unknown_table_is_refused(self, validate):
        assert ViolationCode.TABLE_NOT_IN_CATALOG in codes(
            validate("SELECT id FROM sales.nope")
        )

    def test_a_column_that_cannot_be_attributed_is_refused(self):
        """Two tables, an ambiguous bare column, and no way to pick.

        Guessing would apply one table's permissions to the other's column.
        """
        validator = SqlPolicyValidator()
        found = validator.validate(
            "SELECT id FROM sales.orders, sales.customers",
            dialect="postgres",
            policy=SqlPolicy.read_only(),
            catalog_tables=CATALOG_TABLES,
            catalog_columns=CATALOG_COLUMNS,
        )
        assert found != []

    def test_an_ambiguous_bare_table_name_resolves_to_nothing(self):
        """Two schemas can each hold an ``orders``.

        Mapping the bare name to either one would apply the wrong table's
        permissions, so it maps to neither and the query is refused.
        """
        catalog = {
            "sales.orders": {"id": {"read"}},
            "hr.orders": {"id": {"read"}},
        }
        validator = SqlPolicyValidator()
        found = validator.validate(
            "SELECT id FROM orders",
            dialect="postgres",
            policy=SqlPolicy.read_only(),
            catalog_tables=["orders", "sales.orders", "hr.orders"],
            catalog_columns=catalog,
        )
        assert found != []


class TestTheAllowlistMatchesWhatThePromptShowed:
    """planner-visible is a subset of validator-allowed.

    The one invariant worth stating on its own. The prompt and the validator are
    built from the same resolution, so a column the model was shown must be
    queryable and a column it was not shown must not be. If these ever disagree,
    either the model is being refused things it was told it could use -- which
    reads as a broken product -- or it is being allowed things it was not, which
    is a bypass.
    """

    def test_every_visible_column_is_queryable(self, validate):
        for table, columns in CATALOG_COLUMNS.items():
            for column, uses in columns.items():
                if "read" not in uses:
                    continue
                assert validate(f"SELECT {column} FROM {table}") == [], (
                    f"{table}.{column} is in the prompt but the validator refuses it"
                )

    def test_every_hidden_column_is_refused(self, validate):
        for table, columns in CATALOG_COLUMNS.items():
            for column, uses in columns.items():
                if "read" in uses:
                    continue
                assert validate(f"SELECT {column} FROM {table}") != [], (
                    f"{table}.{column} is hidden from the prompt but allowed"
                )
