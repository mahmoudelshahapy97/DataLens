"""A search index the knowledge and catalog stores can share.

Retrieval today is Jaccard term overlap: the fraction of words a question and a
stored example have in common. That is cheap and dependency-free, and it fails
in a specific, common way -- it cannot tell that a word appearing in one table
out of four hundred is more informative than one appearing in all of them.

This interface exists so that ranking can be improved without every store
learning about embeddings. The default implementation is still dependency-free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class IndexDocument:
    """Something searchable.

    Args:
        id: Stable identifier the caller uses to find the real object.
        text: What gets matched against a query.
        kind: ``example``, ``table``, ``column_values`` -- lets one index serve
            several stores without them colliding.
        tenant_id: Scope. Every search filters on it; an index that ignores
            tenancy leaks one customer's questions into another's suggestions.
        boost: Multiplier applied to the score. Used to prefer verified
            examples over candidates.
        metadata: Carried through to the hit, so a caller can rank further
            without a second lookup.
    """

    id: str
    text: str
    kind: str = "example"
    tenant_id: str = "default"
    boost: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IndexHit:
    """One result."""

    id: str
    score: float
    kind: str = "example"
    metadata: Dict[str, Any] = field(default_factory=dict)


class SearchIndex(ABC):
    """Ranked lookup over documents, scoped by tenant."""

    #: Reported on ``/schema`` so an operator can see which backend is really
    #: serving retrieval, rather than which one they configured.
    name: str = "base"

    @abstractmethod
    def add(self, documents: Iterable[IndexDocument]) -> None:
        """Insert or replace documents by id."""

    @abstractmethod
    def remove(self, ids: Iterable[str]) -> None:
        """Delete documents by id."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        limit: int = 10,
        kind: Optional[str] = None,
    ) -> List[IndexHit]:
        """Best matches, highest score first."""

    def clear(self, *, tenant_id: Optional[str] = None) -> None:
        """Drop everything, or one tenant's documents."""
        raise NotImplementedError

    def fingerprints(
        self, *, tenant_id: str = "default", kind: Optional[str] = None
    ) -> Optional[Dict[str, str]]:
        """``{document_id: content hash}`` for what is stored, or None.

        Optional, and the distinction it draws is the important one:

        * **None** means "I cannot tell you cheaply". The caller then rebuilds
          from scratch, which is right for an in-process index where building is
          sub-millisecond and a stale entry is the worse failure.
        * **A mapping** lets :func:`vanna.capabilities.index.sync_documents`
          re-embed only what changed. For a persistent or remote index that is
          not an optimisation but a requirement -- rebuilding per search means
          re-embedding the whole corpus for every question asked.
        """
        return None

    def __len__(self) -> int:  # pragma: no cover - diagnostics
        return 0
