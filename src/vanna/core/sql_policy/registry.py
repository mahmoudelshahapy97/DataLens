"""A ToolRegistry that enforces SQL policy before any tool executes.

``ToolRegistry.transform_args`` is the seam Vanna already provides for
inspecting and rejecting tool arguments per user, after Pydantic validation and
before execution. Its docstring names row-level security as the motivating use
case. This subclass uses it for exactly that.

Enforcing here rather than inside individual tools means:

* every SQL-bearing tool is covered, including custom ones, with no per-tool
  code and no way to forget;
* rejection produces a ``ToolRejection`` that flows back to the model as a
  normal tool error, so the agent can correct itself instead of crashing;
* the check is centralised, so it can be audited and reasoned about in one
  place.

Usage::

    registry = SqlPolicyToolRegistry(policy=SqlPolicy.read_only())
    registry.register_local_tool(RunSqlTool(sql_runner=runner), [])

    agent = Agent(llm_service=llm, tool_registry=registry, ...)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Union

from ..registry import ToolRegistry
from ..tool import Tool, ToolContext, ToolRejection
from ..user import User
from .models import PolicyViolation, SqlPolicy
from .validator import SqlPolicyValidator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...capabilities.schema_catalog import SchemaCatalog

logger = logging.getLogger(__name__)

#: Argument field names inspected for SQL. Covers the built-in RunSqlTool
#: (`sql`) and the common alternatives custom tools use.
DEFAULT_SQL_FIELDS = ("sql", "query", "statement", "sql_query")


class SqlPolicyToolRegistry(ToolRegistry):
    """Tool registry that validates SQL arguments against a policy.

    Args:
        policy: Rules to apply. Defaults to strict read-only.
        dialect: sqlglot dialect for parsing. Improves fidelity considerably --
            supply it whenever the target engine is known.
        sql_fields: Argument names to inspect. Defaults to
            :data:`DEFAULT_SQL_FIELDS`.
        catalog: Optional schema catalog, required by
            ``policy.require_catalog_tables``. Without it, strict mode cannot
            check table references and logs a warning rather than silently
            passing everything.
        policy_for_user: Optional hook returning a per-user or per-tenant
            policy, e.g. a stricter one for external users. Receives the user
            and the tool context.
        audit_logger / audit_config: Passed through to ``ToolRegistry``.
    """

    def __init__(
        self,
        *,
        policy: Optional[SqlPolicy] = None,
        dialect: Optional[str] = None,
        sql_fields: Iterable[str] = DEFAULT_SQL_FIELDS,
        catalog: Optional["SchemaCatalog"] = None,
        policy_for_user: Optional[
            Callable[[User, ToolContext], SqlPolicy]
        ] = None,
        audit_logger: Any = None,
        audit_config: Any = None,
    ) -> None:
        super().__init__(audit_logger=audit_logger, audit_config=audit_config)
        self.policy = policy or SqlPolicy()
        self.dialect = dialect
        self.sql_fields = tuple(sql_fields)
        self.catalog = catalog
        self.policy_for_user = policy_for_user
        self.validator = SqlPolicyValidator()

    async def transform_args(
        self,
        tool: Tool[Any],
        args: Any,
        user: User,
        context: ToolContext,
    ) -> Union[Any, ToolRejection]:
        """Validate any SQL-bearing argument before the tool runs."""
        sql_values = self._extract_sql(args)
        if not sql_values:
            return args  # nothing SQL-shaped; nothing to check

        policy = (
            self.policy_for_user(user, context)
            if self.policy_for_user
            else self.policy
        )

        catalog_tables: Optional[List[str]] = None
        if policy.require_catalog_tables:
            catalog_tables = await self._catalog_table_names(context)
            if catalog_tables is None:
                # Strict mode without a catalog cannot do its job. Say so loudly
                # rather than quietly downgrading to "allow anything".
                logger.warning(
                    "SqlPolicy.require_catalog_tables is enabled but no schema "
                    "catalog is configured; table references cannot be checked "
                    "for tool '%s'.",
                    tool.name,
                )

        all_violations: List[PolicyViolation] = []
        for field_name, sql in sql_values.items():
            violations = self.validator.validate(
                sql,
                dialect=self.dialect,
                policy=policy,
                catalog_tables=catalog_tables,
            )
            if violations:
                logger.warning(
                    "SQL policy rejected tool=%s field=%s user=%s tenant=%s "
                    "codes=%s",
                    tool.name,
                    field_name,
                    user.id,
                    getattr(context, "tenant_id", "default"),
                    [v.code.value for v in violations],
                )
                all_violations.extend(violations)

        if all_violations:
            return ToolRejection(reason=self._rejection_message(all_violations))

        return args

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_sql(self, args: Any) -> Dict[str, str]:
        """Pull SQL-looking string fields out of validated tool arguments."""
        found: Dict[str, str] = {}
        for field in self.sql_fields:
            value = getattr(args, field, None)
            if isinstance(value, str) and value.strip():
                found[field] = value
        return found

    async def _catalog_table_names(
        self, context: ToolContext
    ) -> Optional[List[str]]:
        if self.catalog is None:
            return None
        try:
            tables = await self.catalog.get_tables(context)
        except Exception as e:
            # A catalog outage must not silently disable strict mode.
            logger.error("Schema catalog unavailable during policy check: %s", e)
            return []
        names: List[str] = []
        for table in tables:
            names.append(table.table_name)
            if table.schema_name:
                names.append(f"{table.schema_name}.{table.table_name}")
        return names

    @staticmethod
    def _rejection_message(violations: List[PolicyViolation]) -> str:
        """Compose the message sent back to the model.

        Phrased as instructions rather than a bare error: the recipient is an
        LLM that can rewrite the query, and telling it what to do next converts
        a dead end into a self-correction. Individual messages already exclude
        offending expressions, so this is safe to surface.
        """
        if len(violations) == 1:
            detail = violations[0].message
        else:
            detail = " ".join(f"({i + 1}) {v.message}" for i, v in enumerate(violations))
        return (
            f"The query was blocked by the SQL safety policy. {detail} "
            "Rewrite it as a plain read-only SELECT over the available tables."
        )
