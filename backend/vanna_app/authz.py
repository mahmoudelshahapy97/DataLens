"""Who may do what. One implementation, used everywhere.

This logic previously existed three times -- in the user resolver, in the portal
routes, and in the auth routes -- and all three spelled it the same dangerous way::

    is_platform_admin = (not ADMIN_EMAILS) or (email in ADMIN_EMAILS)

An unset ``VANNA_ADMIN_EMAILS`` therefore made **every authenticated user a platform
admin of every workspace**: the multi-tenancy was indistinguishable from having
none. It was documented in a comment and defaulted in the compose file, which
protects exactly one deployment path and no other.

The rule here is the inverse. Admin is granted by naming an address, never by
failing to name one. A deployment with no platform admins has no platform admins,
and ``config.validate`` refuses to start a multi-tenant deployment in that state,
so the fail-closed default cannot lock anybody out either.

Demo mode is the one exception, and it is explicit: ``mode == "demo"`` grants
everyone platform admin because a zero-configuration demo has to be usable. That is
a mode somebody chose, not a variable somebody forgot.

Two tiers throughout:

* **Platform admin** -- an address in ``VANNA_ADMIN_EMAILS``. Creates and deletes
  workspaces, administers any of them, binds datasources, sets plans.
* **Tenant admin** -- ``role = 'admin'`` on a ``tenant_users`` row. Manages members,
  starters and knowledge *for their own workspace only*.

Refusals are 404, never 403. A 403 confirms the resource exists to somebody who has
no business knowing that it does.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Set

from fastapi import HTTPException

logger = logging.getLogger("vanna.authz")

ROLES = ("admin", "analyst", "viewer")

#: The response every authorisation failure gets. Identical for "does not exist"
#: and "exists but not for you", deliberately.
_NOT_FOUND = "Not found"


def email_of(user: Any) -> str:
    return (getattr(user, "email", "") or getattr(user, "id", "") or "").strip().lower()


def groups_of(user: Any) -> Set[str]:
    return set(getattr(user, "group_memberships", None) or [])


def role_of(user: Any) -> str:
    """The caller's role in their current workspace.

    Read from ``metadata``, which the resolver fills from the ``tenant_users`` row --
    never from anything the request supplied.
    """
    metadata = getattr(user, "metadata", None) or {}
    role = str(metadata.get("role") or "").lower()
    return role if role in ROLES else "analyst"


def is_platform_admin(user: Any, settings: Any) -> bool:
    """Whether this caller administers the whole deployment.

    Demo mode grants it to everyone; every other mode requires the address to be
    named in ``VANNA_ADMIN_EMAILS``.
    """
    if settings.is_demo:
        return True
    return email_of(user) in settings.admin_emails


def is_tenant_admin(user: Any, tenant_id: str, settings: Any) -> bool:
    """Whether this caller administers *this* workspace."""
    if is_platform_admin(user, settings):
        return True
    if getattr(user, "tenant_id", None) != tenant_id:
        return False
    return "admin" in groups_of(user)


def is_viewer(user: Any) -> bool:
    return role_of(user) == "viewer"


# ----------------------------------------------------------------------
# Route guards
# ----------------------------------------------------------------------


def require_platform_admin(user: Any, settings: Any) -> None:
    if not is_platform_admin(user, settings):
        logger.info(
            "Refused platform-admin action for %s (not in VANNA_ADMIN_EMAILS)",
            email_of(user) or "anonymous",
        )
        raise HTTPException(status_code=404, detail=_NOT_FOUND)


def require_tenant_admin(user: Any, tenant_id: str, settings: Any) -> None:
    if not is_tenant_admin(user, tenant_id, settings):
        logger.info(
            "Refused tenant-admin action on %s for %s",
            tenant_id, email_of(user) or "anonymous",
        )
        raise HTTPException(status_code=404, detail=_NOT_FOUND)


def require_same_tenant(user: Any, tenant_id: str, settings: Any) -> None:
    """Membership of the named workspace, at any role.

    For read routes that take a tenant in the path. A platform admin passes so an
    operator can inspect a workspace they are not a member of.
    """
    if is_platform_admin(user, settings):
        return
    if getattr(user, "tenant_id", None) != tenant_id:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)


def forbid_viewer(user: Any, what: str = "make changes") -> None:
    """Viewers read everything in their workspace and write nothing.

    403 rather than 404 here, unlike the guards above: the caller is a member and
    already knows the resource exists, so hiding it buys nothing and an accurate
    message saves a support ticket.
    """
    if is_viewer(user):
        raise HTTPException(
            status_code=403,
            detail=f"Your role in this workspace is viewer, which cannot {what}.",
        )


def require_role(user: Any, allowed: Iterable[str]) -> None:
    permitted = {r.lower() for r in allowed}
    if role_of(user) not in permitted:
        raise HTTPException(
            status_code=403,
            detail=f"This needs one of: {', '.join(sorted(permitted))}.",
        )


def require_full_session(user: Any) -> None:
    """Reject a session that exists only so its owner can change their password.

    ``must_change`` used to be a flag in the login response that only the browser
    honoured, so a temporary password granted a full 72-hour session to anything
    speaking HTTP directly. The resolver now marks restricted sessions and this
    refuses them everywhere except the two endpoints that must stay reachable.
    """
    metadata = getattr(user, "metadata", None) or {}
    if metadata.get("session_scope") == "password_change_only":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "password_change_required",
                "message": "Set a new password before continuing.",
            },
        )


def visible_tenant(user: Any, requested: Optional[str], settings: Any) -> str:
    """The tenant a request may act on.

    Routes take a tenant in the path and must never trust it. This resolves the
    requested id against the caller: a platform admin gets what they asked for,
    anybody else gets their own workspace or a 404.
    """
    own = getattr(user, "tenant_id", "") or ""
    if not requested or requested == own:
        return own
    if is_platform_admin(user, settings):
        return requested
    raise HTTPException(status_code=404, detail=_NOT_FOUND)
