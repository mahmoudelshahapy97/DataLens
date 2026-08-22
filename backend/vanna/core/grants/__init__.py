"""Per-table and per-column grants.

The authorization surface a write plan is checked against, and an optional
narrowing of the read surface. See :mod:`vanna.core.grants.models` for how this
differs from :mod:`vanna.core.access`, which decides rows rather than columns.
"""

from .base import GrantStore
from .models import (
    AUTOFILL,
    MACHINE_PREFIX,
    ColumnGrant,
    EffectiveColumn,
    EffectiveGrants,
    EffectiveTable,
    TableGrant,
    normalize_identifier,
    normalize_table,
)
from .presets import (
    BUILTIN_PRESETS,
    GrantPreset,
    get_preset,
    preset_grants,
    preset_names,
    register_preset,
)
from .resolve import catalog_write_facts, resolve_grants

__all__ = [
    "AUTOFILL",
    "MACHINE_PREFIX",
    "ColumnGrant",
    "EffectiveColumn",
    "EffectiveGrants",
    "EffectiveTable",
    "GrantStore",
    "GrantPreset",
    "BUILTIN_PRESETS",
    "get_preset",
    "preset_grants",
    "preset_names",
    "register_preset",
    "TableGrant",
    "catalog_write_facts",
    "normalize_identifier",
    "normalize_table",
    "resolve_grants",
]
