"""Running a query in a workspace that has a semantic layer.

Nothing covered this path end to end, and it was completely broken: the policy's
final pass runs on the *compiled* SQL, which names physical tables, but it asked
the *semantic* catalog whether those tables existed -- and that catalog lists
models. So every query in every workspace with a manifest was refused, with a
message describing the opposite problem ("the table 'track' is not present in the
schema catalog"). It surfaced on the dashboards screen because a dashboard runs
six tiles at once and printed six copies of it.

The tests here are the two halves of the invariant:

* SQL over a model the caller may read **runs** -- the allowlist for the compiled
  pass is projected through the manifest, so `chinook.track` is allowed exactly
  because `tracks` is.
* SQL over the physical table **does not** -- the compiler passes an unknown
  table through untouched, so allowing physical names would let a caller skip the
  row rules and column drops that expanding a model injects.

And the reason the allowlist is projected from the catalog rather than the
manifest: a model the caller may not read must not become reachable through its
table.
"""

from __future__ import annotations

import pytest

from vanna.core.sql_policy import SqlPolicy
from vanna.core.sql_policy.semantic import SemanticSqlPolicyToolRegistry
from vanna.semantic.models import Manifest

pytestmark = pytest.mark.anyio


MANIFEST = Manifest.model_validate(
    {
        "catalog": "vanna",
        "schema": "public",
        "models": [
            {
                "name": "tracks",
                "tableReference": "chinook.track",
                "columns": [
                    {"name": "track_id", "type": "INTEGER"},
                    {"name": "name", "type": "VARCHAR"},
                    {"name": "unit_price", "type": "DECIMAL"},
                ],
            },
            {
                "name": "salaries",
                "tableReference": "chinook.salary",
                "columns": [
                    {"name": "employee_id", "type": "INTEGER"},
                    {"name": "amount", "type": "DECIMAL"},
                ],
            },
        ],
    }
)


class FakeTable:
    def __init__(self, name):
        self.table_name = name
        self.schema_name = None
        self.columns = []


class FakeCatalog:
    """Stands in for the grant-filtered semantic catalog: it lists *models*."""

    def __init__(self, *models):
        self.models = list(models)

    async def get_tables(self, context, data_source_id=None):
        return [FakeTable(name) for name in self.models]


class FakeTool:
    name = "run_sql"


class Args:
    def __init__(self, sql):
        self.sql = sql


def a_registry(*visible):
    return SemanticSqlPolicyToolRegistry(
        policy=SqlPolicy.read_only(),
        dialect="postgres",
        catalog=FakeCatalog(*visible),
        manifest=MANIFEST,
    )


async def run(registry, sql, user, context):
    return await registry.transform_args(FakeTool(), Args(sql), user, context)


@pytest.fixture
def caller(user_factory):
    return user_factory("ada@acme.example", tenant_id="acme")


class TestModelSqlRuns:
    async def test_a_plain_select_over_a_model_is_allowed(
        self, caller, tool_context
    ):
        """The whole feature, in one assertion. This returned a rejection."""
        registry = a_registry("tracks")

        result = await run(
            registry, "SELECT name FROM tracks LIMIT 10", caller, tool_context()
        )

        assert not hasattr(result, "reason"), getattr(result, "reason", "")
        # The argument is replaced with what will really run.
        assert "chinook.track" in result.sql

    async def test_an_aggregate_over_a_model_is_allowed(self, caller, tool_context):
        registry = a_registry("tracks")

        result = await run(
            registry,
            "SELECT count(*) AS n, sum(unit_price) AS total FROM tracks",
            caller,
            tool_context(),
        )

        assert not hasattr(result, "reason"), getattr(result, "reason", "")

    async def test_a_column_the_model_does_not_have_is_still_refused(
        self, caller, tool_context
    ):
        """The projection must not turn into "allow everything"."""
        registry = a_registry("tracks")

        result = await run(
            registry, "SELECT nonexistent FROM tracks", caller, tool_context()
        )

        assert hasattr(result, "reason")


class TestPhysicalSqlIsRefused:
    async def test_naming_the_table_behind_a_model_is_refused(
        self, caller, tool_context
    ):
        """It compiles to itself, so it would otherwise reach the warehouse with
        none of the model's row rules or column drops applied."""
        registry = a_registry("tracks")

        result = await run(
            registry, "SELECT name FROM chinook.track", caller, tool_context()
        )

        assert hasattr(result, "reason")
        assert "semantic models" in result.reason
        assert "chinook.track" in result.reason

    async def test_the_message_names_what_was_wrong(self, caller, tool_context):
        registry = a_registry("tracks")

        result = await run(
            registry,
            "SELECT t.name FROM chinook.track t JOIN chinook.album a"
            " ON a.album_id = t.album_id",
            caller,
            tool_context(),
        )

        assert "chinook.track" in result.reason and "chinook.album" in result.reason

    async def test_a_cte_of_the_callers_own_is_not_mistaken_for_a_table(
        self, caller, tool_context
    ):
        registry = a_registry("tracks")

        result = await run(
            registry,
            "WITH cheap AS (SELECT * FROM tracks WHERE unit_price < 1)"
            " SELECT count(*) FROM cheap",
            caller,
            tool_context(),
        )

        assert not hasattr(result, "reason"), getattr(result, "reason", "")


class TestTheAllowlistFollowsTheGrants:
    async def test_a_model_the_caller_cannot_see_is_refused(
        self, caller, tool_context
    ):
        """`salaries` exists in the manifest and is absent from this caller's
        catalog, which is what a revoked grant looks like from here."""
        registry = a_registry("tracks")

        result = await run(
            registry, "SELECT amount FROM salaries", caller, tool_context()
        )

        assert hasattr(result, "reason")

    async def test_and_its_table_is_refused_too(self, caller, tool_context):
        """The interesting half: the allowlist is projected from what the caller
        may read, so a hidden model does not become reachable through its table."""
        registry = a_registry("tracks")

        result = await run(
            registry, "SELECT amount FROM chinook.salary", caller, tool_context()
        )

        assert hasattr(result, "reason")

    async def test_granting_it_makes_it_work(self, caller, tool_context):
        registry = a_registry("tracks", "salaries")

        result = await run(
            registry, "SELECT amount FROM salaries", caller, tool_context()
        )

        assert not hasattr(result, "reason"), getattr(result, "reason", "")


class TestWithoutAManifestNothingChanges:
    async def test_the_parent_behaviour_is_untouched(self, caller, tool_context):
        """Deployments with no semantic project keep the old path exactly."""
        registry = SemanticSqlPolicyToolRegistry(
            policy=SqlPolicy.read_only(),
            dialect="postgres",
            catalog=FakeCatalog("track"),
            manifest=None,
        )

        result = await run(registry, "SELECT 1 FROM track", caller, tool_context())

        assert not hasattr(result, "reason"), getattr(result, "reason", "")
