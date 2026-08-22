"""Subscriptions and payments.

Two decisions shape this file.

**Subscriptions attach to the workspace, not the person.** The quota is enforced per
workspace; billing one thing while metering another guarantees the two disagree the
first time a workspace has two members.

**Payments go through a provider seam with an offline implementation by default.**
This stack runs against local databases with no internet, so hard-wiring Stripe
would make billing untestable here and unusable in an air-gapped deployment.
``ManualProvider`` records a payment somebody took by other means and extends the
subscription; a Stripe implementation is the same interface with a network call.

One thing changed: **who may use any of this**. Setting a plan and recording a
payment used to require only tenant admin, so a workspace admin could POST
``{"plan": "enterprise"}`` and grant themselves a five-hundred-fold quota increase
for nothing. Those routes are platform-admin now; a workspace admin can still *see*
what their workspace is on and what it has been charged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .db import SCHEMA
from .tenancy import _iso

logger = logging.getLogger("vanna.billing")


class Billing:
    """Subscription and payment storage."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def get_subscription(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        """The workspace's current subscription, or None.

        Newest first, so a renewal that created a fresh row wins over the one it
        replaced without anything having to delete history.
        """
        row = await self.db.fetch_one(
            f"""SELECT * FROM {SCHEMA}.subscriptions
                 WHERE tenant_id = %s
                 ORDER BY created_at DESC LIMIT 1""",
            (tenant_id,),
        )
        if row:
            row["id"] = str(row["id"])
            for key in ("starts_at", "expires_at", "cancelled_at", "created_at"):
                row[key] = _iso(row[key])
        return row

    async def set_subscription(
        self,
        tenant_id: str,
        plan: str,
        *,
        months: int = 1,
        status: str = "active",
    ) -> Dict[str, Any]:
        """Start or replace a workspace's subscription."""
        from vanna.core.billing import PLANS

        plan = (plan or "").strip().lower()
        if plan not in PLANS:
            raise ValueError(f"Unknown plan {plan!r}. Known: {', '.join(sorted(PLANS))}")
        if months < 0:
            raise ValueError("months cannot be negative")

        await self.db.execute(
            f"""INSERT INTO {SCHEMA}.subscriptions (tenant_id, plan, status, expires_at)
                VALUES (%s, %s, %s,
                        CASE WHEN %s::int IS NULL OR %s::int = 0 THEN NULL
                             ELSE now() + make_interval(months => %s::int) END)""",
            (tenant_id, plan, status, months, months, months),
        )
        logger.info("Subscription for %s set to %s (%s months)", tenant_id, plan, months)
        return await self.get_subscription(tenant_id)  # type: ignore[return-value]

    async def cancel_subscription(self, tenant_id: str) -> bool:
        """Cancel, without deleting.

        The row stays so the history reads correctly, and because a cancelled
        subscription inside its paid period should keep working -- which the plan
        resolver decides, not this method.
        """
        return bool(
            await self.db.execute(
                f"""UPDATE {SCHEMA}.subscriptions
                       SET status = 'cancelled', cancelled_at = now()
                     WHERE tenant_id = %s AND status <> 'cancelled'""",
                (tenant_id,),
            )
        )

    async def extend(self, tenant_id: str, months: int) -> Optional[Dict[str, Any]]:
        """Push the expiry out, from whichever is later: now, or the current expiry.

        Extending from ``now()`` when the subscription still has a month left would
        quietly take that month away from somebody who renewed early.
        """
        await self.db.execute(
            f"""UPDATE {SCHEMA}.subscriptions
                   SET expires_at = GREATEST(COALESCE(expires_at, now()), now())
                                    + make_interval(months => %s),
                       status = 'active', cancelled_at = NULL
                 WHERE tenant_id = %s
                   AND created_at = (SELECT max(created_at) FROM {SCHEMA}.subscriptions
                                      WHERE tenant_id = %s)""",
            (months, tenant_id, tenant_id),
        )
        return await self.get_subscription(tenant_id)

    # ------------------------------------------------------------------
    # Payments
    # ------------------------------------------------------------------

    async def record_payment(
        self,
        tenant_id: str,
        *,
        provider: str,
        provider_ref: str,
        amount_cents: int,
        currency: str = "usd",
        description: str = "",
        status: str = "succeeded",
    ) -> bool:
        """Record a payment. Returns False if it was already recorded.

        ``provider_ref`` is unique, so a replayed webhook -- or an operator clicking
        twice -- cannot bill the same payment again or extend a subscription twice.
        """
        affected = await self.db.execute(
            f"""INSERT INTO {SCHEMA}.payments
                    (tenant_id, provider, provider_ref, amount_cents, currency,
                     description, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (provider_ref) DO NOTHING""",
            (
                tenant_id,
                provider,
                provider_ref,
                int(amount_cents),
                (currency or "usd").lower(),
                description,
                status,
            ),
        )
        if not affected:
            logger.info("Payment %s already recorded; ignoring replay", provider_ref)
        return bool(affected)

    async def list_payments(self, tenant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT id, provider, provider_ref, amount_cents, currency,
                       description, status, created_at
                  FROM {SCHEMA}.payments
                 WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s""",
            (tenant_id, limit),
        )
        for row in rows:
            row["id"] = str(row["id"])
            row["created_at"] = _iso(row["created_at"])
        return rows


# ----------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------


class PaymentProvider:
    """How money is taken.

    An interface rather than a Stripe call so the default deployment -- local
    databases, no internet -- has a billing system that works and can be exercised.
    """

    name = "manual"

    async def charge(
        self, tenant_id: str, plan: str, months: int, **kwargs: Any
    ) -> Dict[str, Any]:
        """Take payment. Returns ``{provider_ref, amount_cents, currency, status}``."""
        raise NotImplementedError


class ManualProvider(PaymentProvider):
    """Payment taken elsewhere and recorded here by an operator.

    Invoice, bank transfer, or an internal arrangement. The reference is supplied by
    whoever recorded it and is what makes the entry idempotent.
    """

    name = "manual"

    async def charge(
        self, tenant_id: str, plan: str, months: int, **kwargs: Any
    ) -> Dict[str, Any]:
        reference = str(kwargs.get("reference") or "").strip()
        if not reference:
            raise ValueError(
                "A payment reference is required -- it is what stops the same "
                "payment being recorded twice."
            )
        amount = kwargs.get("amount_cents")
        try:
            amount_cents = int(amount or 0)
        except (TypeError, ValueError):
            raise ValueError("amount_cents must be a whole number of cents.")
        if amount_cents < 0:
            raise ValueError("amount_cents cannot be negative.")

        return {
            "provider_ref": f"manual:{tenant_id}:{reference}",
            "amount_cents": amount_cents,
            "currency": str(kwargs.get("currency") or "usd"),
            "status": "succeeded",
        }


def build_provider(name: str = "") -> PaymentProvider:
    """Resolve the configured provider.

    Only ``manual`` ships. A Stripe implementation belongs behind an optional extra
    and a network call; naming an unimplemented one here would be a runtime
    surprise, so an unknown name falls back with a warning rather than failing at
    boot.
    """
    wanted = (name or "manual").strip().lower()
    if wanted != "manual":
        logger.warning(
            "Payment provider %r is not implemented; using manual entry. "
            "Implement PaymentProvider and register it here.",
            wanted,
        )
    return ManualProvider()
