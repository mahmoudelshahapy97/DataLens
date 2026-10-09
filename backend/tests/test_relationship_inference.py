"""Inferred relationships: joins guessed for databases that declare no foreign keys.

The shapes are Northwind's, the demo warehouse that declares none: its keys do
not follow its table names (`salesorder.custid` -> `customer.custid`), which is
why there are two naming rules rather than one. The scanner test runs the whole
path -- scan, infer, sample the data, store -- against a real SQLite file.
"""

from __future__ import annotations

import sqlite3

import pytest

from vanna.capabilities.schema_catalog import SchemaScanner
from vanna.capabilities.schema_catalog.inference import (
    ORPHANED_CONFIDENCE,
    VERIFIED_CONFIDENCE,
    containment_sql,
    infer_relationships,
    rescore,
    type_family,
)
from vanna.capabilities.schema_catalog.models import (
    INFERRED_MIN_CONFIDENCE,
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    TableMetadata,
)
from vanna.capabilities.schema_graph import build_schema_graph
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog
from vanna.integrations.sqlite import SqliteRunner


def _t(name: str, *cols: tuple, schema: str = "nw") -> TableMetadata:
    """cols: (name, type, is_pk)."""
    return TableMetadata(
        table_name=name,
        schema_name=schema,
        columns=[ColumnMetadata(name=n, data_type=t, is_primary_key=pk) for n, t, pk in cols],
    )


def _pairs(rels) -> set:
    return {(r.from_table, r.from_column, r.to_table, r.to_column) for r in rels}


class TestNamingRules:
    def test_stem_rule_matches_table_named_after_the_column(self):
        tables = [
            _t("product", ("productid", "integer", True), ("categoryid", "integer", False)),
            _t("category", ("categoryid", "serial", True)),
        ]
        assert _pairs(infer_relationships(tables)) == {
            ("nw.product", "categoryid", "nw.category", "categoryid")
        }

    def test_plural_table_and_plain_id_key(self):
        tables = [
            _t("orders", ("id", "integer", True), ("customer_id", "integer", False)),
            _t("customers", ("id", "integer", True)),
        ]
        (rel,) = infer_relationships(tables)
        assert (rel.to_table, rel.to_column) == ("nw.customers", "id")
        assert rel.confidence == pytest.approx(0.8)

    def test_key_name_rule_when_keys_ignore_table_names(self):
        """Northwind: nothing named `cust` exists, but `customer.custid` does."""
        tables = [
            _t("salesorder", ("orderid", "integer", True), ("custid", "integer", False),
               ("empid", "integer", False)),
            _t("customer", ("custid", "integer", True)),
            _t("employee", ("empid", "integer", True)),
            _t("orderdetail", ("orderid", "integer", True), ("productid", "integer", True)),
        ]
        assert _pairs(infer_relationships(tables)) == {
            ("nw.salesorder", "custid", "nw.customer", "custid"),
            ("nw.salesorder", "empid", "nw.employee", "empid"),
            # Composite key members reference too: orderdetail's key is not one column.
            ("nw.orderdetail", "orderid", "nw.salesorder", "orderid"),
        }

    def test_ambiguous_key_name_is_not_guessed(self):
        tables = [
            _t("a", ("ref", "integer", False), ("aid", "integer", True)),
            _t("b", ("ref", "integer", True)),
            _t("c", ("ref", "integer", True)),
        ]
        assert infer_relationships(tables) == []

    def test_bare_id_never_matches_by_key_name(self):
        tables = [_t("a", ("id", "integer", False)), _t("b", ("id", "integer", True))]
        assert infer_relationships(tables) == []

    def test_incompatible_types_disqualify(self):
        tables = [
            _t("orders", ("id", "integer", True), ("customer_id", "uuid", False)),
            _t("customers", ("id", "integer", True)),
        ]
        assert infer_relationships(tables) == []

    def test_unknown_type_costs_confidence(self):
        tables = [
            _t("orders", ("id", "integer", True), ("customer_id", "unknown", False)),
            _t("customers", ("customer_id", "integer", True)),
        ]
        (rel,) = infer_relationships(tables)
        assert rel.confidence == pytest.approx(0.75)

    def test_declared_foreign_keys_are_not_re_inferred(self):
        orders = _t("orders", ("id", "integer", True), ("customer_id", "integer", False))
        orders.columns[1].foreign_key = ForeignKey(
            column="customer_id", references_table="nw.customers", references_column="id"
        )
        tables = [orders, _t("customers", ("id", "integer", True))]
        assert infer_relationships(tables) == []

    def test_other_schemas_are_not_matched(self):
        tables = [
            _t("orders", ("id", "integer", True), ("customer_id", "integer", False)),
            _t("customers", ("id", "integer", True), schema="crm"),
        ]
        assert infer_relationships(tables) == []

    def test_a_tables_own_key_references_nothing(self):
        tables = [_t("customer", ("customer_id", "integer", True))]
        assert infer_relationships(tables) == []

    def test_everything_inferred_is_marked_so(self):
        tables = [
            _t("product", ("productid", "integer", True), ("categoryid", "integer", False)),
            _t("category", ("categoryid", "integer", True)),
        ]
        (rel,) = infer_relationships(tables, data_source_id="wh")
        assert rel.origin == "inferred"
        assert rel.review_status == "proposed"
        assert rel.data_source_id == "wh"

    @pytest.mark.parametrize(
        "data_type,family",
        [("INTEGER", "integer"), ("serial", "integer"), ("character varying", "text"),
         ("uuid", "uuid"), ("unknown", None), ("", None), ("bytea", "other")],
    )
    def test_type_family(self, data_type, family):
        assert type_family(data_type) == family


