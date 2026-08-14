"""Turning text into vectors, locally.

Retrieval that only matches words cannot tell that "sales per area" and "revenue
by region" are the same question. Embeddings can, and this is where they come
from.

**Computed on this machine, not through an API.** The rest of this stack runs
against local databases and is expected to work with no internet; putting a
network round trip and a per-token charge in the middle of every retrieval would
undo that. ``fastembed`` runs a small ONNX model in-process, needs no API key,
and is already declared under the ``qdrant`` extra.

The model is ``BAAI/bge-small-en-v1.5``: 384 dimensions, ~130 MB, and the same
one :mod:`vanna.legacy.azuresearch` already uses -- so this follows the
precedent in the codebase rather than introducing a second choice to maintain.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import List, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)

#: Default model. Small enough to load quickly and to run on a CPU, good enough
#: that the difference from a large model is not what limits retrieval here.
DEFAULT_MODEL = os.getenv("VANNA_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

#: Dimensions for the default model. Only used to fail early with a clear
#: message; the real value is read from the model once it is loaded.
DEFAULT_DIMENSION = 384


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn text into vectors.

    A protocol rather than a base class so a deployment can supply its own --
    an in-house model, or a hosted API where that is genuinely wanted -- without
    inheriting from us.
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


class FastEmbedEmbedder:
    """Local ONNX embeddings via ``fastembed``.

    Args:
        model_name: A fastembed model id.
        cache_dir: Where the model weights live. Set it to a mounted volume in a
            container, or every restart re-downloads ~130 MB -- which also means
            a restart without internet fails to produce any embeddings at all.

    The model is loaded on first use, not in ``__init__``: constructing this is
    part of resolving the index at boot, and a deployment that never enables
    vectors should not spend a second and 130 MB proving it.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.model_name = model_name
        self._cache_dir = cache_dir or os.getenv("VANNA_EMBED_CACHE", "") or None
        self._model = None
        # Loading is not thread-safe and the first two requests after a restart
        # routinely arrive together.
        self._lock = threading.Lock()
        self.dimension = DEFAULT_DIMENSION

    # ------------------------------------------------------------------

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:      # another thread won the race
                return self._model
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - depends on the extra
                raise ImportError(
                    "Vector retrieval needs fastembed. "
                    "Install with: pip install 'vanna[qdrant]'"
                ) from exc

            logger.info("Loading embedding model %s (first use)", self.model_name)
            kwargs = {"model_name": self.model_name}
            if self._cache_dir:
                kwargs["cache_dir"] = self._cache_dir
            model = TextEmbedding(**kwargs)

            # Ask the model rather than trusting the constant: a different model
            # id means a different width, and a collection created at the wrong
            # width fails later with a message about nothing.
            probe = list(model.embed(["dimension probe"]))
            self.dimension = len(probe[0])
            logger.info(
                "Embedding model ready: %s (%d dimensions)",
                self.model_name,
                self.dimension,
            )
            self._model = model
            return model

    # ------------------------------------------------------------------

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch.

        Batched deliberately: fastembed amortises tokenisation and the ONNX
        session across a call, so 400 documents in one call is roughly a second
        where 400 calls is roughly a minute.
        """
        items = [t if isinstance(t, str) else str(t) for t in texts]
        if not items:
            return []
        model = self._load()
        return [list(map(float, vector)) for vector in model.embed(items)]

    def embed_query(self, text: str) -> List[float]:
        """Embed one query.

        Separate from :meth:`embed` because retrieval models are often trained
        with distinct query and document prefixes. bge-small does not require
        one for short queries, so this is currently the same call -- kept apart
        so switching to a model that does need it is a change here and nowhere
        else.
        """
        vectors = self.embed([text])
        return vectors[0] if vectors else []

    def warm_up(self) -> None:
        """Load the model now, rather than during someone's first question."""
        self._load()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        state = "loaded" if self._model is not None else "not loaded"
        return f"<FastEmbedEmbedder {self.model_name} ({state})>"


def build_embedder(model_name: Optional[str] = None) -> Optional[Embedder]:
    """Build the default embedder, or None if it cannot be built.

    Returns None rather than raising so a caller can fall back to keyword search:
    a missing optional dependency should cost ranking quality, not answers.
    """
    try:
        embedder = FastEmbedEmbedder(model_name or DEFAULT_MODEL)
        embedder.warm_up()
        return embedder
    except Exception as exc:  # noqa: BLE001 - any failure means no vectors
        logger.warning(
            "Could not initialise embeddings (%s): %s. Retrieval stays keyword-only.",
            type(exc).__name__,
            exc,
        )
        return None
