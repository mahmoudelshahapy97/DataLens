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

#: Argument field names inspected for SQL when a tool does not say otherwise.
#: Covers the built-in RunSqlTool (`sql`) and the common alternatives custom tools
#: use.
#:
#: A name-based guess is a fallback, not the mechanism. It is wrong in both
#: directions: `search_tables` takes a plain-English phrase in a field called
#: `query`, which sqlglot parses as an Alias and the policy then refuses -- and a
#: tool holding real SQL in a field named `body` would never be checked at all. The
#: second is the dangerous one.
#:
#: A tool declares its own SQL-bearing fields with `sql_argument_fields`. That is
#: authoritative; this list applies only to tools that stay silent.
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
        data_source_id: Optional[str] = None,
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
        # Which of the workspace's databases this registry serves.
        #
        # Not cosmetic once a workspace has more than one. Both catalog reads
        # below used to omit it, so they returned every table the *tenant* had
        # anywhere -- and the table check happily accepted a table that exists
        # only in a sibling database. Harmless while that produced a plain
        # 'relation does not exist' from the server, and not harmless at all if
        # two of a workspace's databases share a table name: the column check
        # would then validate against the wrong schema's columns.
        self.data_source_id = data_source_id
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
        sql_values = self._extract_sql(args, tool)
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

        catalog_columns = None
        if policy.require_catalog_columns:
            catalog_columns = await self._catalog_columns(context)
            if catalog_columns is None:
                logger.warning(
                    "SqlPolicy.require_catalog_columns is enabled but the catalog "
                    "cannot report column permissions; column references cannot be "
                    "checked for tool '%s'.",
                    tool.name,
                )

        all_violations: List[PolicyViolation] = []
        for field_name, sql in sql_values.items():
            violations = self.validator.validate(
                sql,
                dialect=self.dialect,
                policy=policy,
                catalog_tables=catalog_tables,
                catalog_columns=catalog_columns,
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

        # Execute what was checked, not what was written.
        #
        # Validation expands `SELECT *` against the caller's filtered catalog, so
        # it only ever sees permitted columns and passes. The database expands the
        # same star against the physical table, which still has the revoked column
        # in it -- and returns it. Substituting the expanded form makes the
        # statement say what the validator understood it to say.
        if catalog_columns:
            for field_name, sql in sql_values.items():
                expanded = self.validator.expand_stars(
                    sql, dialect=self.dialect, catalog_columns=catalog_columns
                )
                if expanded:
                    args = self._replace_sql(args, field_name, expanded)

        return args

    @staticmethod
    def _replace_sql(args: Any, field_name: str, sql: str) -> Any:
        """Put a rewritten statement back where it came from.

        Mirrors ``_extract_sql``: dict-shaped arguments are the common case, and a
        model object is updated by attribute so a tool taking a typed payload
        behaves the same way.
        """
        if isinstance(args, dict):
            updated = dict(args)
            updated[field_name] = sql
            return updated
        try:
            setattr(args, field_name, sql)
        except Exception:  # frozen or exotic payload: leave it alone
            logger.warning(
                "Could not substitute the expanded statement into field '%s'; "
                "a wildcard may execute unexpanded.",
                field_name,
            )
        return args

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_sql(self, args: Any, tool: Any = None) -> Dict[str, str]:
        """Pull the SQL-bearing string fields out of validated tool arguments.

        A tool may declare `sql_argument_fields`, and that wins: `()` means the tool
        carries no SQL and must not be policy-checked, a tuple of names says exactly
        where its SQL lives. Only a tool that declares nothing falls back to guessing
        by name.
        """
        fields = getattr(tool, "sql_argument_fields", None) if tool is not None else None
        if fields is None:
            fields = self.sql_fields

        found: Dict[str, str] = {}
        for field in fields:
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
            tables = await self.catalog.get_tables(
                context, data_source_id=self.data_source_id
            )
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

    async def _catalog_columns(self, context: ToolContext) -> Optional[dict]:
        """``{table: {column: {"read", "filter", "aggregate"}}}`` for this caller.

        Asks the catalog itself, because the catalog in front of us is already
        narrowed to the caller (``GrantFilteredCatalog`` wraps it) and can report
        the permissions it filtered on. Resolving grants a second time here would
        create a second answer to the same question, and the two only have to
        disagree once -- in the widening direction -- for the check to be bypassed.

        A catalog that cannot report permissions still gets the *existence* check:
        every column the caller can see is allowed for every use. That is exactly
        today's behaviour for a deployment with read enforcement switched off, so
        turning ``require_catalog_columns`` on cannot narrow access that nobody
        asked to narrow -- it only starts refusing columns that do not exist.
        """
        if self.catalog is None:
            return None

        reporter = getattr(self.catalog, "column_uses", None)
        if callable(reporter):
            try:
                reported = await reporter(context)
            except Exception as exc:
                # Same asymmetry as everywhere else on this path: failing to
                # resolve grants must not become "allow everything".
                logger.error("Column permissions unavailable during policy check: %s", exc)
                return {}
            if reported is not None:
                return reported

        try:
            tables = await self.catalog.get_tables(
                context, data_source_id=self.data_source_id
            )
        except Exception as exc:
            logger.error("Schema catalog unavailable during policy check: %s", exc)
            return {}

        every_use = {"read", "filter", "aggregate"}
        columns: dict = {}
        for table in tables:
            allowed = {
                str(column.name).lower(): set(every_use)
                for column in getattr(table, "columns", []) or []
            }
            if not allowed:
                continue
            name = getattr(table, "table_name", "")
            schema = getattr(table, "schema_name", None)
            # One canonical key per table, qualified when there is a schema.
            #
            # Emitting both spellings -- as `_catalog_table_names` does, correctly,
            # for the flat set the table check uses -- produces a mapping of mixed
            # depth here: `{"chinook": {"artist": {...}}, "artist": {...}}`. sqlglot's
            # qualifier infers one depth for the whole schema, so a mixed mapping
            # resolves nothing and *every* query against a schema-qualified database
            # was rejected as having unresolvable columns. Unqualified references are
            # handled where they belong, by the bare-name map in `_check_columns`.
            key = f"{schema}.{name}" if schema else name
            columns[str(key).lower()] = allowed
        return columns

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