class TestRescore:
    def _rel(self, confidence: float = 0.85) -> RelationshipMetadata:
        return RelationshipMetadata(
            name="r", from_table="a", from_column="b_id", to_table="b", to_column="id",
            origin="inferred", confidence=confidence, review_status="proposed",
        )

    def test_clean_sample_raises_confidence(self):
        assert rescore(self._rel(), 200, 0).confidence == VERIFIED_CONFIDENCE

    def test_orphans_sink_it_below_use(self):
        scored = rescore(self._rel(), 200, 40)
        assert scored.confidence == ORPHANED_CONFIDENCE
        assert not scored.is_usable

    def test_a_few_orphans_leave_it_alone(self):
        assert rescore(self._rel(), 200, 5).confidence == 0.85

    def test_empty_sample_changes_nothing(self):
        assert rescore(self._rel(), 0, 0).confidence == 0.85

    def test_containment_sql_quotes_identifiers(self):
        sql = containment_sql(self._rel().model_copy(update={"from_table": 'we"ird.t'}))
        assert '"we""ird"."t"' in sql
        assert "LIMIT 200" in sql


class TestUsability:
    @pytest.mark.parametrize(
        "status,confidence,usable",
        [
            ("accepted", None, True),
            ("rejected", 0.99, False),
            ("proposed", INFERRED_MIN_CONFIDENCE, True),
            ("proposed", INFERRED_MIN_CONFIDENCE - 0.01, False),
            ("proposed", None, False),
        ],
    )
    def test_is_usable(self, status, confidence, usable):
        rel = RelationshipMetadata(
            name="r", from_table="a", from_column="x", to_table="b", to_column="y",
            origin="inferred", confidence=confidence, review_status=status,
        )
        assert rel.is_usable is usable

    def test_unconfirmed_edges_are_labelled_in_the_prompt(self):
        rel = RelationshipMetadata(
            name="r", from_table="a", from_column="x", to_table="b", to_column="y",
            origin="inferred", confidence=0.9, review_status="proposed",
        )
        assert "inferred" in rel.describe()
        accepted = rel.model_copy(update={"review_status": "accepted"})
        assert "inferred" not in accepted.describe()

    def test_graph_skips_unusable_and_weights_unconfirmed(self):
        tables = [_t("a", ("id", "integer", True)), _t("b", ("id", "integer", True)),
                  _t("c", ("id", "integer", True))]
        rels = [
            RelationshipMetadata(name="ab", from_table="nw.a", from_column="b_id",
                                 to_table="nw.b", to_column="id", origin="inferred",
                                 confidence=0.9, review_status="proposed"),
            RelationshipMetadata(name="ac", from_table="nw.a", from_column="c_id",
                                 to_table="nw.c", to_column="id", origin="inferred",
                                 confidence=0.9, review_status="rejected"),
        ]
        graph = build_schema_graph(tables, rels)
        (edge,) = graph.edges_from("nw.a")
        assert edge.right == "nw.b"
        assert edge.source == "inferred"


# ----------------------------------------------------------------------
# End to end: a SQLite file with no declared foreign keys
# ----------------------------------------------------------------------


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="system", email="system@acme.test", tenant_id="acme"),
        conversation_id="scan",
        request_id="scan",
        tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )


@pytest.fixture
def warehouse(tmp_path):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE warehouses (id INTEGER PRIMARY KEY, city TEXT);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER,   -- every value exists in customers
            warehouse_id INTEGER   -- mostly values no warehouse has
        );
        INSERT INTO customers VALUES (1, 'a'), (2, 'b'), (3, 'c');
        INSERT INTO warehouses VALUES (1, 'x');
        INSERT INTO orders VALUES (1, 1, 1), (2, 2, 7), (3, 3, 8), (4, 1, 9);
        """
    )
    conn.commit()
    conn.close()
    return str(path)


class TestScannerInfersAndVerifies:
    async def test_scan_proposes_checks_and_stores(self, warehouse):
        catalog = LocalSchemaCatalog()
        report = await SchemaScanner(SqliteRunner(warehouse), dialect="sqlite").scan(
            _context(), catalog
        )

        assert report.relationships_inferred == 2
        assert "inferred" in report.summary()

        served = await catalog.get_relationships(_context())
        # customer_id: every sampled value found -> verified and served.
        # warehouse_id: 3 of 4 orphaned -> sunk below use, stored but not served.
        assert _pairs(served) == {("orders", "customer_id", "customers", "id")}
        assert served[0].confidence == VERIFIED_CONFIDENCE

    async def test_inference_can_be_switched_off(self, warehouse):
        catalog = LocalSchemaCatalog()
        report = await SchemaScanner(
            SqliteRunner(warehouse), dialect="sqlite", infer_relationships=False
        ).scan(_context(), catalog)
        assert report.relationships_inferred == 0
        assert await catalog.get_relationships(_context()) == []
