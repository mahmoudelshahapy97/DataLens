"""The typed write plan, the policy it is checked against, and the SQL it becomes.

Two layers are tested separately because they fail differently, and the
distinction is the whole design:

* **Unrepresentable** -- the shape cannot be constructed at all. No policy is
  consulted, so no policy bug can let it through.
* **Refused** -- the shape is legal but this caller may not do it. A coded
  refusal, checked against what they were actually granted.

If a case moves from the first list to the second, that is a regression even if
the test still passes: it means a guarantee became a check.
"""

import pytest
from pydantic import ValidationError

from vanna.capabilities.schema_catalog import ColumnMetadata, ForeignKey, TableMetadata
from vanna.core.grants import ColumnGrant, TableGrant, catalog_write_facts, resolve_grants
from vanna.core.write import (
    ColumnAssignment,
    KeyPredicate,
    StepReference,
    WriteCode,
    WritePlan,
    WriteRefusal,
    WriteStep,
    build_write_policy,
    validate_write_plan,
)

A, K, R, S = ColumnAssignment, KeyPredicate, StepReference, WriteStep


@pytest.fixture
def catalog():
    return [
        TableMetadata(table_name="orders", schema_name="sales", columns=[
            ColumnMetadata(name="order_id", nullable=False, is_primary_key=True,
                           is_generated=True),
            ColumnMetadata(name="status", nullable=False),
            ColumnMetadata(name="customer", nullable=False),
            ColumnMetadata(name="note", nullable=True),
            ColumnMetadata(name="created", nullable=False, has_default=True),
        ]),
        TableMetadata(table_name="order_items", schema_name="sales", columns=[
            ColumnMetadata(name="item_id", nullable=False, is_primary_key=True,
                           is_generated=True),
            ColumnMetadata(name="order_id", nullable=False, foreign_key=ForeignKey(
                column="order_id", references_table="sales.orders",
                references_column="order_id")),
            ColumnMetadata(name="sku", nullable=False),
        ]),
        # Composite key: a partial predicate must be refused.
        TableMetadata(table_name="stock", schema_name="sales", columns=[
            ColumnMetadata(name="warehouse", nullable=False, is_primary_key=True),
            ColumnMetadata(name="sku", nullable=False, is_primary_key=True),
            ColumnMetadata(name="quantity", nullable=False),
        ]),
    ]


def grants_for(catalog, **verbs):
    tables, columns = [], []
    for table in catalog:
        qualified = f"{table.schema_name}.{table.table_name}"
        tables.append(TableGrant(
            data_source_id="wh", role="admin", table=qualified, can_select=True, **verbs))
        for column in table.columns:
            columns.append(ColumnGrant(
                data_source_id="wh", role="admin", table=qualified, column=column.name,
                can_read=True, can_write=not column.is_generated))
    keys, unassignable = catalog_write_facts(catalog)
    return resolve_grants(
        data_source_id="wh", roles=["admin"], table_grants=tables,
        column_grants=columns, version=11,
        key_columns=keys, unassignable_columns=unassignable)


@pytest.fixture
def policy(catalog):
    grants = grants_for(catalog, can_insert=True, can_update=True, can_delete=True)
    return build_write_policy(grants, catalog, dialect="postgres", max_rows=50)


def update(**kw):
    kw.setdefault("assignments", [A(column="status", value="shipped")])
    kw.setdefault("predicate", [K(column="order_id", value=1)])
    kw.setdefault("expected_row_count", 1)
    return WritePlan(steps=[S(operation="update", table="orders", **kw)])


