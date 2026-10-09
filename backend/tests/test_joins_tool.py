"""`suggest_joins` -- the multi-hop join path finder.

The case that matters is the one `get_table_schema` cannot answer: `track` and
`artist` have no edge between them, and a model left to guess joins on
same-named columns and returns a plausible wrong number. These tests are shaped
like Chinook because that is the demo warehouse `qa.json` is scored against.

`LocalSchemaCatalog` backs this the same way `test_schema_tools.py` uses it --
in-memory and dependency-free, so the graph logic is tested without a database.
"""

from __future__ import annotations

import pytest

from vanna.capabilities.schema_catalog.models import (
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    TableMetadata,
)
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog
from vanna.tools.joins import SuggestJoinsArgs, SuggestJoinsTool


def _context(tenant: str = "acme") -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


def _table(name: str, columns: list) -> TableMetadata:
    return TableMetadata(table_name=name, schema_name="chinook", columns=columns)


async def _chinook(
    tenant: str = "acme", *, with_relationships: bool = True
) -> LocalSchemaCatalog:
    """track -> album -> artist, plus an unconnected `log` table."""
    catalog = LocalSchemaCatalog()
    context = _context(tenant)
    await catalog.upsert_tables(
        context,
        [
            _table(
                "track",
                [
                    ColumnMetadata(
                        name="track_id", data_type="integer", is_primary_key=True
                    ),
                    ColumnMetadata(name="album_id", data_type="integer"),
                    ColumnMetadata(name="name", data_type="varchar"),
                ],
            ),
            _table(
                "album",
                [
                    ColumnMetadata(
                        name="album_id", data_type="integer", is_primary_key=True
                    ),
                    ColumnMetadata(name="artist_id", data_type="integer"),
                ],
            ),
            _table(
                "artist",
                [
                    ColumnMetadata(
                        name="artist_id", data_type="integer", is_primary_key=True
                    ),
                    ColumnMetadata(name="name", data_type="varchar"),
                ],
            ),
            _table("log", [ColumnMetadata(name="id", data_type="integer")]),
        ],
    )
    if with_relationships:
        await catalog.upsert_relationships(
            context,
            [
                RelationshipMetadata(
                    name="track_album",
                    from_table="chinook.track",
                    from_column="album_id",
                    to_table="chinook.album",
                    to_column="album_id",
                ),
                RelationshipMetadata(
                    name="album_artist",
                    from_table="chinook.album",
                    from_column="artist_id",
                    to_table="chinook.artist",
                    to_column="artist_id",
                ),
            ],
        )
    return catalog


class TestSuggestJoinsTool:
    def test_declares_no_sql_argument_fields(self):
        assert SuggestJoinsTool(LocalSchemaCatalog()).sql_argument_fields == ()

    async def test_finds_two_hop_path_through_unnamed_table(self):
        tool = SuggestJoinsTool(await _chinook())
        result = await tool.execute(
            _context(), SuggestJoinsArgs(tables=["track", "artist"])
        )

        assert result.success
        # The whole point: album is named even though nobody asked for it.
        assert "chinook.album" in result.result_for_llm
        assert result.metadata["bridges"] == ["chinook.album"]
        assert not result.metadata["unreachable"]

    async def test_emits_usable_on_clauses(self):
        tool = SuggestJoinsTool(await _chinook())
        result = await tool.execute(
            _context(), SuggestJoinsArgs(tables=["track", "artist"])
        )

        text = result.result_for_llm
        assert (
            "JOIN chinook.album ON chinook.track.album_id = chinook.album.album_id"
            in text
        )
        assert (
            "JOIN chinook.artist ON chinook.album.artist_id = chinook.artist.artist_id"
            in text
        )

    async def test_direct_edge_is_one_hop(self):
        tool = SuggestJoinsTool(await _chinook())
        result = await tool.execute(
            _context(), SuggestJoinsArgs(tables=["track", "album"])
        )

        assert "(1 join)" in result.result_for_llm
        assert result.metadata["bridges"] == []

    async def test_foreign_keys_alone_are_enough(self):
        """A catalog with no curated relationships still yields a path."""
        catalog = LocalSchemaCatalog()
        context = _context()
        await catalog.upsert_tables(
            context,
            [
                _table(
                    "track",
                    [
                        ColumnMetadata(
                            name="album_id",
                            data_type="integer",
                            foreign_key=ForeignKey(
                                column="album_id",
                                references_table="chinook.album",
                                references_column="album_id",
                            ),
                        )
                    ],
                ),
                _table(
                    "album",
                    [
                        ColumnMetadata(
                            name="album_id", data_type="integer", is_primary_key=True
                        )
                    ],
                ),
            ],
        )

        result = await SuggestJoinsTool(catalog).execute(
            context, SuggestJoinsArgs(tables=["track", "album"])
        )
        assert result.success
        assert (
            "chinook.track.album_id = chinook.album.album_id" in result.result_for_llm
        )

    async def test_unreachable_table_is_named_not_invented(self):
        tool = SuggestJoinsTool(await _chinook())
        result = await tool.execute(
            _context(), SuggestJoinsArgs(tables=["track", "log"])
        )

        assert result.metadata["unreachable"] == ["chinook.log"]
        assert "No join path" in result.result_for_llm
        # It must not fabricate an ON clause for a table it cannot reach.
        assert "JOIN chinook.log" not in result.result_for_llm

    async def test_unknown_table_is_reported(self):
        tool = SuggestJoinsTool(await _chinook())
        result = await tool.execute(
            _context(), SuggestJoinsArgs(tables=["track", "nonexistent"])
        )

        assert result.metadata["unknown"] == ["nonexistent"]
        assert "search_tables" in result.result_for_llm

    async def test_bare_and_qualified_names_both_resolve(self):
        tool = SuggestJoinsTool(await _chinook())
        result = await tool.execute(
            _context(), SuggestJoinsArgs(tables=["TRACK", "chinook.artist"])
        )

        assert result.success
        assert result.metadata["tables"] == ["chinook.track", "chinook.artist"]

    async def test_output_is_deterministic(self):
        """An unordered set would make identical calls disagree."""
        tool = SuggestJoinsTool(await _chinook())
        args = SuggestJoinsArgs(tables=["track", "artist"])
        first = await tool.execute(_context(), args)
        second = await tool.execute(_context(), args)

        assert first.result_for_llm == second.result_for_llm

    async def test_another_tenant_sees_nothing(self):
        """The catalog is tenant-scoped; the tool must not widen that."""
        tool = SuggestJoinsTool(await _chinook(tenant="acme"))
        result = await tool.execute(
            _context(tenant="other"), SuggestJoinsArgs(tables=["track", "artist"])
        )

        assert result.metadata.get("unknown") == ["track", "artist"]

    async def test_catalog_failure_is_reported_not_swallowed(self):
        class Broken(LocalSchemaCatalog):
            async def get_tables(self, *a, **k):
                raise RuntimeError("catalog down")

        result = await SuggestJoinsTool(Broken()).execute(
            _context(), SuggestJoinsArgs(tables=["a", "b"])
        )
        assert not result.success
        assert "catalog down" in result.error
