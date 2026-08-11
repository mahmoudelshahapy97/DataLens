"""The single place row- and column-level access is enforced.

``ToolRegistry.transform_args`` has documented itself as the seam for row-level
security since the first version of this framework, and returned its arguments
unchanged. This is the implementation.

Everything that can run SQL goes through ``ToolRegistry.execute``, so putting
enforcement here means chat, MCP, dashboards, and evaluation runs are all
covered by construction -- and a tool someone adds next year is covered without
knowing this file exists. Enforcement anywhere else would have to be repeated
per entry point, and the one that gets forgotten is the breach.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Union

from ...semantic.compiler import CompiledSql, compile_sql
from ...semantic.models import Manifest
from ..errors import ErrorCode, ErrorPhase, VannaError
from ..sql_policy.semantic import SemanticSqlPolicyToolRegistry
from ..tool.models import ToolContext, ToolRejection
from ..user.models import User
from .rules import apply_access_rules
from .session import SessionPropertyResolver

logger = logging.getLogger(__name__)


class AccessControlToolRegistry(SemanticSqlPolicyToolRegistry):
    """Compiles with the caller's row and column rules applied.

    Args:
        session_resolver: Maps a user onto the properties rules read. Without
            one, only rules using built-in properties (``@tenant_id``,
            ``@user_email``, ``@groups``) can resolve.
        audit_access: Log every rule application. On by default -- "which rows
            was this person allowed to see, and when" is the question an
            incident review opens with.
    """

    def __init__(
        self,
        *args: Any,
        session_resolver: Optional[SessionPropertyResolver] = None,
        audit_access: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.session_resolver = session_resolver or SessionPropertyResolver()
        self.audit_access = audit_access

    # ------------------------------------------------------------------

    def compile_for(
        self, sql: str, user: User, context: ToolContext
    ) -> Optional[CompiledSql]:
        """Compile against a manifest narrowed to what this caller may read."""
        manifest = self._manifest_for(user, context)
        if manifest is None:
            return None

        properties = self.session_resolver.resolve(user, context)
        decision = apply_access_rules(manifest, properties)

        compiled = compile_sql(
            sql,
            decision.manifest,
            dialect=self.dialect or "",
            fanout_guard=self.fanout_guard,
        )

        compiled.applied_row_rules = decision.applied_row_rules
        compiled.dropped_columns = decision.dropped_columns

        if self.audit_access and (decision.applied_row_rules or decision.dropped_columns):
            logger.info(
                "Access control applied user=%s tenant=%s rows=%s hidden=%s",
                user.id,
                getattr(context, "tenant_id", "default"),
                decision.applied_row_rules or "-",
                decision.dropped_columns or "-",
            )
            context.metadata.setdefault("access", {}).update(
                {
                    "row_rules": decision.applied_row_rules,
                    "dropped_columns": decision.dropped_columns,
                }
            )

        return compiled

    # ------------------------------------------------------------------

    async def transform_args(
        self,
        tool: Any,
        args: Any,
        user: User,
        context: ToolContext,
    ) -> Union[Any, ToolRejection]:
        try:
            return await super().transform_args(tool, args, user, context)
        except VannaError as exc:
            if exc.phase is not ErrorPhase.ACCESS_CONTROL:
                raise
            # An unresolvable required property means we cannot prove what this
            # caller may see. Refuse the tool call rather than let it run
            # unfiltered -- the failure direction matters more than the message.
            logger.warning(
                "Access denied user=%s tenant=%s: %s",
                user.id,
                getattr(context, "tenant_id", "default"),
                exc,
            )
            reason = exc.args[0] if exc.args else str(exc)
            return ToolRejection(reason=reason)


def preview_sql(
    registry: AccessControlToolRegistry,
    sql: str,
    user: User,
    context: ToolContext,
) -> CompiledSql:
    """What a given user's query would actually become.

    The admin-facing counterpart to enforcement: rather than inferring from an
    empty result set that a rule fired, an operator can see the predicate. This
    is the difference between access control that can be reviewed and access
    control that is merely believed.
    """
    compiled = registry.compile_for(sql, user, context)
    if compiled is None:
        raise VannaError(
            ErrorCode.MISCONFIGURED,
            "No semantic manifest is configured, so there is nothing to preview.",
            phase=ErrorPhase.ACCESS_CONTROL,
        )
    return compiled


from ..errors import ErrorCode  # noqa: E402  (used only by preview_sql)
