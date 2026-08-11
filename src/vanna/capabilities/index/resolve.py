"""Choosing an index backend, and saying so when the choice was not honoured.

The one design note: a downgrade is **logged loudly and reported**, never
silent. A deployment that asked for a vector backend and got lexical because a
dependency was missing looks, from the outside, exactly like a deployment whose
answers quietly got worse after a release -- and that is an unclosable support
ticket. The effective backend is surfaced on ``/schema`` for the same reason.
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import SearchIndex
from .lexical import LexicalIndex

logger = logging.getLogger(__name__)


def resolve_index(spec: Optional[str] = None) -> SearchIndex:
    """Build the requested index, falling back with a warning.

    Args:
        spec: ``lexical`` (default), or the name of a vector integration to
            fuse with lexical -- ``chromadb``, ``qdrant``, ``faiss``.
    """
    wanted = (spec or "lexical").strip().lower()

    if wanted in ("", "lexical", "bm25", "none"):
        return LexicalIndex()

    vector = _try_vector_index(wanted)
    if vector is None:
        logger.warning(
            "Index backend %r is unavailable; using lexical instead. Retrieval "
            "will be keyword-only. Install the extra and restart to enable it.",
            wanted,
        )
        return LexicalIndex()

    from .hybrid import HybridIndex

    return HybridIndex([LexicalIndex(), vector])


def _try_vector_index(name: str) -> Optional[SearchIndex]:
    """Build a vector-backed index, or None if it cannot be built.

    Vector backends are adapters over the same clients the ``AgentMemory``
    integrations use, but in their **own collection**. Sharing one would mean a
    memory purge silently wiping the example index -- two things with different
    lifecycles and different owners.
    """
    try:
        module = __import__(
            f"vanna.integrations.{name}.search_index", fromlist=["build_index"]
        )
    except ImportError as exc:
        logger.debug("No search_index adapter for %s: %s", name, exc)
        return None

    try:
        return module.build_index()
    except Exception as exc:
        logger.warning("Could not initialise the %s index: %s", name, exc)
        return None
