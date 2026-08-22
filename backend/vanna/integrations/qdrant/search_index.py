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
from ...capabilities.index.embeddings import (
    Embedder,
    build_embedder,
)

logger = logging.getLogger(__name__)

#: Kept apart from QdrantAgentMemory's `tool_memories`. See the module docstring.
COLLECTION = os.getenv("VANNA_QDRANT_COLLECTION", "vanna_knowledge")

#: Namespace for deterministic point ids. Qdrant wants a UUID or an integer, and
#: our document ids are strings like `example:abc123` -- hashing them into a
#: fixed namespace means re-indexing a document *updates* its point instead of
#: adding a second copy of it.
_NAMESPACE = uuid.UUID("6f1a0d6e-4a1e-4e8a-9a2b-3c5d7e9f1a2b")


#: Fixed id of the sentinel point recording which embedder wrote this collection.
#: A UUID from the same namespace as document ids, so it cannot collide with one.
_IDENTITY_POINT = str(uuid.uuid5(_NAMESPACE, "__vanna_embedder_identity__"))

#: Payload kind of the sentinel. Never searched for; present so a human reading the
#: collection can tell what the point is.
_IDENTITY_KIND = "__identity__"

#: What wrote a collection that carries no identity sentinel. Such a collection
#: predates the check, back when the only embedder was a local ONNX model -- so this
#: is a historical fact about old data, not a provider this build can use. Named
#: rather than left as None so the mismatch message tells an operator what they are
#: looking at instead of the word "None".
_PRE_SENTINEL_MODEL = "BAAI/bge-small-en-v1.5"


def _point_id(document_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, document_id))



#: Drop and recreate a collection whose width no longer matches the embedder.
#: Off by default: dropping a collection is data loss, and this code cannot tell a
#: deliberate provider change from a mistyped variable.
_RECREATE_ON_MISMATCH = os.getenv(
    "VANNA_QDRANT_RECREATE_ON_MISMATCH", "false"
).strip().lower() in ("true", "1", "yes", "on")


class VectorWidthMismatch(RuntimeError):
    """The collection's vectors are a different width from the embedder's."""