class TestUnrepresentableShapes:
    """Dangerous statements are not rejected -- they cannot be built."""

    def test_update_without_a_predicate(self):
        with pytest.raises(ValidationError, match="every row"):
            WritePlan(steps=[S(operation="update", table="orders",
                               assignments=[A(column="status", value="x")],
                               expected_row_count=1)])

    def test_delete_without_a_predicate(self):
        with pytest.raises(ValidationError, match="empties the table"):
            WritePlan(steps=[S(operation="delete", table="orders", expected_row_count=1)])

    @pytest.mark.parametrize("verb", ["drop", "truncate", "create", "alter", "grant"])
    def test_ddl_has_no_representation(self, verb):
        with pytest.raises(ValidationError):
            WritePlan(steps=[S(operation=verb, table="orders", expected_row_count=0)])

    def test_an_assignment_is_a_value_or_a_reference_never_both(self):
        with pytest.raises(ValidationError, match="never both"):
            A(column="q", value=1, reference=R(from_step=0, column="id"))

    def test_insert_cannot_carry_a_predicate(self):
        with pytest.raises(ValidationError, match="insert cannot carry a predicate"):
            WritePlan(steps=[S(operation="insert", table="orders",
                               assignments=[A(column="status", value="x")],
                               predicate=[K(column="order_id", value=1)],
                               expected_row_count=1)])

    def test_a_column_cannot_be_assigned_twice(self):
        with pytest.raises(ValidationError, match="assigned twice"):
            WritePlan(steps=[S(operation="update", table="orders",
                               assignments=[A(column="status", value="a"),
                                            A(column="Status", value="b")],
                               predicate=[K(column="order_id", value=1)],
                               expected_row_count=1)])

    def test_references_cannot_point_forward(self):
        with pytest.raises(ValidationError, match="does not run before it"):
            WritePlan(steps=[
                S(operation="insert", table="order_items", assignments=[
                    A(column="order_id", reference=R(from_step=1, column="order_id")),
                    A(column="sku", value="X")], expected_row_count=1),
                S(operation="insert", table="orders", assignments=[
                    A(column="status", value="new"), A(column="customer", value="c")],
                  expected_row_count=1)])

    def test_references_cannot_point_at_a_multi_row_insert(self):
        """Two parent rows means two candidate keys and no rule for choosing."""
        with pytest.raises(ValidationError, match="rather than exactly one"):
            WritePlan(steps=[
                S(operation="insert", table="orders", assignments=[
                    A(column="status", value="new"), A(column="customer", value="c")],
                  expected_row_count=3),
                S(operation="insert", table="order_items", assignments=[
                    A(column="order_id", reference=R(from_step=0, column="order_id")),
                    A(column="sku", value="X")], expected_row_count=1)])

    def test_an_unknown_field_is_rejected_not_ignored(self):
        """A model inventing a `confirm` flag is told, not silently overruled."""
        with pytest.raises(ValidationError, match="extra_forbidden|not permitted"):
            WritePlan.model_validate({"confirm": True, "steps": [
                {"operation": "delete", "table": "orders",
                 "predicate": [{"column": "order_id", "value": 1}],
                 "expected_row_count": 1}]})


