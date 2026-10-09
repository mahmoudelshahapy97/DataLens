"""Which tables a statement actually reads.

Every case here is one a regex-based extractor gets wrong, and getting it wrong
is worse than missing an edge: an impact analysis that lists tables nobody uses
is one nobody trusts, and after the second wrong answer people stop opening it.

The graph itself is derived rather than recorded -- see the header of
`vanna_app/lineage.py` -- so there is no "did the recorder run" case to test. The
extraction is the part with judgement in it.
"""

from __future__ import annotations

import pytest

from vanna_app.lineage import tables_in


def test_a_plain_select() -> None:
    assert tables_in("SELECT * FROM orders") == {"orders"}


def test_a_join_finds_both_sides() -> None:
    assert tables_in(
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id"
    ) == {"orders", "customers"}


def test_a_cte_name_is_not_a_table() -> None:
    """The single most common regex failure.

    `WITH recent AS (...) SELECT * FROM recent` reads `orders`. Reporting
    `recent` puts a name in the graph that exists nowhere in the warehouse.
    """
    sql = """
        WITH recent AS (SELECT * FROM orders WHERE created_at > now() - interval '7 day')
        SELECT count(*) FROM recent
    """
    assert tables_in(sql) == {"orders"}


def test_nested_ctes_are_all_excluded() -> None:
    sql = """
        WITH a AS (SELECT * FROM orders),
             b AS (SELECT * FROM a JOIN customers USING (customer_id))
        SELECT * FROM b
    """
    assert tables_in(sql) == {"orders", "customers"}


def test_a_string_literal_is_not_a_table() -> None:
    """`FROM` inside a quoted string is text, not a reference."""
    assert tables_in("SELECT 'select * from secrets' AS note FROM orders") == {"orders"}


def test_a_subquery_is_walked() -> None:
    sql = "SELECT * FROM (SELECT customer_id FROM invoices) x JOIN customers USING (customer_id)"
    assert tables_in(sql) == {"invoices", "customers"}


def test_a_schema_qualified_name_keeps_its_schema() -> None:
    """`sales.orders` and `public.orders` are different tables.

    Merging them into one node draws an edge that does not exist.
    """
    assert tables_in("SELECT * FROM sales.orders") == {"sales.orders"}


def test_quoted_identifiers_are_unquoted() -> None:
    assert tables_in('SELECT * FROM "Order Items"') == {"order items"}


def test_a_union_finds_every_branch() -> None:
    assert tables_in(
        "SELECT id FROM orders UNION ALL SELECT id FROM archived_orders"
    ) == {"orders", "archived_orders"}


def test_case_is_normalised() -> None:
    assert tables_in("SELECT * FROM Orders") == tables_in("select * from ORDERS")


@pytest.mark.parametrize("sql", ["", "   ", None])
def test_empty_input_yields_nothing(sql) -> None:
    assert tables_in(sql or "") == set()


def test_unparseable_sql_yields_nothing_rather_than_raising() -> None:
    """Lineage is a reporting feature.

    One saved query somebody left half-written must not take the whole graph down
    with it -- which is what an exception here would do.
    """
    assert tables_in("SELECT FROM WHERE ((((") == set()
