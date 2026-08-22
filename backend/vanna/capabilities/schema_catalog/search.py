"""Lexical ranking of tables against a question.

Lives here rather than beside a storage backend because it is catalog logic, not
a storage concern: every :class:`~vanna.capabilities.schema_catalog.SchemaCatalog`
implementation needs the same fallback when no vector index is configured, and
two copies of a relevance function drift into two different answers to the same
question.

Used when :meth:`SchemaCatalog.get_context` exceeds its character budget and has
to choose which tables to send. Relevance quality matters most exactly there --
on a large schema, where being wrong means the model never sees the table it
needed.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, List, Optional, Set

if TYPE_CHECKING:  # pragma: no cover
    from .models import TableMetadata

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    """Crude singular form, for matching only.

    Table names are plural ("customers") and questions are usually singular
    ("customer orders"), so exact term matching misses the most obvious pairing
    there is. A real stemmer would be better but would mean a new dependency
    for a job that three suffix rules handle: this is used purely to compare
    two words, never to display anything, so being linguistically wrong about
    an edge case costs nothing as long as it is wrong *consistently* on both
    sides of the comparison.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"      # categories -> category
    if len(word) > 4 and word.endswith("ses"):
        return word[:-2]            # addresses -> address
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]            # customers -> customer
    return word


def terms(text: str) -> Set[str]:
    """Split text into lowercase stems, also breaking snake_case and camelCase.

    ``customer_order_id`` and ``customerOrderId`` both yield
    ``{customer, order, id}``, so a question mentioning "orders" matches a
    column named ``customerOrderId``. Without the split, identifier-style names
    -- which is most of them -- would rarely match natural language at all.

    Both the original word and its stem are kept, so an exact match still
    scores and a singular/plural match also scores.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    words = _WORD_RE.findall(spaced.lower())
    found: Set[str] = set()
    for word in words:
        found.add(word)
        found.add(stem(word))
    return found


def rank_tables(
    query: str, tables: List["TableMetadata"], limit: int
) -> List["TableMetadata"]:
    """Return the *limit* tables most relevant to *query*, most relevant first.

    Never returns nothing for a non-empty catalog. An empty schema section
    guarantees a hallucinated table name, so a weak guess the model can reject
    beats no information at all.
    """
    if not tables:
        return []

    query_terms = terms(query)
    if not query_terms:
        return tables[:limit]

    scored = []
    for table in tables:
        # Table name and description weigh more than column names: a question
        # about "orders" should surface the orders table ahead of a table that
        # merely has an order_id column.
        name_terms = terms(table.qualified_name)
        desc_terms = terms(table.description or "")
        col_terms: Set[str] = set()
        for column in table.columns:
            col_terms |= terms(column.name)
            col_terms |= terms(column.description or "")

        score = (
            3.0 * len(query_terms & name_terms)
            + 2.0 * len(query_terms & desc_terms)
            + 1.0 * len(query_terms & col_terms)
        )
        if score > 0:
            scored.append((score, table))

    scored.sort(key=lambda pair: (-pair[0], pair[1].qualified_name))
    results = [t for _, t in scored[:limit]]
    return results or tables[:limit]


def search_indexed(
    index: Any,
    context: Any,
    query: str,
    tables: List["TableMetadata"],
    limit: int,
) -> Optional[List["TableMetadata"]]:
    """Rank through a search index, or None to fall back to :func:`rank_tables`.

    Storage-agnostic on purpose: it needs the tables and the index, not the place
    they came from, so a JSON-backed and a Postgres-backed catalog rank a question
    identically. Returning None rather than raising is what makes the fallback a
    degradation instead of an outage.

    The document set is rebuilt per call from the tables handed in. A scan replaces
    the catalog wholesale, so an index kept alongside it would need invalidating on
    every write -- and the one invalidation that gets missed serves stale table
    names, which is worse than the microseconds this costs.
    """
    try:
        from vanna.capabilities.agent_memory import tenant_scope
        from vanna.capabilities.index import documents_for_tables, sync_documents

        tenant = tenant_scope(context)
        # The index decides whether that means a rebuild (cheap, in-process BM25)
        # or a hash diff that re-embeds only what changed (a persistent vector
        # store). A hard-coded clear-and-add here would re-embed every table on
        # every search.
        sync_documents(
            index,
            documents_for_tables(tables, tenant_id=tenant),
            tenant_id=tenant,
            # Exactly what documents_for_tables emits. Not None: that would claim
            # the whole tenant and sweep away the knowledge store's examples on the
            # next schema search.
            kinds=("table", "column_values"),
        )

        by_name = {t.qualified_name: t for t in tables}
        ordered: List["TableMetadata"] = []
        seen = set()
        # One table can match twice -- once on structure, once on its values.
        # Keep the better rank and drop the duplicate.
        for hit in index.search(query, tenant_id=tenant, limit=limit * 2):
            name = hit.metadata.get("qualified")
            table = by_name.get(name)
            if table is not None and name not in seen:
                seen.add(name)
                ordered.append(table)

        # Same reasoning as the fallback path: never return nothing on a
        # non-empty catalog.
        return (ordered or tables[:limit])[:limit]
    except Exception as exc:
        logger.warning("Index search failed, falling back to term overlap: %s", exc)
        return None