class TestPolicyRefusals:
    @pytest.mark.parametrize("plan_factory,expected", [
        (lambda: update(predicate=[K(column="customer", value="acme")]),
         WriteCode.PREDICATE_NOT_KEY),
        (lambda: update(assignments=[A(column="order_id", value=9)]),
         WriteCode.COLUMN_NOT_ALLOWED),
        (lambda: update(assignments=[A(column="nope", value="x")]),
         WriteCode.COLUMN_UNKNOWN),
        (lambda: update(expected_row_count=9999), WriteCode.ROW_LIMIT_EXCEEDED),
        (lambda: WritePlan(steps=[S(operation="insert", table="orders",
                                    assignments=[A(column="status", value="new")],
                                    expected_row_count=1)]),
         WriteCode.MISSING_REQUIRED_COLUMN),
        (lambda: WritePlan(steps=[S(operation="delete", table="payroll",
                                    predicate=[K(column="id", value=1)],
                                    expected_row_count=1)]),
         WriteCode.TABLE_NOT_ALLOWED),
    ])
    def test_refusal_codes(self, policy, plan_factory, expected):
        with pytest.raises(WriteRefusal) as caught:
            validate_write_plan(plan_factory(), policy)
        assert caught.value.code is expected

    def test_partial_composite_key_is_refused(self, policy):
        """A subset of a composite key matches every row sharing it.

        The reference implementation this was ported from raised this refusal
        with a code missing from its own vocabulary, turning the check into a
        500. Asserting the code here is what keeps that fixed.
        """
        plan = WritePlan(steps=[S(operation="update", table="stock",
                                  assignments=[A(column="quantity", value=5)],
                                  predicate=[K(column="sku", value="WIDGET")],
                                  expected_row_count=1)])
        with pytest.raises(WriteRefusal) as caught:
            validate_write_plan(plan, policy)
        assert caught.value.code is WriteCode.PREDICATE_INCOMPLETE_KEY

    def test_complete_composite_key_is_accepted(self, policy):
        plan = WritePlan(steps=[S(operation="update", table="stock",
                                  assignments=[A(column="quantity", value=5)],
                                  predicate=[K(column="warehouse", value="W1"),
                                             K(column="sku", value="WIDGET")],
                                  expected_row_count=1)])
        assert validate_write_plan(plan, policy).steps[0].parameters == [5, "W1", "WIDGET"]

    def test_verb_must_be_granted(self, catalog):
        policy = build_write_policy(
            grants_for(catalog, can_update=True), catalog,
            dialect="postgres", max_rows=50)
        plan = WritePlan(steps=[S(operation="delete", table="orders",
                                  predicate=[K(column="order_id", value=1)],
                                  expected_row_count=1)])
        with pytest.raises(WriteRefusal) as caught:
            validate_write_plan(plan, policy)
        assert caught.value.code is WriteCode.OPERATION_NOT_ALLOWED

    def test_nothing_writable_is_a_refusal_not_a_crash(self, catalog):
        policy = build_write_policy(
            grants_for(catalog), catalog, dialect="postgres", max_rows=50)
        assert policy.is_empty
        with pytest.raises(WriteRefusal) as caught:
            validate_write_plan(update(), policy)
        assert caught.value.code is WriteCode.NOT_ENABLED

    def test_an_unknown_refusal_code_cannot_be_raised(self):
        """The constructor is strict so a typo fails loudly, not as a 500."""
        with pytest.raises(ValueError, match="unknown write refusal code"):
            WriteRefusal("write_nonsense", "boom")


