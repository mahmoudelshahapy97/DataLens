"""Turning a workspace's grant policy into actual grant rows.

One function, used by both the admin route and the first-use path, so the two
cannot drift into granting different things.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger("vanna.grants.defaults")


async def materialize(
    *,
    store: Any,
    policy_store: Any,
    context: Any,
    tenant_id: str,
    data_source_id: str,
    catalog: Any,
    roles: Optional[Sequence[str]] = None,
    mode: str = "fill",
    only_tables: Optional[Sequence[str]] = None,
    granted_by: str = "",
) -> Dict[str, Dict[str, int]]:
    """Write the rows this workspace's policy implies. Returns per-role counts.

    Tables come from the **scanned catalog**, never a live query against the
    warehouse. The catalog is what the write policy is built from, so granting
    from anything else would grant columns the policy cannot see -- the same
    reasoning `routes/grants.py::_columns_of` gives for autofill.

    A role whose preset is ``none``, or names something unregistered, is skipped
    rather than treated as an error: an unknown preset should leave permissions
    exactly as they were, not clear them.
    """
    from vanna.core.grants import get_preset, preset_grants

    policies = await policy_store.get(tenant_id, data_source_id)
    wanted = (
        [policies[r] for r in roles if r in policies]
        if roles is not None
        else list(policies.values())
    )

    try:
        # `data_source_id` matters here: a preset expanded over every database
        # the tenant has would grant one database's tables on another.
        tables = await catalog.get_tables(context, data_source_id=data_source_id) or []
    except Exception as exc:
        logger.warning(
            "Cannot apply grant defaults for %s: the catalog is unavailable (%s)",
            tenant_id, exc,
        )
        return {}

    applied: Dict[str, Dict[str, int]] = {}
    for policy in wanted:
        preset = get_preset(policy.preset)
        if preset is None or preset.name == "none":
            continue

        table_grants, column_grants = preset_grants(
            preset,
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            role=policy.role,
            tables=tables,
            only=only_tables,
        )
        counts = await store.apply_preset(
            context,
            data_source_id=data_source_id,
            role=policy.role,
            table_grants=table_grants,
            column_grants=column_grants,
            mode=mode,
            granted_by=granted_by or "system:preset",
        )
        version = await store.version(context, data_source_id=data_source_id)
        await policy_store.mark_applied(
            tenant_id, data_source_id, policy.role, version=version
        )
        applied[policy.role] = counts
        logger.info(
            "Applied preset %s to %s/%s for role %s: %s",
            preset.name, tenant_id, data_source_id, policy.role, counts,
        )

    return applied


async def apply_to_new_tables(
    *,
    store: Any,
    policy_store: Any,
    context: Any,
    tenant_id: str,
    data_source_id: str,
    catalog: Any,
) -> None:
    """Extend the defaults over whatever the latest scan found.

    Called after a successful scan, and never allowed to be fatal. A workspace
    that cannot apply its defaults must still boot, and the failure direction is
    "nothing granted", which is the safe one.
    """
    try:
        pending = await policy_store.pending_for_new_tables(tenant_id, data_source_id)
        if not pending:
            return
        await materialize(
            store=store,
            policy_store=policy_store,
            context=context,
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            catalog=catalog,
            roles=[p.role for p in pending],
            mode="fill",
            granted_by="system:preset",
        )
    except Exception as exc:
        logger.error(
            "Could not apply grant defaults for %s: %s", tenant_id, exc, exc_info=True
        )
