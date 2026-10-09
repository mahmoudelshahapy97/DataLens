"""`vanna.capabilities.schema_graph` -- the weighted join graph.

Shaped like Chinook, the warehouse `qa.json` is scored against:

    artist <- album <- track <- invoice_line -> invoice -> customer -> employee
                         |  ^
                     genre  playlist_track

Pure graph tests need no catalog; the build tests go through
`LocalSchemaCatalog` the way `test_joins_tool.py` does.
"""

from __future__ import annotations

import pytest

from vanna.capabilities.schema_catalog.models import (
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    TableMetadata,
)
from vanna.capabilities.schema_graph import (
    JoinEdge,
    SchemaGraph,
    build_schema_graph,
    fan_traps,
    load_schema_graph,
)
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog
from vanna.tools.joins import SuggestJoinsArgs, SuggestJoinsTool

#: (holder, column, referenced) -- every edge many-to-one from holder.
_CHINOOK_FKS = [
    ("album", "artist_id", "artist"),
    ("track", "album_id", "album"),
    ("track", "genre_id", "genre"),
    ("invoice_line", "track_id", "track"),
    ("invoice_line", "invoice_id", "invoice"),
    ("invoice", "customer_id", "customer"),
    ("customer", "support_rep_id", "employee"),
    ("playlist_track", "track_id", "track"),
]


def _chinook_graph() -> SchemaGraph:
    graph = SchemaGraph()
    for holder, column, target in _CHINOOK_FKS:
        graph.add_edge(
            JoinEdge(
                left=f"chinook.{holder}",
                left_column=column,
                right=f"chinook.{target}",
                right_column=column if column != "support_rep_id" else "employee_id",
            )
        )
    return graph


def _q(*names: str) -> list:
    return [f"chinook.{n}" for n in names]


class TestShortestPath:
    def test_three_hop_path_names_every_bridge(self):
        path = _chinook_graph().shortest_path("chinook.invoice_line", "chinook.artist")
        assert [e.right for e in path] == _q("track", "album", "artist")

    def test_hop_limit_is_respected(self):
        graph = _chinook_graph()
        # employee -> customer -> invoice -> invoice_line -> track -> album: 5 hops
        assert graph.shortest_path("chinook.employee", "chinook.album") is None
        assert graph.shortest_path("chinook.employee", "chinook.album", max_hops=5)

    def test_path_is_walkable_in_either_direction(self):
        """Stored many-to-one, walked one-to-many: reversing must not lose it."""
        path = _chinook_graph().shortest_path("chinook.artist", "chinook.track")
        assert [e.right for e in path] == _q("album", "track")
        assert path[0].cardinality == "one_to_many"

    def test_declared_edge_beats_inferred_one_of_equal_length(self):
        graph = SchemaGraph()
        graph.add_edge(JoinEdge("a", "x_id", "x", "id", source="inferred"))
        graph.add_edge(JoinEdge("x", "id", "b", "x_id", source="inferred"))
        graph.add_edge(JoinEdge("a", "y_id", "y", "id"))
        graph.add_edge(JoinEdge("y", "id", "b", "y_id"))
        assert [e.right for e in graph.shortest_path("a", "b")] == ["y", "b"]

    def test_many_to_many_hop_costs_extra(self):
        graph = SchemaGraph()
        graph.add_edge(JoinEdge("a", "id", "b", "a_id", cardinality="many_to_many"))
        graph.add_edge(JoinEdge("a", "c_id", "c", "id"))
        graph.add_edge(JoinEdge("c", "id", "b", "c_id"))
        # One many-to-many hop (cost 3) loses to two plain hops (cost 2).
        assert [e.right for e in graph.shortest_path("a", "b")] == ["c", "b"]

    def test_cheaper_longer_path_that_fits_beats_dear_short_one(self):
        graph = SchemaGraph()
        # Inferred many-to-many: 1.5 + 2.0 = 3.5, dearer than three plain hops.
        graph.add_edge(JoinEdge("a", "id", "b", "a_id", cardinality="many_to_many",
                                source="inferred"))
        for left, right in [("a", "m1"), ("m1", "m2"), ("m2", "b")]:
            graph.add_edge(JoinEdge(left, "k", right, "k"))
        assert len(graph.shortest_path("a", "b")) == 3
        # ...but not when the cheaper one is over the hop limit.
        assert len(graph.shortest_path("a", "b", max_hops=2)) == 1


