"""Which tool arguments the SQL policy is allowed to judge.

The registry decided this by field *name*: anything called ``sql``, ``query``,
``statement`` or ``sql_query`` was parsed by sqlglot and checked against the policy.
That is wrong in both directions, and both directions bit.

**Refusing what it should not.** ``search_tables`` takes a plain-English phrase in a
field called ``query``. "customer orders and payments" parses as an ``Alias``, so
every catalog search came back *"The query was blocked by the SQL safety policy.
ALIAS statements are not permitted."* The agent could not search the catalog at all
and fell back to guessing table names.

**Passing what it should not.** A tool holding real SQL in a field the list does not
name -- ``body``, ``statement_text``, ``q`` -- was never checked. That is the
serious one: the policy is the last thing between a generated query and the
warehouse, and it was opt-in by naming convention.

A tool now declares ``sql_argument_fields``. It is authoritative; the name list
applies only where a tool says nothing.
"""

from __future__ import annotations

from typing import Any, List

import pytest
from pydantic import BaseModel

from vanna.core.sql_policy import SqlPolicy, SqlPolicyToolRegistry
from vanna.core.sql_policy.registry import DEFAULT_SQL_FIELDS


class _Args(BaseModel):
    query: str = ""
    sql: str = ""
    body: str = ""


class _Tool:
    """The minimum the registry reads off a tool."""

    def __init__(self, name: str, fields: Any = "unset") -> None:
        self.name = name
        if fields != "unset":
            self.sql_argument_fields = fields


@pytest.fixture
def registry():
    return SqlPolicyToolRegistry(policy=SqlPolicy(), dialect="postgres")


class TestFieldSelection:
    def test_a_tool_declaring_no_sql_is_not_checked(self, registry):
        """The search_tables bug."""
        found = registry._extract_sql(
            _Args(query="customer orders and payments"), _Tool("search_tables", ())
        )
        assert found == {}

    def test_a_tool_declaring_its_field_is_checked_there(self, registry):
        found = registry._extract_sql(
            _Args(sql="SELECT 1", query="ignored"), _Tool("run_sql", ("sql",))
        )
        assert found == {"sql": "SELECT 1"}

    def test_a_declared_field_outside_the_default_list_is_checked(self, registry):
        """The dangerous direction: SQL in a field the name list never knew about."""
        found = registry._extract_sql(
            _Args(body="DELETE FROM users"), _Tool("custom", ("body",))
        )
        assert found == {"body": "DELETE FROM users"}, (
            "a tool declaring SQL in `body` must still be policy-checked"
        )

    def test_a_silent_tool_falls_back_to_the_name_list(self, registry):
        found = registry._extract_sql(_Args(sql="SELECT 1"), _Tool("legacy"))
        assert found == {"sql": "SELECT 1"}

    def test_the_fallback_still_covers_every_default_name(self, registry):
        # Removing a name from the default list would silently stop checking a
        # third-party tool that relied on it.
        assert set(DEFAULT_SQL_FIELDS) >= {"sql", "query", "statement", "sql_query"}

    def test_no_tool_means_the_name_list(self, registry):
        # transform_args always passes one; this keeps the helper usable alone.
        assert registry._extract_sql(_Args(sql="SELECT 1")) == {"sql": "SELECT 1"}

    def test_an_empty_string_is_not_sql(self, registry):
        assert registry._extract_sql(_Args(sql="   "), _Tool("run_sql", ("sql",))) == {}


class TestShippedTools:
    """What the built-in tools declare, asserted rather than assumed."""

    @pytest.mark.parametrize(
        "module,name",
        [
            ("vanna.tools", "SearchTablesTool"),
        ],
    )
    def test_search_tools_carry_no_sql(self, module: str, name: str):
        import importlib

        tool = getattr(importlib.import_module(module), name)
        assert tool.sql_argument_fields == (), (
            f"{name} takes a plain-text query; the policy must not parse it as SQL"
        )

    @pytest.mark.parametrize("name", ["RunSqlTool", "ValidateSqlTool"])
    def test_the_sql_tools_declare_their_field(self, name: str):
        import vanna.tools as tools

        tool = getattr(tools, name)
        assert tool.sql_argument_fields == ("sql",), (
            f"{name} must state where its SQL is -- it is the reason the policy exists"
        )

    def test_every_tool_with_a_colliding_field_name_has_decided(self):
        """A field called `query` or `sql` must not be left to the fallback.

        This is the check that would have caught the original bug: `search_tables`
        had a `query` field and no declaration, so the policy guessed -- wrongly.
        """
        import importlib
        import inspect

        undecided: List[str] = []
        for module_name in (
            "vanna.tools.schema",
            "vanna.tools.run_sql",
            "vanna.tools.validate_sql",
            "vanna.tools.column_values",
            "vanna.tools.dashboard",
            "vanna.tools.agent_memory",
            "vanna.tools.calculator",
            "vanna.tools.knowledge",
            "vanna.tools.query_history",
            "vanna.tools.value_dictionary",
        ):
            module = importlib.import_module(module_name)
            for attribute, obj in vars(module).items():
                if not (inspect.isclass(obj) and attribute.endswith("Tool")):
                    continue
                args = getattr(obj, "__orig_bases__", None)
                if not args:
                    continue
                # The Tool[...] parameter is the args model.
                model = getattr(args[0], "__args__", (None,))[0]
                fields = set(getattr(model, "model_fields", {}) or {})
                if not fields & set(DEFAULT_SQL_FIELDS):
                    continue
                if not hasattr(obj, "sql_argument_fields"):
                    undecided.append(f"{module_name}.{attribute}")

        assert not undecided, (
            "these tools have an argument the policy would guess about; declare "
            f"sql_argument_fields on each: {undecided}"
        )
