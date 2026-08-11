"""Ranked retrieval for the knowledge and catalog stores.

    from vanna.capabilities.index import LexicalIndex, documents_for_tables

    index = LexicalIndex()
    index.add(documents_for_tables(tables, tenant_id="acme"))
    index.search("cancelled orders", tenant_id="acme")

The default backend is dependency-free BM25. A vector backend can be fused in
with :func:`resolve_index`, which reports -- rather than hides -- a fallback.
"""

from .base import IndexDocument, IndexHit, SearchIndex
from .hybrid import HybridIndex
from .indexer import (
    MAX_VALUES_PER_COLUMN,
    documents_for_examples,
    documents_for_table,
    documents_for_tables,
)
from .lexical import LexicalIndex, tokenize
from .resolve import resolve_index

__all__ = [
    "SearchIndex",
    "IndexDocument",
    "IndexHit",
    "LexicalIndex",
    "HybridIndex",
    "tokenize",
    "resolve_index",
    "documents_for_table",
    "documents_for_tables",
    "documents_for_examples",
    "MAX_VALUES_PER_COLUMN",
]
