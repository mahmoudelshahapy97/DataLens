"""BM25 over an in-memory inverted index. The default, and dependency-free.

Two things make this meaningfully better than the Jaccard overlap it replaces.

**IDF.** A term appearing in one document out of four hundred says far more
about relevance than one appearing in all of them. Jaccard weighs both equally,
so a question mentioning "customer" matches every table that has a customer
column. BM25 does not.

**SQL-aware tokenization.** ``customer_id`` is indexed as ``customer_id``,
``customer`` and ``id``; ``orderDate`` as ``orderdate``, ``order`` and ``date``.
Identifiers are compound words, and matching only the whole thing means a
question about "orders" misses ``order_items`` entirely.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set

from .base import IndexDocument, IndexHit, SearchIndex

# BM25 parameters. k1 controls how fast term frequency saturates, b how much
# document length is penalised. These are the standard defaults; the corpus
# here (a few thousand short documents) is not the kind that rewards tuning.
_K1 = 1.5
_B = 0.75

#: Underscores are part of a word here, deliberately. Splitting on them first
#: would mean `customer_id` never exists as a token, so an exact match on it
#: could never outrank a table that merely has a `customer` column.
_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: SQL keywords carry no information about *which* table a question is about --
#: every stored query has SELECT and FROM in it.
_STOPWORDS = frozenset(
    """
    select from where group by order having join left right inner outer on as
    and or not in is null distinct limit offset union all with case when then
    else end count sum avg min max cast asc desc between like exists
    a an the of to for it its this that what which how many much show me list
    """.split()
)


def tokenize(text: str) -> List[str]:
    """Words, plus the parts of any compound identifier.

    Both forms are kept: a query for ``customer_id`` should rank an exact match
    above a table that merely has a ``customer`` column, and dropping the joined
    form would lose that distinction.
    """
    tokens: List[str] = []
    for word in _WORD.findall(text or ""):
        split = _CAMEL.sub(" ", word).lower()
        parts = [p for p in split.split() if p]

        whole = word.lower()
        if whole not in _STOPWORDS and len(whole) > 1:
            tokens.append(whole)

        if len(parts) > 1:
            tokens.extend(p for p in parts if p not in _STOPWORDS and len(p) > 1)

        for part in whole.split("_"):
            if part and part != whole and part not in _STOPWORDS and len(part) > 1:
                tokens.append(part)
    return tokens


class LexicalIndex(SearchIndex):
    """BM25 ranking over an inverted index held in memory.

    Rebuilt from the stores rather than persisted: the corpus is small, the
    build is milliseconds, and a persisted index is one more thing that can
    silently disagree with its source.
    """

    name = "lexical"

    def __init__(self) -> None:
        self._documents: Dict[str, IndexDocument] = {}
        self._tokens: Dict[str, List[str]] = {}
        self._postings: Dict[str, Set[str]] = defaultdict(set)
        self._length: Dict[str, int] = {}

    # -- writes --------------------------------------------------------

    def add(self, documents: Iterable[IndexDocument]) -> None:
        for document in documents:
            if document.id in self._documents:
                self._unindex(document.id)

            tokens = tokenize(document.text)
            self._documents[document.id] = document
            self._tokens[document.id] = tokens
            self._length[document.id] = len(tokens) or 1
            for token in set(tokens):
                self._postings[token].add(document.id)

    def _unindex(self, document_id: str) -> None:
        for token in set(self._tokens.get(document_id, ())):
            postings = self._postings.get(token)
            if postings is not None:
                postings.discard(document_id)
                if not postings:
                    del self._postings[token]
        self._documents.pop(document_id, None)
        self._tokens.pop(document_id, None)
        self._length.pop(document_id, None)

    def remove(self, ids: Iterable[str]) -> None:
        for document_id in list(ids):
            self._unindex(document_id)

    def clear(self, *, tenant_id: Optional[str] = None) -> None:
        if tenant_id is None:
            self._documents.clear()
            self._tokens.clear()
            self._postings.clear()
            self._length.clear()
            return
        self.remove(
            [d.id for d in self._documents.values() if d.tenant_id == tenant_id]
        )

    # -- reads ---------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tenant_id: str = "default",
        limit: int = 10,
        kind: Optional[str] = None,
    ) -> List[IndexHit]:
        terms = tokenize(query)
        if not terms or not self._documents:
            return []

        candidates = {
            document_id
            for term in terms
            for document_id in self._postings.get(term, ())
        }
        candidates = {
            document_id
            for document_id in candidates
            if self._documents[document_id].tenant_id == tenant_id
            and (kind is None or self._documents[document_id].kind == kind)
        }
        if not candidates:
            return []

        total = len(self._documents)
        average_length = sum(self._length.values()) / total

        scored: List[IndexHit] = []
        for document_id in candidates:
            document = self._documents[document_id]
            tokens = self._tokens[document_id]
            length = self._length[document_id]

            score = 0.0
            for term in set(terms):
                frequency = tokens.count(term)
                if not frequency:
                    continue
                # +0.5/+0.5 smoothing keeps IDF positive for a term present in
                # more than half the corpus, which would otherwise score
                # negatively and push a legitimate match below an unrelated one.
                containing = len(self._postings.get(term, ()))
                idf = math.log(1 + (total - containing + 0.5) / (containing + 0.5))
                saturation = frequency * (_K1 + 1)
                normalisation = frequency + _K1 * (1 - _B + _B * length / average_length)
                score += idf * saturation / normalisation

            if score > 0:
                scored.append(
                    IndexHit(
                        id=document_id,
                        score=score * document.boost,
                        kind=document.kind,
                        metadata=document.metadata,
                    )
                )

        # Ties broken by id so repeated searches return a stable order --
        # a result list that reshuffles between identical calls makes any
        # retrieval regression impossible to reproduce.
        scored.sort(key=lambda hit: (-hit.score, hit.id))
        return scored[:limit]

    def __len__(self) -> int:
        return len(self._documents)
