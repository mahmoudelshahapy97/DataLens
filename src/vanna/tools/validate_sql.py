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
            "and policy violations without running it. Use this before "
            "run_sql on any query that is long, joins several tables, or "
            "scans a large table."
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

        text = "The query is valid and ready to run."
        return _result(True, text, "Valid")


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
