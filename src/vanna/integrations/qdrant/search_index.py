"""A :class:`SearchIndex` backed by Qdrant.

``resolve_index`` looks for a ``build_index`` factory in
``vanna.integrations.<name>.search_index``. Until now no integration had one, so
every deployment that asked for a vector backend quietly got keyword-only BM25.
This is that module for Qdrant.

Three things it is careful about.

**Its own collection.** ``QdrantAgentMemory`` stores tool-usage memories in
``tool_memories``; this uses ``vanna_knowledge``. They have different lifecycles
and different owners, and sharing one would mean clearing an agent's memory
silently emptied the example index.

**Tenancy on every operation.** ``tenant_id`` is a payload field, filtered on
every search and every delete. An index that ignores it leaks one customer's
questions into another's suggestions.

**Failures degrade.** ``HybridIndex`` catches per-index errors so a vector store
that is briefly unreachable costs ranking quality rather than answers; this
adapter holds up its end by not raising for anything recoverable.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Iterable, List, Optional

from ...capabilities.index.base import IndexDocument, IndexHit, SearchIndex
from ...capabilities.index.embeddings import Embedder, build_embedder

logger = logging.getLogger(__name__)

#: Kept apart from QdrantAgentMemory's `tool_memories`. See the module docstring.
COLLECTION = os.getenv("VANNA_QDRANT_COLLECTION", "vanna_knowledge")

#: Namespace for deterministic point ids. Qdrant wants a UUID or an integer, and
#: our document ids are strings like `example:abc123` -- hashing them into a
#: fixed namespace means re-indexing a document *updates* its point instead of
#: adding a second copy of it.
_NAMESPACE = uuid.UUID("6f1a0d6e-4a1e-4e8a-9a2b-3c5d7e9f1a2b")


def _point_id(document_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, document_id))


class QdrantSearchIndex(SearchIndex):
    """Semantic search over Qdrant.

    Args:
        embedder: Turns text into vectors. Built by default from
            :func:`vanna.capabilities.index.embeddings.build_embedder`.
        url: Qdrant server URL. Falls back to ``VANNA_QDRANT_URL``; with neither,
            an embedded on-disk instance at ``path`` is used.
        path: Embedded-mode directory. Only consulted when there is no URL.
        collection: Collection name.
    """

    name = "qdrant"

    def __init__(
        self,
        embedder: Embedder,
        *,
        url: Optional[str] = None,
        path: Optional[str] = None,
        api_key: Optional[str] = None,
        collection: str = COLLECTION,
    ) -> None:
        self.embedder = embedder
        self.collection = collection
        self._url = url or os.getenv("VANNA_QDRANT_URL", "") or None
        self._path = path or os.getenv("VANNA_QDRANT_PATH", "") or None
        self._api_key = api_key or os.getenv("VANNA_QDRANT_API_KEY", "") or None
        self._client = None

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client

        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        if self._url:
            client = QdrantClient(url=self._url, api_key=self._api_key)
        else:
            # Embedded mode. Fine for one process; a second process opening the
            # same directory will fail to acquire the lock, which is why the
            # compose file runs a server.
            client = QdrantClient(path=self._path or ":memory:")

        existing = {c.name for c in client.get_collections().collections}
        if self.collection not in existing:
            client.create_collection(
                collection_name=self.collection,
                # Size comes from the embedder, never a constant: a collection
                # created at the wrong width fails at insert with a message
                # about nothing.
                vectors_config=VectorParams(
                    size=self.embedder.dimension, distance=Distance.COSINE
                ),
            )
            logger.info(
                "Created Qdrant collection %s (%d dims, cosine)",
                self.collection,
                self.embedder.dimension,
            )

        self._client = client
        return client

    def _filter(self, tenant_id: Optional[str], kind: Optional[str] = None):
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = []
        if tenant_id is not None:
            conditions.append(
                FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id))
            )
        if kind is not None:
            conditions.append(
                FieldCondition(key="kind", match=MatchValue(value=kind))
            )
        return Filter(must=conditions) if conditions else None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(self, documents: Iterable[IndexDocument]) -> None:
        """Insert or replace documents.

        Embedding happens in one batch for the whole call -- the reason
        :func:`sync_documents` exists is to make sure this is only ever handed
        the documents that actually changed.
        """
        items = list(documents)
        if not items:
            return

        from qdrant_client.models import PointStruct

        client = self._get_client()
        vectors = self.embedder.embed([d.text for d in items])

        points = [
            PointStruct(
                id=_point_id(document.id),
                vector=vector,
                payload={
                    "doc_id": document.id,
                    "text": document.text,
                    "kind": document.kind,
                    "tenant_id": document.tenant_id,
                    "boost": document.boost,
                    # The content hash sync() compares against. Stored in the
                    # payload so the index can answer "what do you already have"
                    # without re-reading the source.
                    "fingerprint": document.metadata.get("fingerprint", ""),
                    "metadata": document.metadata,
                },
            )
            for document, vector in zip(items, vectors)
        ]
        client.upsert(collection_name=self.collection, points=points, wait=True)
        logger.debug("Indexed %d document(s) into %s", len(points), self.collection)

    def remove(self, ids: Iterable[str]) -> None:
        document_ids = list(ids)
        if not document_ids:
            return
        client = self._get_client()
        client.delete(
            collection_name=self.collection,
            points_selector=[_point_id(i) for i in document_ids],
            wait=True,
        )

    def clear(self, *, tenant_id: Optional[str] = None) -> None:
        """Drop one tenant's documents, or everything.

        Scoped by filter rather than by dropping the collection: recreating it
        would race any other tenant's search running at the same moment.
        """
        client = self._get_client()
        client.delete(
            collection_name=self.collection,
            points_selector=self._filter(tenant_id),
            wait=True,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        limit: int = 10,
        kind: Optional[str] = None,
    ) -> List[IndexHit]:
        client = self._get_client()
        vector = self.embedder.embed_query(query)
        if not vector:
            return []

        response = client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=self._filter(tenant_id, kind),
            limit=limit,
            with_payload=True,
        )

        hits: List[IndexHit] = []
        for point in getattr(response, "points", response) or []:
            payload = point.payload or {}
            # Cosine similarity in [-1, 1]; the boost mirrors what LexicalIndex
            # does so verified examples outrank candidates in either backend.
            score = float(point.score) * float(payload.get("boost", 1.0) or 1.0)
            hits.append(
                IndexHit(
                    id=str(payload.get("doc_id") or point.id),
                    score=score,
                    kind=str(payload.get("kind") or "example"),
                    metadata=payload.get("metadata") or {},
                )
            )
        return hits

    def fingerprints(
        self, *, tenant_id: str = "default", kind: Optional[str] = None
    ) -> Dict[str, str]:
        """``{document_id: fingerprint}`` for everything currently stored.

        This is what makes :func:`sync_documents` incremental. Without it the
        only safe strategy is clear-and-add, which against a persistent store
        means re-embedding the entire corpus on every search.
        """
        client = self._get_client()
        found: Dict[str, str] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=self.collection,
                scroll_filter=self._filter(tenant_id, kind),
                limit=512,
                with_payload=["doc_id", "fingerprint"],
                with_vectors=False,
                offset=offset,
            )
            for point in points:
                payload = point.payload or {}
                document_id = payload.get("doc_id")
                if document_id:
                    found[str(document_id)] = str(payload.get("fingerprint") or "")
            if offset is None:
                break
        return found

    def __len__(self) -> int:  # pragma: no cover - diagnostics
        try:
            return int(self._get_client().count(self.collection, exact=True).count)
        except Exception:  # noqa: BLE001
            return 0


def build_index() -> Optional[QdrantSearchIndex]:
    """Factory ``resolve_index`` calls. None means "cannot be built".

    Returning None rather than raising is what lets ``resolve_index`` log a loud
    downgrade and carry on with lexical search, instead of a missing optional
    dependency taking the whole deployment down.
    """
    embedder = build_embedder()
    if embedder is None:
        return None
    try:
        index = QdrantSearchIndex(embedder)
        index._get_client()      # connect now, so a failure is a boot-time warning
        return index
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant index unavailable: %s: %s", type(exc).__name__, exc)
        return None
