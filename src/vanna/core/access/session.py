"""Session properties: the values access rules are evaluated against.

``region = @session_region`` needs a value for ``@session_region``, and where
that value comes from is the whole security question. It comes from the resolved
:class:`User` -- never from the request body, never from a tool argument. A
session property a caller can set is not a control, it is a suggestion.

The mapping from property name to user attribute is declarative, configured per
project, so adding a new rule does not mean writing code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from ...semantic.models import SessionProperty
from ..errors import ErrorCode, ErrorPhase, VannaError
from ..tool.models import ToolContext
from ..user.models import User

logger = logging.getLogger(__name__)

#: Attributes readable from a User without configuration. Anything else has to
#: be mapped explicitly, so a typo in a rule fails loudly instead of resolving
#: to an unrelated attribute.
_BUILT_IN = {
    "user_id": lambda user, ctx: user.id,
    "user_email": lambda user, ctx: user.email,
    "username": lambda user, ctx: user.username,
    "tenant_id": lambda user, ctx: getattr(ctx, "tenant_id", None) or user.tenant_id,
    "groups": lambda user, ctx: list(user.group_memberships or []),
}


@dataclass
class SessionProperties:
    """Resolved values for one request."""

    values: Dict[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> Any:
        return self.values.get(name.lower())

    def has(self, name: str) -> bool:
        value = self.values.get(name.lower())
        return value is not None and value != ""

    def __contains__(self, name: str) -> bool:
        return self.has(name)


class SessionPropertyResolver:
    """Turns a user into the properties rules can read.

    Args:
        mapping: Property name -> where to read it. Values are either a
            built-in name (``tenant_id``, ``groups``, ...) or a
            ``metadata.<key>`` path into ``User.metadata``.

    Example::

        SessionPropertyResolver({
            "session_region": "metadata.region",
            "session_tenant": "tenant_id",
            "session_level":  "metadata.clearance",
        })
    """

    def __init__(self, mapping: Optional[Mapping[str, str]] = None) -> None:
        self.mapping = {k.lower(): v for k, v in (mapping or {}).items()}

    def resolve(self, user: User, context: ToolContext) -> SessionProperties:
        values: Dict[str, Any] = {}

        for name, source in self.mapping.items():
            values[name] = self._read(source, user, context)

        # Built-ins are available under their own names too, so a rule can say
        # `@tenant_id` without any configuration at all.
        for name, reader in _BUILT_IN.items():
            values.setdefault(name, reader(user, context))

        return SessionProperties(values)

    @staticmethod
    def _read(source: str, user: User, context: ToolContext) -> Any:
        if source in _BUILT_IN:
            return _BUILT_IN[source](user, context)

        if source.startswith("metadata."):
            key = source.split(".", 1)[1]
            return (user.metadata or {}).get(key)

        logger.warning(
            "Session property source %r is not recognised; treating as unset. "
            "Use a built-in name or metadata.<key>.",
            source,
        )
        return None


def require(
    properties: SessionProperties,
    declared: List[SessionProperty],
    *,
    rule_name: str,
) -> Dict[str, Any]:
    """Check every required property is present, or refuse.

    Fails closed, always. The tempting alternative -- skip the rule when its
    property is missing -- means a misconfiguration silently returns *more*
    data, which is the one direction an access-control bug must never fail in.
    """
    resolved: Dict[str, Any] = {}

    for property_ in declared:
        name = property_.name.lower()
        if properties.has(name):
            resolved[name] = properties.get(name)
            continue

        if property_.default_expr is not None:
            resolved[name] = property_.default_expr
            continue

        if property_.required:
            raise VannaError(
                ErrorCode.PERMISSION_DENIED,
                f"Access rule {rule_name!r} needs {property_.name!r}, which is "
                "not set for this user.",
                phase=ErrorPhase.ACCESS_CONTROL,
                hint=(
                    "Populate it on the user (metadata) and map it in the "
                    "project's session_properties."
                ),
                metadata={"rule": rule_name, "property": property_.name},
            )

        resolved[name] = None

    return resolved
