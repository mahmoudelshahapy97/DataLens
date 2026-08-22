"""Fusing a lexical and a semantic index.

Combined with Reciprocal Rank Fusion, not by adding scores. BM25 produces
unbounded positive numbers whose scale depends on corpus statistics; cosine
similarity produces values in [-1, 1]. Adding or averaging them means whichever
happens to have the larger range silently decides every ranking, and the
weighting drifts as the corpus grows.

RRF uses only *rank*, so it needs no calibration, cannot be destabilised by an
outlier score, and degrades to the surviving index when one returns nothing.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Sequence

from .base import IndexDocument, IndexHit, SearchIndex

logger = logging.getLogger(__name__)

#: RRF's damping constant. 60 is the value from the original paper and the one
#: every implementation uses; it makes the difference between rank 1 and 2
#: meaningful without letting rank 1 dominate outright.
_RRF_K = 60


class HybridIndex(SearchIndex):
    """Two indexes, one ranking.

    Args:
        indexes: Searched in order. Writes go to all of them; a failure in one
            is logged and skipped rather than raised, because a vector store
            being briefly unreachable should degrade ranking, not break search.
        weights: Optional per-index multiplier on the RRF contribution, for
            deployments that trust one side more.
    """

    name = "hybrid"

    def __init__(
        self,
        indexes: Sequence[SearchIndex],
        *,
        weights: Optional[Sequence[float]] = None,
    ) -> None:
        if not indexes:
            raise ValueError("HybridIndex needs at least one index")
        self.indexes = list(indexes)
        self.weights = list(weights or [1.0] * len(self.indexes))
        self.name = "hybrid(" + "+".join(i.name for i in self.indexes) + ")"

    # -- writes --------------------------------------------------------

    def add(self, documents: Iterable[IndexDocument]) -> None:
        materialised = list(documents)
        for index in self.indexes:
            try:
                index.add(materialised)
            except Exception as exc:
                logger.warning("Index %s rejected a write: %s", index.name, exc)

    def remove(self, ids: Iterable[str]) -> None:
        materialised = list(ids)
        for index in self.indexes:
            try:
                index.remove(materialised)
            except Exception as exc:
                logger.warning("Index %s rejected a delete: %s", index.name, exc)

    def clear(self, *, tenant_id: Optional[str] = None) -> None:
        for index in self.indexes:
            try:
                index.clear(tenant_id=tenant_id)
            except Exception as exc:
                logger.warning("Index %s could not be cleared: %s", index.name, exc)

    # -- reads ---------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        limit: int = 10,
        kind: Optional[str] = None,
    ) -> List[IndexHit]:
        fused: Dict[str, float] = {}
        seen: Dict[str, IndexHit] = {}

        for index, weight in zip(self.indexes, self.weights):
            try:
                # Over-fetch: a document ranked 15th by one index and 2nd by the
                # other should still surface, and it cannot if we only look at
                # each index's top `limit`.
                hits = index.search(
                    query, tenant_id=tenant_id, limit=limit * 3, kind=kind
                )
            except Exception as exc:
                logger.warning("Index %s failed to search: %s", index.name, exc)
                continue

            for rank, hit in enumerate(hits, start=1):
                fused[hit.id] = fused.get(hit.id, 0.0) + weight / (_RRF_K + rank)
                seen.setdefault(hit.id, hit)

        ranked = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
        return [
            IndexHit(
                id=document_id,
                score=score,
                kind=seen[document_id].kind,
                metadata=seen[document_id].metadata,
            )
            for document_id, score in ranked[:limit]
        ]

    def __len__(self) -> int:
        return max((len(index) for index in self.indexes), default=0)