def _is_already_exists(exc: Exception) -> bool:
    """Whether a Qdrant error means "somebody else created it first".

    Matched on the HTTP status where the client exposes one, and on the message
    otherwise. Not on the exception type: `UnexpectedResponse` covers every
    non-2xx, so catching the type would swallow a genuine failure -- an
    authentication error, a wrong URL -- as a successful creation.
    """
    if getattr(exc, "status_code", None) == 409:
        return True
    return "already exist" in str(exc).lower()


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

        self._ensure_collection(client, VectorParams, Distance)

        self._client = client
        return client

    def _ensure_collection(self, client, VectorParams, Distance) -> None:
        """Create the collection if it is missing, tolerating a concurrent creator.

        Checking ``get_collections()`` and then creating is check-then-act, and with
        several API workers starting together they all check, all find it missing,
        and all create. One wins; the rest get 409 Conflict.

        That mattered more than a stray warning. The caller treats *any* exception
        from here as "Qdrant is unavailable" and downgrades the whole process to
        keyword-only retrieval -- so on a first boot two workers ran hybrid search
        and two ran lexical, and which one answered your question decided how good
        the answer was. A silent, per-worker difference in retrieval quality is
        exactly the failure this class's logging was written to make impossible.

        The check is kept because it avoids a pointless round trip in the normal
        case; the 409 is now the expected outcome of the race rather than an error.
        """
        try:
            existing = {c.name for c in client.get_collections().collections}
            if self.collection in existing:
                self._check_width(client)
                return

            client.create_collection(
                collection_name=self.collection,
                # Size comes from the embedder, never a constant: a collection
                # created at the wrong width fails at insert with a message about
                # nothing.
                vectors_config=VectorParams(
                    size=self.embedder.dimension, distance=Distance.COSINE
                ),
            )
            self._write_identity(
                client,
                f"{self.embedder.model_name}:{self.embedder.dimension}",
                self.embedder.dimension,
            )
            logger.info(
                "Created Qdrant collection %s (%d dims, cosine, %s)",
                self.collection,
                self.embedder.dimension,
                self.embedder.model_name,
            )
        except Exception as exc:
            if not _is_already_exists(exc):
                raise
            logger.debug(
                "Qdrant collection %s was created concurrently; using it.",
                self.collection,
            )

    def _check_width(self, client) -> None:
        """Refuse a collection that was written by a different embedder.

        Two distinct problems, and only one of them is about width.

        **Width.** ``text-embedding-3-small`` is 1536, ``-3-large`` is 3072, and a
        collection cannot hold both. Left unchecked this surfaces on the first
        *insert*, as a Qdrant error about vector dimensions, from inside a
        background indexing pass nobody is watching.

        **Identity, which is the subtle one.** Two models can agree on width and
        still be incompatible: ``VANNA_EMBED_DIMENSIONS=1536`` makes a truncated
        ``-3-large`` vector exactly as wide as a ``-3-small`` one, and cosine
        similarity between them is meaningless -- they are different vector spaces.
        A width check alone would pass, nothing would error, and retrieval would
        return confident nonsense. That is strictly worse than failing, so the
        *model* is recorded and compared, not just its size.

        The identity lives in a sentinel point with a fixed id. Qdrant has no
        collection metadata, and a point costs one vector; the alternative -- encoding
        the model in the collection name -- silently orphans the old collection every
        time somebody experiments.

        The collection is *derived*: the markdown under knowledge/ is the source of
        truth and ``vanna knowledge reindex`` rebuilds it, so recreating loses
        nothing. It is still opt-in, because this code cannot tell a deliberate
        provider change from a mistyped variable.
        """
        wanted = int(getattr(self.embedder, "dimension", 0) or 0)
        if not wanted:
            return  # an unprobed model; nothing to compare against yet

        identity = f"{self.embedder.model_name}:{wanted}"

        try:
            info = client.get_collection(self.collection)
            params = info.config.params.vectors
            # Qdrant reports either a single unnamed vector config or a mapping of
            # named ones. Only the unnamed form is ever created here.
            actual = int(getattr(params, "size", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read collection geometry: %s", exc)
            return

        stored = self._read_identity(client)

        # An unlabelled collection predates this check. Its vectors came from the
        # local model that was the default then, which is the only thing that could
        # have written them.
        if stored is None and actual:
            stored = f"{_PRE_SENTINEL_MODEL}:{actual}"

        if actual == wanted and stored == identity:
            return  # same model, same width: nothing to do

        reason = (
            f"width {actual} != {wanted}"
            if actual != wanted
            else f"written by {stored!r}, now configured as {identity!r}"
        )

        if _RECREATE_ON_MISMATCH:
            logger.warning(
                "Rebuilding collection %s (%s). VANNA_QDRANT_RECREATE_ON_MISMATCH "
                "is set. Knowledge is re-indexed from markdown on the next sync.",
                self.collection, reason,
            )
            from qdrant_client.models import Distance, VectorParams

            client.delete_collection(self.collection)
            client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=wanted, distance=Distance.COSINE),
            )
            self._write_identity(client, identity, wanted)
            return

        if actual == wanted:
            # Same width, different model. Spelled out separately because "it is the
            # right size" is exactly why somebody would expect this to work.
            raise VectorWidthMismatch(
                "\n".join([
                    f"Qdrant collection {self.collection!r} was written by "
                    f"{stored!r}, but the configured embedder is {identity!r}.",
                    "",
                    "They produce the same-width vectors, and that is not enough: "
                    "different models embed into different spaces, so comparing "
                    "their vectors returns plausible-looking nonsense rather than "
                    "an error.",
                    "",
                    "Rebuild the collection (it is derived from knowledge/):",
                    "  docker compose stop qdrant && docker volume rm vanna_qdrant-data",
                    "  docker compose up -d qdrant && vanna knowledge reindex",
                    "",
                    "Or set VANNA_QDRANT_RECREATE_ON_MISMATCH=true to do that "
                    "automatically on the next start.",
                ])
            )

        # Built as lines rather than one string with escapes: this message is the
        # entire remediation an operator gets, and it has to survive being read in a
        # log, a terminal and a ticket.
        raise VectorWidthMismatch(
            "\n".join([
                f"Qdrant collection {self.collection!r} holds {actual}-dimension "
                f"vectors, but the configured embedder "
                f"({self.embedder.model_name}) produces {wanted}. A collection "
                f"cannot hold both.",
                "",
                "The collection is derived from the markdown under knowledge/, so "
                "rebuilding it loses nothing:",
                "  docker compose stop qdrant && docker volume rm vanna_qdrant-data",
                "  docker compose up -d qdrant && vanna knowledge reindex",
                "",
                "Alternatives:",
                "  VANNA_QDRANT_RECREATE_ON_MISMATCH=true  do that automatically",
                f"  VANNA_EMBED_DIMENSIONS={actual}{' ' * max(1, 16 - len(str(actual)))}"
                f"keep the existing collection",
                "     (OpenAI models can be asked for a shorter vector, and a "
                "truncated one is still usable)",
            ])
        )

    # -- embedder identity ---------------------------------------------
    #
    # Qdrant has no place to hang collection metadata, so it goes in a point with a
    # fixed id. It carries a zero vector and a payload nothing else matches, and the
    # search filter always requires a tenant_id, so it can never appear in a result.

    def _read_identity(self, client) -> Optional[str]:
        """Which embedder wrote this collection, or None if it is unlabelled."""
        try:
            points = client.retrieve(
                collection_name=self.collection,
                ids=[_IDENTITY_POINT],
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read the collection identity: %s", exc)
            return None
        if not points:
            return None
        return str((points[0].payload or {}).get("embedder") or "") or None

    def _write_identity(self, client, identity: str, dimension: int) -> None:
        """Record which embedder wrote this collection."""
        from qdrant_client.models import PointStruct

        try:
            client.upsert(
                collection_name=self.collection,
                points=[
                    PointStruct(
                        id=_IDENTITY_POINT,
                        vector=[0.0] * dimension,
                        payload={"embedder": identity, "kind": _IDENTITY_KIND},
                    )
                ],
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001 - a label is not worth an outage
            logger.debug("Could not record the collection identity: %s", exc)

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
