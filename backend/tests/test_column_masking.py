"""Column masking: how it resolves across roles, and what SQL it produces.

Masking is the weakest of the three things that can happen to a column -- weaker
than dropping it, which is still the default and the recommendation. It exists
because the alternative people reach for is granting the column outright and
hoping.

Two properties are worth a test each, and both are places a plausible
implementation goes quietly wrong:

* **Adding a role can only widen.** Masks are the one grant flag that is not a
  boolean, so "union" has to mean "the most revealing", not "the last one seen".
* **A dialect with no hash function must not fall back to a truncation.** A mask
  that reveals more than its name promises is worse than one that reveals less.
"""

from __future__ import annotations

import pytest

from vanna.core.access.rules import mask_expression
from vanna.core.grants.models import MASK_STRATEGIES, ColumnGrant, TableGrant
from vanna.core.grants.resolve import resolve_grants


def _column(role: str, *, read: bool = True, mask: str = "none") -> ColumnGrant:
    return ColumnGrant(
        data_source_id="warehouse",
        role=role,
        table="customers",
        column="email",
        can_read=read,
        mask=mask,
    )


def _table(role: str) -> TableGrant:
    return TableGrant(
        data_source_id="warehouse", role=role, table="customers", can_select=True
    )


def _resolve(roles, table_grants, column_grants):
    return resolve_grants(
        data_source_id="warehouse",
        roles=roles,
        table_grants=table_grants,
        column_grants=column_grants,
    )


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------


def test_a_single_role_carries_its_mask() -> None:
    effective = _resolve(["analyst"], [_table("analyst")], [_column("analyst", mask="hash")])
    column = effective.tables["customers"].column("email")
    assert column is not None
    assert column.mask == "hash"
    assert column.is_masked


def test_holding_a_second_role_can_only_widen() -> None:
    """The union rule, applied to masks.

    `analyst` sees the address hashed; `support` sees it in the clear. Somebody
    holding both must see it in the clear -- otherwise *gaining* a role takes
    something away, which is not a permission model anybody can reason about.
    """
    effective = _resolve(
        ["analyst", "support"],
        [_table("analyst"), _table("support")],
        [_column("analyst", mask="hash"), _column("support", mask="none")],
    )
    assert effective.tables["customers"].column("email").mask == "none"


def test_the_widening_is_order_independent() -> None:
    """"Most revealing wins", not "last one seen".

    An implementation that simply assigns on each merge passes the previous test
    for one ordering and fails for the other.
    """
    forward = _resolve(
        ["a", "b"],
        [_table("a"), _table("b")],
        [_column("a", mask="null"), _column("b", mask="partial")],
    )
    backward = _resolve(
        ["a", "b"],
        [_table("a"), _table("b")],
        [_column("b", mask="partial"), _column("a", mask="null")],
    )
    assert forward.tables["customers"].column("email").mask == "partial"
    assert backward.tables["customers"].column("email").mask == "partial"


def test_a_role_that_cannot_read_does_not_vote_on_the_mask() -> None:
    """A `can_read=False` row says nothing, and must not say "in the clear".

    Its `mask` defaults to "none", so a resolver that let every row vote would
    unmask the column for anybody who also happened to hold that role -- turning
    a *withheld* column into a fully readable one.
    """
    effective = _resolve(
        ["analyst", "auditor"],
        [_table("analyst"), _table("auditor")],
        [
            _column("analyst", mask="hash"),
            _column("auditor", read=False, mask="none"),
        ],
    )
    assert effective.tables["customers"].column("email").mask == "hash"


def test_masking_never_resurrects_a_withheld_column() -> None:
    """Dropping still wins. A mask applies only to a column that survives.

    `can_read=False` everywhere means the column is not in the caller's world at
    all, whatever any mask says.
    """
    effective = _resolve(
        ["analyst"], [_table("analyst")], [_column("analyst", read=False, mask="partial")]
    )
    assert effective.tables.get("customers") is None or (
        effective.tables["customers"].column("email") is None
    )


# ----------------------------------------------------------------------
# The SQL
# ----------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["postgres", "mysql", "duckdb", "clickhouse", "oracle"])
def test_hash_is_a_real_hash_where_one_exists(dialect: str) -> None:
    sql = mask_expression("email", "hash", dialect).lower()
    assert "md5" in sql or "hash" in sql


def test_sqlite_hash_reveals_nothing_rather_than_truncating() -> None:
    """SQLite ships no hash function.

    The tempting fallback is a truncation, which reveals the first characters --
    *more* than `hash` promises. A mask that quietly reveals more than it says is
    the failure this whole module is trying to avoid, so it degrades to a
    constant instead.
    """
    sql = mask_expression("email", "hash", "sqlite")
    assert sql == "'[hashed]'"
    assert "substr" not in sql.lower()


def test_an_unknown_strategy_masks_to_null() -> None:
    """The safe reading of a bug in this file is "reveal nothing"."""
    assert "NULL" in mask_expression("email", "not-a-strategy", "postgres")


def test_none_is_the_column_itself() -> None:
    assert mask_expression("email", "none", "postgres") == "email"
    assert mask_expression("email", "", "postgres") == "email"


def test_a_mask_wraps_whatever_expression_was_there() -> None:
    """Columns can be renames or calculations, not just bare names.

    Assuming a bare column name produces invalid SQL for a calculated column --
    and it would fail at execution, long after the grant looked correct.
    """
    sql = mask_expression("lower(first_name || last_name)", "partial", "postgres")
    assert "lower(first_name || last_name)" in sql
    assert "***" in sql


def test_every_declared_strategy_produces_sql() -> None:
    """A strategy the model offers but the SQL layer cannot spell is a rule that
    silently does nothing."""
    for strategy in MASK_STRATEGIES:
        assert mask_expression("email", strategy, "postgres")