class TestSteinerTree:
    def test_connects_all_terminals_in_one_tree(self):
        tree = _chinook_graph().steiner_tree(_q("artist", "genre", "customer"))
        assert not tree.unreachable
        assert set(tree.bridges(_q("artist", "genre", "customer"))) == set(
            _q("album", "track", "invoice_line", "invoice")
        )
        # A tree: one edge fewer than the tables it spans.
        assert len(tree.edges) == len(tree.tables) - 1

    def test_edges_are_ordered_for_rendering(self):
        tree = _chinook_graph().steiner_tree(_q("invoice_line", "artist", "genre"))
        joined = {tree.anchor}
        for edge in tree.edges:
            assert edge.left in joined, "each JOIN must reference a table already joined"
            joined.add(edge.right)

    def test_shares_bridges_instead_of_duplicating_them(self):
        # album and genre both hang off track; the tree must use track once.
        tree = _chinook_graph().steiner_tree(_q("invoice_line", "album", "genre"))
        assert [e.right for e in tree.edges].count("chinook.track") == 1
        assert len(tree.edges) == 3

    def test_unreachable_terminal_is_reported(self):
        graph = _chinook_graph()
        graph.add_table("chinook.log")
        tree = graph.steiner_tree(_q("track", "log", "album"))
        assert tree.unreachable == ["chinook.log"]
        assert [e.right for e in tree.edges] == ["chinook.album"]

    def test_is_deterministic(self):
        first = _chinook_graph().steiner_tree(_q("customer", "artist", "genre"))
        second = _chinook_graph().steiner_tree(_q("customer", "artist", "genre"))
        assert first.edges == second.edges


class TestFanTraps:
    def test_two_one_to_many_branches_from_one_table_is_a_trap(self):
        tree = _chinook_graph().steiner_tree(_q("track", "invoice_line", "playlist_track"))
        assert fan_traps(tree.edges) == [
            ("chinook.track", _q("invoice_line", "playlist_track"))
        ]

    def test_a_chain_of_lookups_is_not_a_trap(self):
        tree = _chinook_graph().steiner_tree(_q("invoice_line", "artist"))
        assert fan_traps(tree.edges) == []

    def test_one_detail_branch_is_not_a_trap(self):
        tree = _chinook_graph().steiner_tree(_q("customer", "invoice", "employee"))
        assert fan_traps(tree.edges) == []


# ----------------------------------------------------------------------
# Building from catalog metadata
# ----------------------------------------------------------------------


def _table(name: str, columns: list) -> TableMetadata:
    return TableMetadata(table_name=name, schema_name="chinook", columns=columns)


