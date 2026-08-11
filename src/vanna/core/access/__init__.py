"""Row- and column-level access control.

    from vanna.core.access import AccessControlToolRegistry, SessionPropertyResolver

    registry = AccessControlToolRegistry(
        policy=SqlPolicy(),
        dialect="postgres",
        catalog=catalog,
        manifest=manifest,
        session_resolver=SessionPropertyResolver({
            "session_region": "metadata.region",
        }),
    )

Rules live in the manifest and are enforced in ``transform_args``, which every
tool call passes through -- so chat, MCP, dashboards and evaluations are all
covered without any of them knowing this package exists.
"""

from .registry import AccessControlToolRegistry, preview_sql
from .rules import AccessDecision, apply_access_rules, apply_column_rules, apply_row_rules
from .session import SessionProperties, SessionPropertyResolver, require

__all__ = [
    "AccessControlToolRegistry",
    "preview_sql",
    "SessionPropertyResolver",
    "SessionProperties",
    "require",
    "apply_access_rules",
    "apply_row_rules",
    "apply_column_rules",
    "AccessDecision",
]
