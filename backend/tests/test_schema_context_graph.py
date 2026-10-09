"""Graph-expanded schema retrieval and the knowledge hook around it.

`SchemaCatalog.get_context` on the search path used to keep only the tables
search ranked and the edges between them -- so "revenue by artist" got
`invoice_line` and `artist` and no way to join them. These tests pin the
repair: the join tree's bridge tables are added and marked, hinted tables are
always included, and the enhancer puts core columns and table-aware examples
around the result.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vanna.capabilities.schema_catalog.models import (
    ColumnMetadata,
    RelationshipMetadata,
    TableMetadata,
)
from vanna.capabilities.schema_graph import SchemaKnowledge
from vanna.capabilities.schema_graph.knowledge import (
    match_phrases,
    overlap,
    tables_in_sql,
    words,
)
from vanna.core.enhancer import RetrievalContextEnhancer
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog

_CHAIN = ["invoice_line", "track", "album", "artist"]
_FILLER = ["warehouse", "shipment", "supplier", "payroll", "timesheet", "ledger"]


def _user(tenant: str = "acme") -> User:
    return User(id="u1", email="u1@acme.test", tenant_id=tenant)


def _context(tenant: str = "acme") -> ToolContext:
    return ToolContext(
        user=_user(tenant), conversation_id="c1", request_id="r1",
        tenant_id=tenant, agent_memory=DemoAgentMemory(),
    )


def _table(name: str) -> TableMetadata:
    return TableMetadata(
        table_name=name,
        schema_name="chinook",
        description=f"The {name.replace('_', ' ')} table.",
        columns=[
            ColumnMetadata(name=f"{name}_id", data_type="integer", is_primary_key=True),
            ColumnMetadata(name="name", data_type="varchar"),
            ColumnMetadata(name="secret_note", data_type="varchar"),
        ],
    )


async def _catalog() -> LocalSchemaCatalog:
    """invoice_line -> track -> album -> artist, plus unrelated filler tables."""
    catalog = LocalSchemaCatalog()
    context = _context()
    await catalog.upsert_tables(context, [_table(n) for n in _CHAIN + _FILLER])
    await catalog.upsert_relationships(
        context,
        [
            RelationshipMetadata(
                name=f"{a}_{b}", from_table=f"chinook.{a}", from_column=f"{b}_id",
                to_table=f"chinook.{b}", to_column=f"{b}_id",
            )
            for a, b in zip(_CHAIN, _CHAIN[1:])
        ],
    )
    return catalog


class TestSearchPathBridges:
    async def test_bridge_tables_are_added_and_marked(self):
        catalog = await _catalog()
        ctx = await catalog.get_context(
            _context(), "artist invoice line", threshold=0, search_limit=2
        )
        assert ctx.strategy == "search"
        assert ctx.bridge_tables == ["chinook.track", "chinook.album"]
        assert set(ctx.table_names) == {f"chinook.{n}" for n in _CHAIN}
        assert "### Table: chinook.album (join-only" in ctx.text
        assert "### Table: chinook.artist (join-only" not in ctx.text
        # The edges along the chain are all present now.
        assert ctx.text.count("(many_to_one)") == 3

    async def test_bridges_can_be_switched_off(self):
        catalog = await _catalog()
        ctx = await catalog.get_context(
            _context(), "artist invoice line", threshold=0, search_limit=2, max_bridges=0
        )
        assert ctx.bridge_tables == []
        assert set(ctx.table_names) == {"chinook.artist", "chinook.invoice_line"}

    async def test_bridges_are_capped_nearest_first(self):
        catalog = await _catalog()
        ctx = await catalog.get_context(
            _context(), "artist invoice line", threshold=0, search_limit=2, max_bridges=1
        )
        assert len(ctx.bridge_tables) == 1

    async def test_hinted_tables_are_included_even_when_search_misses_them(self):
        catalog = await _catalog()
        ctx = await catalog.get_context(
            _context(), "payroll", threshold=0, search_limit=1,
            seed_tables=["CHINOOK.ARTIST", "no_such_table"],
        )
        assert ctx.hinted_tables == ["chinook.artist"]
        assert "chinook.artist" in ctx.table_names
        assert "chinook.payroll" in ctx.table_names

    async def test_full_path_is_unchanged_but_reports_hints(self):
        catalog = await _catalog()
        ctx = await catalog.get_context(_context(), "anything", seed_tables=["artist"])
        assert ctx.strategy == "full"
        assert ctx.bridge_tables == []
        assert ctx.hinted_tables == ["chinook.artist"]
        assert len(ctx.tables) == len(_CHAIN + _FILLER)
        assert "join-only" not in ctx.text

    async def test_rejected_relationship_never_reaches_the_prompt(self):
        catalog = await _catalog()
        await catalog.upsert_relationships(
            _context(),
            [RelationshipMetadata(
                name="bogus", from_table="chinook.payroll", from_column="ledger_id",
                to_table="chinook.ledger", to_column="ledger_id",
                origin="inferred", confidence=0.95, review_status="rejected",
            )],
        )
        ctx = await catalog.get_context(_context(), "anything")
        assert "chinook.payroll.ledger_id" not in ctx.text

    async def test_serialized_context_omits_table_objects(self):
        catalog = await _catalog()
        ctx = await catalog.get_context(_context(), "anything")
        assert "tables" not in ctx.model_dump()


# ----------------------------------------------------------------------
# The enhancer: hints in, core columns and table-aware examples out
# ----------------------------------------------------------------------


class _Knowledge(SchemaKnowledge):
    def __init__(self, hints=(), core=None, fail: bool = False):
        self.hints, self.core, self.fail = list(hints), core or {}, fail
        self.seen_tables = None

    async def table_hints(self, context, question):
        if self.fail:
            raise RuntimeError("domains down")
        return self.hints

    async def core_columns(self, context, tables):
        self.seen_tables = [t.qualified_name for t in tables]
        return self.core


class _Examples:
    def __init__(self, examples):
        self.examples = examples
        self.limit = None

    async def search(self, context, question, *, limit, verified_only, data_source_id):
        self.limit = limit
        return [
            SimpleNamespace(example=SimpleNamespace(question=q, sql=s))
            for q, s in self.examples[:limit]
        ]


class TestEnhancerKnowledge:
    async def test_hints_seed_the_search_and_are_reported(self):
        enhancer = RetrievalContextEnhancer(
            catalog=await _catalog(), schema_threshold=0,
            knowledge=_Knowledge(hints=["chinook.artist"]),
        )
        result = await enhancer.build_context("payroll", _user())
        schema = result.metadata["schema"]
        assert schema["strategy"] == "search"
        assert schema["hinted_tables"] == ["chinook.artist"]
        assert "### Table: chinook.artist" in result.text

    async def test_core_columns_are_shown_for_visible_columns_only(self):
        core = {
            "chinook.artist": ["name", "dropped_by_grant"],
            "chinook.nowhere": ["name"],
        }
        enhancer = RetrievalContextEnhancer(
            catalog=await _catalog(), knowledge=_Knowledge(core=core)
        )
        result = await enhancer.build_context("anything", _user())
        assert "### Core columns" in result.text
        assert "  - chinook.artist: name" in result.text
        assert "dropped_by_grant" not in result.text
        assert "chinook.nowhere" not in result.text

    async def test_a_failing_knowledge_source_degrades_quietly(self):
        enhancer = RetrievalContextEnhancer(
            catalog=await _catalog(), knowledge=_Knowledge(fail=True)
        )
        result = await enhancer.build_context("anything", _user())
        assert "### Table: chinook.artist" in result.text

    async def test_search_path_prefers_examples_about_the_shown_tables(self):
        examples = _Examples([
            ("payroll", "SELECT * FROM chinook.payroll"),
            ("ledger", "SELECT * FROM chinook.ledger"),
            ("artists", "SELECT a.name FROM chinook.artist a JOIN chinook.album b USING (artist_id)"),
        ])
        enhancer = RetrievalContextEnhancer(
            catalog=await _catalog(), example_store=examples, schema_threshold=0,
            max_examples=1,
        )
        result = await enhancer.build_context("artist album", _user())
        assert examples.limit == 3  # over-fetched to have something to re-rank
        assert "chinook.artist a JOIN" in result.text
        assert "chinook.payroll" not in result.text.split("## Verified query examples")[-1]

    async def test_full_path_keeps_the_search_order(self):
        examples = _Examples([("payroll", "SELECT * FROM chinook.payroll"),
                              ("artists", "SELECT * FROM chinook.artist")])
        enhancer = RetrievalContextEnhancer(
            catalog=await _catalog(), example_store=examples, max_examples=1
        )
        await enhancer.build_context("artist", _user())
        assert examples.limit == 1


class TestMatching:
    def test_words_fold_plurals_and_split_identifiers(self):
        assert words("Invoices by billing_country") == ["invoice", "by", "billing", "country"]
        assert words("categories") == ["category"]

    def test_every_meaningful_word_must_appear(self):
        found = match_phrases("net revenue per customer", {
            "net revenue": 1, "gross revenue": 2, "customer": 3, "the": 4,
        })
        assert found == {"net revenue": 1, "customer": 3}

    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT * FROM chinook.track t JOIN album a ON 1=1",
             {"track", "chinook.track", "album"}),
            ("WITH x AS (SELECT * FROM invoice) SELECT * FROM x", {"invoice"}),
            ("not sql at all FROM foo", {"foo"}),
        ],
    )
    def test_tables_in_sql(self, sql, expected):
        assert expected <= tables_in_sql(sql)
        assert "x" not in tables_in_sql("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_overlap_counts_bare_or_qualified(self):
        assert overlap({"track", "album"}, ["chinook.track", "chinook.album", "x.y"]) == 2