class TestRendering:
    @pytest.mark.parametrize("dialect,paramstyle,expected", [
        ("postgres", "format",
         'UPDATE "sales"."orders" SET "status" = %s WHERE "order_id" = %s'),
        ("mysql", "format",
         "UPDATE `sales`.`orders` SET `status` = %s WHERE `order_id` = %s"),
        ("tsql", "qmark",
         "UPDATE [sales].[orders] SET [status] = ? WHERE [order_id] = ?"),
        ("sqlite", "qmark",
         'UPDATE "sales"."orders" SET "status" = ? WHERE "order_id" = ?'),
    ])
    def test_quoting_follows_the_dialect_and_markers_follow_the_driver(
        self, catalog, dialect, paramstyle, expected
    ):
        """Two separate concerns: pymysql wants %s where MySQL's syntax writes ?."""
        grants = grants_for(catalog, can_update=True)
        policy = build_write_policy(grants, catalog, dialect=dialect, max_rows=50)
        step = validate_write_plan(update(), policy, paramstyle=paramstyle).steps[0]
        assert step.sql == expected
        assert step.parameters == ["shipped", 1]

    def test_values_never_appear_in_the_sql(self, policy):
        validated = validate_write_plan(
            update(assignments=[A(column="note", value="Robert'); DROP TABLE orders;--")]),
            policy)
        assert "DROP" not in validated.statement_preview
        assert validated.steps[0].parameters[0].startswith("Robert')")

    def test_parameters_are_ordered_assignments_then_predicate(self, policy):
        step = validate_write_plan(
            update(assignments=[A(column="status", value="s"), A(column="note", value="n")],
                   predicate=[K(column="order_id", value=7)]), policy).steps[0]
        assert step.parameters == ["s", "n", 7]

    def test_a_multi_step_transaction_binds_the_generated_key(self, policy):
        plan = WritePlan(steps=[
            S(operation="insert", table="orders", assignments=[
                A(column="status", value="new"), A(column="customer", value="acme")],
              expected_row_count=1),
            S(operation="insert", table="order_items", assignments=[
                A(column="order_id", reference=R(from_step=0, column="order_id")),
                A(column="sku", value="WIDGET")], expected_row_count=1)])
        validated = validate_write_plan(plan, policy)

        parent, child = validated.steps
        assert parent.returning_columns == ["order_id"]
        assert parent.sql.endswith('RETURNING "order_id"')
        # The bound slot is reserved with None, and a binding says where to fill it.
        assert child.parameters == [None, "WIDGET"]
        assert (child.bindings[0].position, child.bindings[0].from_step) == (1, 0)
        assert validated.operation == "transaction"

    def test_a_reference_needs_a_dialect_that_can_return_a_key(self, catalog):
        policy = build_write_policy(
            grants_for(catalog, can_insert=True), catalog, dialect="mysql", max_rows=50)
        plan = WritePlan(steps=[
            S(operation="insert", table="orders", assignments=[
                A(column="status", value="new"), A(column="customer", value="acme")],
              expected_row_count=1),
            S(operation="insert", table="order_items", assignments=[
                A(column="order_id", reference=R(from_step=0, column="order_id")),
                A(column="sku", value="W")], expected_row_count=1)])
        with pytest.raises(WriteRefusal) as caught:
            validate_write_plan(plan, policy)
        assert caught.value.code is WriteCode.RETURNING_UNSUPPORTED


class TestPlanHash:
    def test_covers_bound_values_not_just_sql(self, policy):
        """`SET city = %s` approved for Berlin must not execute with Bochum."""
        berlin = validate_write_plan(
            update(assignments=[A(column="note", value="Berlin")]), policy)
        bochum = validate_write_plan(
            update(assignments=[A(column="note", value="Bochum")]), policy)
        assert berlin.steps[0].sql == bochum.steps[0].sql
        assert berlin.plan_hash != bochum.plan_hash

    def test_covers_the_promised_row_count(self, policy):
        one = validate_write_plan(update(expected_row_count=1), policy)
        two = validate_write_plan(update(expected_row_count=2), policy)
        assert one.plan_hash != two.plan_hash

    def test_is_stable_for_an_identical_plan(self, policy):
        assert validate_write_plan(update(), policy).plan_hash == \
            validate_write_plan(update(), policy).plan_hash


class TestDestructiveClassification:
    """Warn about what deserves warning, so the warning keeps meaning something."""

    @pytest.mark.parametrize("factory,destructive", [
        (lambda: update(expected_row_count=1), False),
        (lambda: update(expected_row_count=5), True),
        (lambda: WritePlan(steps=[S(operation="delete", table="orders",
                                    predicate=[K(column="order_id", value=1)],
                                    expected_row_count=1)]), True),
        (lambda: WritePlan(steps=[S(operation="insert", table="orders",
                                    assignments=[A(column="status", value="n"),
                                                 A(column="customer", value="c")],
                                    expected_row_count=1)]), False),
    ])
    def test_classification(self, policy, factory, destructive):
        assert validate_write_plan(factory(), policy).is_destructive is destructive


class TestPreviewSafety:
    def test_preview_and_summary_carry_no_tenant_data(self, policy):
        """Both are stored and audited, so neither may contain a bound value."""
        validated = validate_write_plan(
            update(assignments=[A(column="customer", value="Contoso Ltd")]), policy)
        assert "Contoso" not in validated.statement_preview
        assert "Contoso" not in str(validated.parameter_summary)
        assert validated.parameter_summary["count"] == 2
