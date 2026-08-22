"""Turning text into vectors.

Retrieval that only matches words cannot tell that "sales per area" and "revenue by
region" are the same question. Embeddings can, and this is where they come from.

One provider: OpenAI's ``text-embedding-3-*`` over the API. Nothing resident in the
process, nothing to download at boot, better retrieval quality than a small local
model -- and, in exchange, an API key, a network round trip and a per-token charge on
every indexing pass and every query. A deployment that cannot make that call should
leave ``VANNA_INDEX_BACKEND`` at ``lexical`` and get keyword-only BM25, which needs
no credentials and no network at all.

``VANNA_EMBED_PROVIDER`` remains a setting because :class:`Embedder` is a protocol: a
deployment can supply its own implementation without inheriting from anything here.
``openai`` is the only value this module builds.

**Switching models changes the vector width**, and a vector store cannot mix widths in
one collection. ``text-embedding-3-small`` is 1536, ``-3-large`` is 3072. The Qdrant
adapter detects the mismatch at startup and says exactly what to do about it rather
than failing later on an insert. ``VANNA_EMBED_DIMENSIONS`` shortens a vector -- these
models are trained so that a truncated vector is still usable -- which is the way to
keep an existing, narrower collection working.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, List, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

#: Which provider to use. ``openai`` is the only one built here.
DEFAULT_PROVIDER = "openai"

#: Default model. `-3-small` rather than `-3-large`: five times cheaper, and the gap
#: between them is smaller than the gap between either and keyword search.
DEFAULT_MODEL = "text-embedding-3-small"

#: Native widths, used to size a collection before the first call is made. An
#: unknown model is probed instead.
OPENAI_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

#: Documents per request. The API accepts far more, but a failed batch is retried
#: whole, and 128 keeps a retry cheap without making the round trips dominate.
OPENAI_BATCH = 128


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn text into vectors.

    A protocol rather than a base class so a deployment can supply its own -- an
    in-house model, or a different hosted API -- without inheriting from us.
    """

    #: Vector length. Read when a collection is created, so it must be correct
    #: before the first document is embedded.
    dimension: int

    #: Reported on /schema, so an operator can see which model is in use.
    model_name: str

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch of documents."""
        ...

    def embed_query(self, text: str) -> List[float]:
        """Embed one search query."""
        ...


class OpenAIEmbedder:
    """Embeddings from OpenAI's ``text-embedding-3-*`` models.

    Args:
        model_name: An OpenAI embedding model id.
        api_key: Falls back to ``OPENAI_API_KEY``.
        dimensions: Shorten the vector to this many values. These models are
            trained so that a prefix of the vector is still a usable embedding, so
            this trades a little accuracy for a smaller index -- and, more usefully,
            lets an existing narrower collection keep working when switching model.
            Omitted means the model's native width.
        base_url: For an Azure or proxy endpoint. Falls back to ``OPENAI_BASE_URL``.

    There is no model to load, so ``dimension`` is known from the table above without
    a network call. An unrecognised model is probed once, on first use.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        api_key: Optional[str] = None,
        dimensions: Optional[int] = None,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.model_name = model_name
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = base_url or os.getenv("OPENAI_BASE_URL") or None
        self._timeout = timeout
        self._max_retries = max_retries
        self._requested_dimensions = dimensions
        self._client: Any = None
        self._lock = threading.Lock()

        if not self._api_key:
            raise ValueError(
                "OpenAI embeddings need an API key. Set OPENAI_API_KEY, or leave "
                "VANNA_INDEX_BACKEND=lexical for keyword-only retrieval, which "
                "needs no credentials."
            )

        native = OPENAI_DIMENSIONS.get(model_name)
        if dimensions is not None:
            if native is not None and dimensions > native:
                raise ValueError(
                    f"{model_name} produces {native} dimensions; "
                    f"VANNA_EMBED_DIMENSIONS={dimensions} asks for more than exists."
                )
            self.dimension = dimensions
        elif native is not None:
            self.dimension = native
        else:
            # An unknown model. Probe on first use rather than guessing, and hold a
            # placeholder that nothing sizes a collection from until then.
            self.dimension = 0

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - depends on the extra
                raise ImportError(
                    "OpenAI embeddings need the openai package. "
                    "Install with: pip install openai"
                ) from exc

            kwargs: dict = {"api_key": self._api_key, "timeout": self._timeout}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            # The client retries connection errors and 429s itself, which is the
            # behaviour wanted here -- indexing a large catalog will hit a rate
            # limit and should slow down rather than fail.
            kwargs["max_retries"] = self._max_retries
            self._client = OpenAI(**kwargs)
            logger.info(
                "Embedding model ready: %s (%s dimensions, OpenAI API)",
                self.model_name,
                self.dimension or "probing",
            )
            return self._client

    def _call(self, batch: List[str]) -> List[List[float]]:
        client = self._get_client()
        kwargs: dict = {"model": self.model_name, "input": batch}
        if self._requested_dimensions is not None:
            kwargs["dimensions"] = self._requested_dimensions

        response = client.embeddings.create(**kwargs)
        # The API documents that results come back in input order, but it also
        # returns an index on each -- sorting by it costs nothing and removes the
        # need to trust that.
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(map(float, item.embedding)) for item in ordered]

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch, in chunks.

        Empty strings are replaced rather than sent: the API rejects them, and one
        empty description in a scanned catalog would otherwise fail the whole
        indexing pass for every other document in the batch.
        """
        items = [(t if isinstance(t, str) else str(t)).strip() or " " for t in texts]
        if not items:
            return []

        vectors: List[List[float]] = []
        for start in range(0, len(items), OPENAI_BATCH):
            vectors.extend(self._call(items[start : start + OPENAI_BATCH]))

        if self.dimension == 0 and vectors:
            # An unrecognised model, now measured.
            self.dimension = len(vectors[0])
            logger.info(
                "Embedding model %s reports %d dimensions",
                self.model_name,
                self.dimension,
            )
        return vectors

    def embed_query(self, text: str) -> List[float]:
        vectors = self.embed([text])
        return vectors[0] if vectors else []

    def warm_up(self) -> None:
        """Build the client and settle the dimension.

        One embedding of one word. It costs a fraction of a cent and turns "the API
        key is wrong" into a startup error rather than a failed search an hour
        later.
        """
        self.embed(["dimension probe"])

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<OpenAIEmbedder {self.model_name} ({self.dimension} dims)>"


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------


def build_embedder(
    model_name: Optional[str] = None,
    *,
    provider: Optional[str] = None,
) -> Optional[Embedder]:
    """Build the configured embedder, or None if it cannot be built.

    Returns None rather than raising so a caller can fall back to keyword search: a
    missing optional dependency or an absent API key should cost ranking quality,
    not answers. The warning says which, because a silent downgrade to keyword
    search is an unclosable "retrieval got worse" ticket.
    """
    provider = (provider or os.getenv("VANNA_EMBED_PROVIDER", DEFAULT_PROVIDER)).strip().lower()

    try:
        if provider in ("openai", "oai"):
            dimensions = _int_or_none(os.getenv("VANNA_EMBED_DIMENSIONS"))
            embedder: Embedder = OpenAIEmbedder(
                model_name or os.getenv("VANNA_EMBED_MODEL") or DEFAULT_MODEL,
                dimensions=dimensions,
            )
        else:
            logger.error(
                "Unknown VANNA_EMBED_PROVIDER %r. The only value built here is "
                "'openai'. Retrieval stays keyword-only.",
                provider,
            )
            return None

        embedder.warm_up()  # type: ignore[attr-defined]
        return embedder
    except Exception as exc:  # noqa: BLE001 - any failure means no vectors
        logger.warning(
            "Could not initialise %s embeddings (%s): %s. Retrieval stays keyword-only.",
            provider,
            type(exc).__name__,
            exc,
        )
        return None


def _int_or_none(raw: Optional[str]) -> Optional[int]:
    if not raw or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("VANNA_EMBED_DIMENSIONS=%r is not a number; ignoring it.", raw)
        return None