class TestBuild:
    def test_relationship_and_foreign_key_for_one_join_make_one_edge(self):
        tables = [
            _table(
                "track",
                [
                    ColumnMetadata(name="track_id", is_primary_key=True),
                    ColumnMetadata(
                        name="album_id",
                        foreign_key=ForeignKey(
                            column="album_id",
                            references_table="chinook.album",
                            references_column="album_id",
                        ),
                    ),
                ],
            ),
            _table("album", [ColumnMetadata(name="album_id", is_primary_key=True)]),
        ]
        rels = [
            RelationshipMetadata(
                name="track_album",
                from_table="CHINOOK.TRACK",  # spelled differently on purpose
                from_column="album_id",
                to_table="album",
                to_column="album_id",
            )
        ]
        graph = build_schema_graph(tables, rels)
        assert len(graph.edges_from("chinook.track")) == 1
        assert graph.edges_from("chinook.track")[0].cardinality == "many_to_one"

    def test_foreign_key_that_is_the_whole_key_is_one_to_one(self):
        tables = [
            _table(
                "employee_detail",
                [
                    ColumnMetadata(
                        name="employee_id",
                        is_primary_key=True,
                        foreign_key=ForeignKey(
                            column="employee_id",
                            references_table="employee",
                            references_column="employee_id",
                        ),
                    )
                ],
            ),
            _table("employee", [ColumnMetadata(name="employee_id", is_primary_key=True)]),
        ]
        edge = build_schema_graph(tables).edges_from("chinook.employee_detail")[0]
        assert edge.cardinality == "one_to_one"

    def test_relationship_cardinality_is_kept(self):
        tables = [_table("a", []), _table("b", [])]
        rels = [
            RelationshipMetadata(
                name="ab", from_table="chinook.a", from_column="id",
                to_table="chinook.b", to_column="a_id", join_type="one_to_many",
            )
        ]
        graph = build_schema_graph(tables, rels)
        assert graph.edges_from("chinook.a")[0].cardinality == "one_to_many"
        assert graph.edges_from("chinook.b")[0].cardinality == "many_to_one"

    def test_edges_to_tables_outside_the_set_are_dropped(self):
        """A grant-filtered catalog omits a table; no path may route through it."""
        tables = [_table("a", []), _table("c", [])]
        rels = [
            RelationshipMetadata(name="ab", from_table="chinook.a", from_column="b_id",
                                 to_table="chinook.b", to_column="id"),
            RelationshipMetadata(name="bc", from_table="chinook.b", from_column="c_id",
                                 to_table="chinook.c", to_column="id"),
        ]
        graph = build_schema_graph(tables, rels)
        assert "chinook.b" not in graph
        assert graph.shortest_path("chinook.a", "chinook.c") is None


def _context(tenant: str) -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


async def _seed(catalog: LocalSchemaCatalog, tenant: str, names: list) -> None:
    context = _context(tenant)
    await catalog.upsert_tables(context, [_table(n, []) for n in names])
    await catalog.upsert_relationships(
        context,
        [
            RelationshipMetadata(
                name=f"{a}_{b}", from_table=f"chinook.{a}", from_column=f"{b}_id",
                to_table=f"chinook.{b}", to_column=f"{b}_id",
            )
            for a, b in zip(names, names[1:])
        ],
    )


class TestTenantScope:
    async def test_graph_holds_only_the_callers_tables(self):
        catalog = LocalSchemaCatalog()
        await _seed(catalog, "acme", ["track", "album", "artist"])
        await _seed(catalog, "globex", ["order", "customer"])

        acme, _ = await load_schema_graph(catalog, _context("acme"))
        globex, _ = await load_schema_graph(catalog, _context("globex"))

        assert acme.tables == set(_q("track", "album", "artist"))
        assert globex.tables == set(_q("order", "customer"))


class TestSuggestJoinsFanTrapWarning:
    async def test_tool_warns_when_the_tree_multiplies_rows(self):
        catalog = LocalSchemaCatalog()
        context = _context("acme")
        await catalog.upsert_tables(
            context,
            [_table(n, []) for n in ("track", "invoice_line", "playlist_track")],
        )
        await catalog.upsert_relationships(
            context,
            [
                RelationshipMetadata(name="il", from_table="chinook.invoice_line",
                                     from_column="track_id", to_table="chinook.track",
                                     to_column="track_id"),
                RelationshipMetadata(name="pt", from_table="chinook.playlist_track",
                                     from_column="track_id", to_table="chinook.track",
                                     to_column="track_id"),
            ],
        )
        result = await SuggestJoinsTool(catalog).execute(
            context,
            SuggestJoinsArgs(tables=["invoice_line", "playlist_track"]),
        )
        assert "Row multiplication warning" in result.result_for_llm
        assert result.metadata["fan_traps"] == [
            {"hub": "chinook.track", "tables": _q("invoice_line", "playlist_track")}
        ]
        assert result.metadata["bridges"] == ["chinook.track"]

    async def test_three_tables_render_as_one_from_clause(self):
        catalog = LocalSchemaCatalog()
        context = _context("acme")
        await _seed(catalog, "acme", ["invoice_line", "track", "album", "artist"])
        result = await SuggestJoinsTool(catalog).execute(
            context, SuggestJoinsArgs(tables=["invoice_line", "artist", "album"])
        )
        text = result.result_for_llm
        assert text.count("FROM ") == 1
        assert "(3 joins)" in text
        assert result.metadata["bridges"] == ["chinook.track"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
