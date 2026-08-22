"""Plans, and how a workspace's limits are decided.

Plans are **code**, subscriptions are **data**. A plan is a product decision that ships
with a release and should be reviewed like one; making it a table invites someone raising
a limit in production at 2am with no diff and no record of why.

The resolution order below is the whole module, and the order matters more than the
values:

    explicit override  ->  active subscription's plan  ->  deployment default

An override beats a plan so support can raise one workspace's ceiling without inventing a
plan for it — which is otherwise how a pricing table acquires a "Enterprise (Bob)" tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class Plan:
    """What a plan entitles a workspace to."""

    name: str
    label: str
    daily_quota: int
    """Questions per rolling 24 hours, across the whole workspace."""
    max_rows: int
    """Row cap per query."""
    description: str = ""


#: The catalogue. `free` is the fallback for every workspace with no subscription, and
#: an expired subscription lands back here rather than at zero -- losing access to your
#: own data because a card expired is a support incident, not a business model.
PLANS: Dict[str, Plan] = {
    "free": Plan(
        name="free",
        label="Free",
        daily_quota=200,
        max_rows=1_000,
        description="For evaluating, and for small teams.",
    ),
    "pro": Plan(
        name="pro",
        label="Pro",
        daily_quota=5_000,
        max_rows=100_000,
        description="For teams using this daily.",
    ),
    "enterprise": Plan(
        name="enterprise",
        label="Enterprise",
        daily_quota=100_000,
        max_rows=1_000_000,
        description="Negotiated limits.",
    ),
}

DEFAULT_PLAN = "free"

#: Subscription states. `past_due` still grants the plan: a payment problem should
#: interrupt billing, not analytics, and the grace period is what stops a failed card
#: becoming an outage.
ACTIVE_STATUSES = frozenset({"active", "trialing", "past_due"})


def get_plan(name: Optional[str]) -> Plan:
    """The named plan, or free. An unknown name never fails a request."""
    return PLANS.get((name or "").strip().lower(), PLANS[DEFAULT_PLAN])


def _expired(subscription: Mapping[str, Any]) -> bool:
    expires_at = subscription.get("expires_at")
    if not expires_at:
        return False  # open-ended
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < datetime.now(timezone.utc)


def plan_for(subscription: Optional[Mapping[str, Any]]) -> Plan:
    """The plan a subscription currently grants.

    Cancelled or expired falls back to free rather than to nothing.
    """
    if not subscription:
        return PLANS[DEFAULT_PLAN]
    if subscription.get("status") not in ACTIVE_STATUSES:
        return PLANS[DEFAULT_PLAN]
    if _expired(subscription):
        return PLANS[DEFAULT_PLAN]
    return get_plan(subscription.get("plan"))


@dataclass(frozen=True)
class Limits:
    """The limits in force for one workspace, and where each came from.

    The provenance is not decoration: "why is this workspace capped at 200?" is the
    question support actually gets, and answering it should not require reading code.
    """

    daily_quota: int
    max_rows: int
    plan: Plan
    quota_source: str   # "override" | "plan" | "default"
    rows_source: str


def resolve_limits(
    tenant: Optional[Mapping[str, Any]],
    subscription: Optional[Mapping[str, Any]],
    *,
    default_quota: int,
    default_max_rows: int,
) -> Limits:
    """Decide a workspace's limits, and record which rule decided each."""
    plan = plan_for(subscription)
    tenant = tenant or {}

    override_quota = tenant.get("daily_quota")
    override_rows = tenant.get("max_rows")

    if override_quota:
        quota, quota_source = int(override_quota), "override"
    elif subscription:
        quota, quota_source = plan.daily_quota, "plan"
    else:
        quota, quota_source = default_quota, "default"

    if override_rows:
        rows, rows_source = int(override_rows), "override"
    elif subscription:
        rows, rows_source = plan.max_rows, "plan"
    else:
        rows, rows_source = default_max_rows, "default"

    return Limits(
        daily_quota=quota,
        max_rows=rows,
        plan=plan,
        quota_source=quota_source,
        rows_source=rows_source,
    )
