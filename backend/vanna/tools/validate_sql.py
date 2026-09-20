"""Check a query for problems before paying to run it.

Two layers, cheapest first:

1. **Policy validation** -- parse the SQL and check it against the safety
   policy. No database contact at all, so it is effectively free.
2. **Dry run** -- ``EXPLAIN`` against the engine, which resolves every table,
   column, function, and type without reading a row.

On a warehouse that bills per byte scanned, catching a typo'd column here
instead of after a four-minute scan is the difference between a free mistake
and an expensive one. Even on a small database it converts a failed turn into a
corrected one.
"""

from __future__ import annotations

from typing import Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.schema_catalog import SchemaCatalog
from vanna.capabilities.sql_runner import SqlRunner
from vanna.components import (
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.sql_policy import SqlPolicy, SqlPolicyValidator
from vanna.core.tool import Tool, ToolContext, ToolResult


class ValidateSqlArgs(BaseModel):
    """Arguments for validate_sql."""

    sql: str = Field(description="The SQL query to check")


class ValidateSqlTool(Tool[ValidateSqlArgs]):
    """Validates SQL without executing it.

    Args:
        sql_runner: Used for the dry run. Omit to do policy checks only.
        policy: Safety policy to check against.
        dialect: sqlglot dialect. Defaults to the runner's.
        catalog: Enables table-existence checking under strict policy.
    """

    #: Validation runs the same policy as execution, so the same field is checked.
    sql_argument_fields: tuple = ("sql",)

    def __init__(
        self,
        sql_runner: Optional[SqlRunner] = None,
        *,
        policy: Optional[SqlPolicy] = None,
        dialect: Optional[str] = None,
        catalog: Optional[SchemaCatalog] = None,
    ) -> None:
        self.sql_runner = sql_runner
        self.policy = policy or SqlPolicy()
        self.dialect = dialect or getattr(sql_runner, "dialect", None)
        self.catalog = catalog
        self.validator = SqlPolicyValidator()

    @property
    def name(self) -> str:
        return "validate_sql"

    @property
    def description(self) -> str:
        return (
            "Check a SQL query for syntax errors, unknown tables or columns, "
            "and policy violations without running it, and get back the "
            "database's query plan. Use this before run_sql on any query that "
            "is long, joins several tables, or scans a large table -- the plan "
            "shows an accidental cross join, which is not an error and is "
            "otherwise discovered by waiting for it."
        )

    def get_args_schema(self) -> Type[ValidateSqlArgs]:
        return ValidateSqlArgs

    async def execute(
        self, context: ToolContext, args: ValidateSqlArgs
    ) -> ToolResult:
        # -- Layer 1: policy (free) ------------------------------------
        catalog_tables = None
        if self.policy.require_catalog_tables and self.catalog is not None:
            try:
                tables = await self.catalog.get_tables(context)
                catalog_tables = [t.table_name for t in tables] + [
                    t.qualified_name for t in tables
                ]
            except Exception:
                catalog_tables = None

        violations = self.validator.validate(
            args.sql,
            dialect=self.dialect,
            policy=self.policy,
            catalog_tables=catalog_tables,
        )
        if violations:
            detail = "\n".join(f"- {v.message}" for v in violations)
            text = (
                f"The query did not pass the safety policy:\n{detail}\n\n"
                "Rewrite it as a read-only SELECT over the available tables."
            )
            return _result(False, text, "Policy check failed", violations)

        # -- Layer 2: dry run (cheap, needs the database) ---------------
        if self.sql_runner is None or not hasattr(self.sql_runner, "dry_run"):
            text = (
                "The query passed the safety policy. Syntax and column names "
                "could not be checked against the database, so run it and "
                "handle any error."
            )
            return _result(True, text, "Policy OK (not dry-run)")

        error = await self.sql_runner.dry_run(args.sql, context)
        if error:
            text = (
                f"The database rejected this query during planning:\n\n{error}\n\n"
                "Fix the specific problem named above -- do not rewrite the "
                "query from scratch."
            )
            return _result(False, text, "Dry run failed")

        # The same EXPLAIN the dry run just performed, kept this time rather
        # than discarded. A plan is the only thing distinguishing a valid query
        # from an affordable one: an accidental cross join is not an error, and
        # without this it is discovered by waiting for it.
        text = "The query is valid and ready to run."
        plan = await self._plan(args.sql, context)
        if plan:
            text += f"\n\nQuery plan:\n{plan}"
            if _looks_expensive(plan):
                text += (
                    "\n\nThis plan contains a nested loop or a full scan "
                    "over more than one table, which is what an accidental "
                    "cross join looks like. Check every join has an ON clause "
                    "before running it."
                )

        return _result(True, text, "Valid")

    async def _plan(self, sql: str, context: ToolContext) -> Optional[str]:
        """Never raises: a plan is an extra, not part of the verdict."""
        explain = getattr(self.sql_runner, "explain", None)
        if explain is None:
            return None
        try:
            return await explain(sql, context)
        except Exception:
            return None


def _looks_expensive(plan: str) -> bool:
    """Match on words every dialect shares, not on a plan format.

    Plan syntax differs between engines and between versions of one engine, so
    parsing it properly would be a maintenance burden for what is only a hint.
    This is deliberately a heuristic, and it is phrased to the model as
    something to check rather than as a verdict.
    """
    lowered = plan.lower()
    if "nested loop" in lowered or "cartesian" in lowered or "cross join" in lowered:
        return True
    # One full scan is ordinary -- a small table has no index worth using. Two
    # in the same plan is the shape a missing join condition produces.
    return lowered.count("seq scan") + lowered.count("scan ") > 1


def _result(ok: bool, text: str, summary: str, violations=None) -> ToolResult:
    # Reported as success even when the query is invalid. The *tool* did its
    # job -- it found the problem, which is what it exists for. Marking it
    # failed would trip the error-recovery path into retrying a validation
    # that is behaving exactly as intended.
    return ToolResult(
        success=True,
        result_for_llm=text,
        ui_component=UiComponent(
            rich_component=RichTextComponent(content=summary, markdown=False),
            simple_component=SimpleTextComponent(text=summary),
        ),
        metadata={
            "valid": ok,
            "violations": [v.code.value for v in (violations or [])],
        },
    )
