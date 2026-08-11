"""Generation lineage: what the agent produced, and whether it was right.

    from vanna.core.generation import LocalGenerationStore, SqlGeneration

    store = LocalGenerationStore("./generations.jsonl")
    await store.record(ctx, SqlGeneration(question=q, sql=sql, status=...))

    print((await store.stats(ctx)).summary())
    for candidate in await store.promotable(ctx):
        ...  # offer for promotion into the verified example store
"""

from .base import (
    GenerationStore,
    LocalGenerationStore,
    recent_window,
)
from .models import (
    Feedback,
    GenerationStats,
    GenerationStatus,
    SqlGeneration,
)

__all__ = [
    "GenerationStore",
    "LocalGenerationStore",
    "SqlGeneration",
    "GenerationStatus",
    "GenerationStats",
    "Feedback",
    "recent_window",
]
