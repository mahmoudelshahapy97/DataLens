"""Generation rows say what retrieval showed the model.

`SqlGeneration` has always had `retrieval_strategy`, `retrieved_table_names` and
`retrieved_example_ids`, and `RecordingRunSqlTool` never filled them: every row
of the toolkit run had them empty, so lineage reported nothing and triage could
not separate "the model never saw the table" from "the model saw it and erred".
"""

from __future__ import annotations

from types import SimpleNamespace

from vanna.capabilities.schema_catalog.models import ColumnMetadata, TableMetadata
from vanna.core.enhancer import RetrievalContextEnhancer
from vanna.core.enhancer.retrieval import _LAST_RETRIEVAL, last_retrieval
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog


def _user() -> User:
    return User(id="u1", email="u1@acme.test", tenant_id="acme")


class _Examples:
    async def search(self, context, question, *, limit, verified_only, data_source_id):
        return [
            SimpleNamespace(example=SimpleNamespace(id="ex-1", question="q1", sql="SELECT 1")),
            SimpleNamespace(example=SimpleNamespace(id="ex-2", question="q2", sql="SELECT 2")),
        ][:limit]


async def _catalog() -> LocalSchemaCatalog:
    catalog = LocalSchemaCatalog()
    context = ToolContext(
        user=_user(), conversation_id="c", request_id="r", tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )
    await catalog.upsert_tables(context, [
        TableMetadata(table_name=n, schema_name="chinook",
                      columns=[ColumnMetadata(name=f"{n}_id", is_primary_key=True)])
        for n in ("track", "album")
    ])
    return catalog


class TestTheEnhancerRemembersWhatItChose:
    async def test_strategy_tables_and_examples_are_recorded(self):
        enhancer = RetrievalContextEnhancer(
            catalog=await _catalog(), example_store=_Examples(), max_examples=2
        )
        result = await enhancer.build_context("tracks per album", _user())

        chosen = last_retrieval()
        assert chosen["strategy"] == "full"
        assert set(chosen["table_names"]) == {"chinook.track", "chinook.album"}
        assert chosen["example_ids"] == ["ex-1", "ex-2"]
        # The rendered prompt is unchanged by carrying the ids.
        assert "Question: q1\nSQL:\nSELECT 1" in result.text


class TestTheRecorderStoresIt:
    def test_fields_come_from_the_last_retrieval(self):
        from vanna_app.platform import _retrieval_fields

        token = _LAST_RETRIEVAL.set(
            {"strategy": "search", "table_names": ["a", "b"], "example_ids": ["ex-9"]}
        )
        try:
            assert _retrieval_fields() == {
                "retrieval_strategy": "search",
                "retrieved_table_names": ["a", "b"],
                "retrieved_example_ids": ["ex-9"],
            }
        finally:
            _LAST_RETRIEVAL.reset(token)

    def test_nothing_when_no_retrieval_ran(self):
        from vanna_app.platform import _retrieval_fields

        token = _LAST_RETRIEVAL.set(None)
        try:
            assert _retrieval_fields() == {}
        finally:
            _LAST_RETRIEVAL.reset(token)

    def test_the_fields_fit_the_generation_model(self):
        from vanna.core.generation import SqlGeneration

        generation = SqlGeneration(
            tenant_id="acme", question="q", sql="SELECT 1",
            retrieval_strategy="full", retrieved_table_names=["a"], retrieved_example_ids=["e"],
        )
        assert generation.retrieved_table_names == ["a"]
