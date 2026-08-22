"""Compiling semantic SQL inside the tool registry's enforcement seam.

``SqlPolicyToolRegistry`` validates whatever SQL a tool was handed. Once a
semantic layer exists there are two different statements in play -- what the
model wrote, and what will actually run -- and validating either one alone is
wrong:

* Validate only the **semantic** SQL and ``require_catalog_tables`` rejects
  every model name, because no model is a physical table.
* Validate only the **compiled** SQL and a ``DROP`` written against a model name
  reaches the compiler before anything checks it.

So both are checked, at the point each is meaningful: a cheap statement-kind
check on the semantic SQL before compiling, then the full policy pass on the
compiled SQL. The compiled statement replaces the tool's argument, which is what
makes every downstream path -- chat, MCP, dashboards -- go through one seam.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional, Union

from ...semantic.compiler import CompiledSql, compile_sql
from ...semantic.models import Manifest
from ..errors import VannaError
from ..tool.models import ToolContext, ToolRejection
from ..user.models import User
from .models import PolicyViolation, SqlPolicy, ViolationCode
from .registry import SqlPolicyToolRegistry

logger = logging.getLogger(__name__)

#: Statement kinds that are always reads. Used only as the fallback when a
#: policy declares no explicit allow-list.
_READ_ROOTS = frozenset({"select", "with", "union", "except", "intersect"})

#: sqlglot AST class name -> the statement keyword a policy allow-list uses.
#: Needed because the class is `Insert`/`Delete` while policies are written in
#: terms of `INSERT`/`DELETE`.
_ROOT_TO_KEYWORD = {
    "select": "SELECT", "with": "WITH", "union": "UNION",
    "except": "EXCEPT", "intersect": "INTERSECT",
    "insert": "INSERT", "update": "UPDATE", "delete": "DELETE",
    "merge": "MERGE", "drop": "DROP", "create": "CREATE",
    "alter": "ALTER", "truncate": "TRUNCATE", "grant": "GRANT",
}


class SemanticSqlPolicyToolRegistry(SqlPolicyToolRegistry):
    """Compiles model-level SQL to dialect SQL, then applies the policy.

    Args:
        manifest: The semantic layer. When None, this behaves exactly like its
            parent -- deployments without a project keep working unchanged.
        fanout_guard: Passed to the compiler. Warnings it produces are attached
            to the tool result so they reach the answer, not just a log.
        manifest_for: Optional hook returning a per-tenant manifest, for
            deployments where each tenant has its own semantic layer.
    """

    def __init__(
        self,
        *args: Any,
        manifest: Optional[Manifest] = None,
        fanout_guard: str = "warn",
        manifest_for: Optional[Callable[[User, ToolContext], Optional[Manifest]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.manifest = manifest
        self.fanout_guard = fanout_guard
        self.manifest_for = manifest_for

    # ------------------------------------------------------------------

    def _manifest_for(self, user: User, context: ToolContext) -> Optional[Manifest]:
        if self.manifest_for is not None:
            return self.manifest_for(user, context)
        return self.manifest

    def compile_for(
        self, sql: str, user: User, context: ToolContext
    ) -> Optional[CompiledSql]:
        """Compile one statement for this caller, or None with no manifest.

        Overridden by :class:`AccessControlToolRegistry` to inject row and
        column rules, which is why it is a method rather than a call inline.
        """
        manifest = self._manifest_for(user, context)
        if manifest is None:
            return None
        return compile_sql(
            sql,
            manifest,
            dialect=self.dialect or "",
            fanout_guard=self.fanout_guard,
        )

    # ------------------------------------------------------------------

    async def transform_args(
        self,
        tool: Any,
        args: Any,
        user: User,
        context: ToolContext,
    ) -> Union[Any, ToolRejection]:
        manifest = self._manifest_for(user, context)
        if manifest is None:
            return await super().transform_args(tool, args, user, context)

        sql_values = self._extract_sql(args, tool)
        if not sql_values:
            return await super().transform_args(tool, args, user, context)

        # The caller's effective policy decides what may be compiled. Resolving
        # it here rather than assuming read-only is what lets a deployment
        # permit DML for an admin without this class knowing why.
        policy = (
            self.policy_for_user(user, context) if self.policy_for_user else self.policy
        )

        for field_name, sql in sql_values.items():
            # -- 1. Refuse statement kinds this caller may not run -----
            rejection = self._reject_disallowed(
                sql, policy=policy, tool_name=getattr(tool, "name", "?")
            )
            if rejection is not None:
                return rejection

            # -- 2. Compile -------------------------------------------
            try:
                compiled = self.compile_for(sql, user, context)
            except VannaError as exc:
                logger.info(
                    "Semantic compile failed tool=%s tenant=%s: %s",
                    getattr(tool, "name", "?"),
                    getattr(context, "tenant_id", "default"),
                    exc,
                )
                # The hint is the actionable half -- it names the columns or
                # relationships that do exist, which is what lets the model fix
                # it on the next turn instead of guessing again.
                reason = exc.args[0] if exc.args else str(exc)
                if exc.hint:
                    reason = f"{reason} {exc.hint}"
                return ToolRejection(reason=reason)

            if compiled is None:
                continue

            # -- 3. Replace the argument, then let the policy see it ---
            _set_field(args, field_name, compiled.sql)

            if compiled.warnings:
                # Carried on the context rather than the result: the tool
                # returns its own ToolResult and we do not own it, but anything
                # reading the context after execution can surface these.
                warnings = context.metadata.setdefault("semantic_warnings", [])
                warnings.extend(w.message for w in compiled.warnings)

            if compiled.referenced_models:
                context.metadata.setdefault("semantic_models", []).extend(
                    compiled.referenced_models
                )

        # -- 4. Full policy check, on what will really run -------------
        return await super().transform_args(tool, args, user, context)

    # ------------------------------------------------------------------

    def _reject_disallowed(
        self, sql: str, *, policy: SqlPolicy, tool_name: str
    ) -> Optional[ToolRejection]:
        """Cheap statement-kind check on the semantic SQL.

        Deliberately not the full policy: catalog checks and function rules
        cannot be evaluated against model names, because no model is a table
        yet. This exists so a statement kind the caller may not run never
        reaches the compiler at all.

        The allow-list comes from the *resolved* policy, so a deployment that
        permits DML for admins gets it here and everywhere else from one place.
        """
        import sqlglot
        from sqlglot import expressions as exp

        try:
            # `Semicolon` is punctuation, not a statement -- see the note in
            # validator.py. Counting it here rejected `SELECT ...; -- note` as
            # two statements.
            statements = [
                s
                for s in sqlglot.parse(sql, read=self.dialect)
                if s and not isinstance(s, exp.Semicolon)
            ]
        except Exception:
            # Let the compiler produce the parse error -- its message names the
            # position, and duplicating that here would report it twice.
            return None

        if not statements:
            return None

        if len(statements) > 1:
            return ToolRejection(
                reason=self._rejection_message(
                    [
                        PolicyViolation(
                            code=ViolationCode.MULTIPLE_STATEMENTS,
                            message="Only one statement may be executed at a time.",
                        )
                    ]
                )
            )

        root = type(statements[0]).__name__.lower()
        allowed = {s.upper() for s in (policy.allowed_statements or set())} or {
            _ROOT_TO_KEYWORD[r] for r in _READ_ROOTS
        }
        keyword = _ROOT_TO_KEYWORD.get(root, root.upper())

        if keyword not in allowed:
            logger.warning(
                "Non-read statement blocked before compilation tool=%s kind=%s",
                tool_name,
                root,
            )
            return ToolRejection(
                reason=self._rejection_message(
                    [
                        PolicyViolation(
                            code=ViolationCode.STATEMENT_NOT_ALLOWED,
                            message=(
                                f"{keyword} statements are not permitted. "
                                "Allowed: " + ", ".join(sorted(allowed)) + "."
                            ),
                        )
                    ]
                )
            )
        return None


def _set_field(args: Any, field: str, value: str) -> None:
    """Write a value back onto a Pydantic args object or a dict."""
    if isinstance(args, dict):
        args[field] = value
    else:
        setattr(args, field, value)
