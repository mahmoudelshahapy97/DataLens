"""Linking curated knowledge to the tables a question involves.

The catalog knows structure. Meaning lives elsewhere -- a glossary term an
admin wrote ("churn"), a metric in a semantic cube ("revenue"), a column an
admin marked core -- and none of it reaches retrieval unless something connects
it to tables. This module is that connection, kept deliberately small:

* :class:`SchemaKnowledge` -- the hook ``RetrievalContextEnhancer`` calls.
  ``table_hints`` says which tables a question's vocabulary points at, so the
  search path includes them even when their names share no word with the
  question; ``core_columns`` says which columns of the selected tables were
  curated as the ones that matter. The application implements it over its own
  stores (``vanna_app/knowledge_links.py``); the library only defines the shape.
* :func:`match_phrases` -- which curated phrases a question uses.
* :func:`tables_in_sql` -- which tables a verified example touches, so examples
  about the tables in play can be preferred.

Matching is lexical on purpose. A term is curated vocabulary, so the question
either uses it or does not; fuzzy matching would turn "rate" into a hit for
"rating" and spend the prompt on the wrong definitions.
"""

from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    Dict,
    Iterable,
    List,
    Mapping,
    Sequence,
    Set,
    TypeVar,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.capabilities.schema_catalog.models import TableMetadata
    from vanna.core.tool import ToolContext

T = TypeVar("T")

_WORD = re.compile(r"[a-z0-9]+")

#: Words too common to make a phrase match on their own.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
        "in", "is", "it", "of", "on", "or", "per", "the", "to", "vs", "was",
        "what", "which", "who", "with",
    }
)


class SchemaKnowledge:
    """Curated knowledge retrieval can consult. Both methods are optional.

    Implementations must scope to the caller's tenant -- the context carries
    it -- and must never fail retrieval: return empty on any error.
    """

    async def table_hints(self, context: "ToolContext", question: str) -> List[str]:
        """Tables the question's vocabulary is linked to, most specific first."""
        return []

    async def core_columns(
        self, context: "ToolContext", tables: Sequence["TableMetadata"]
    ) -> Dict[str, List[str]]:
        """Curated core columns per qualified table name."""
        return {}


def words(text: str) -> List[str]:
    """Lower-cased word tokens, ``snake_case`` split, plurals folded.

    Folding is the crude kind -- strip one trailing ``s`` from words longer
    than three letters -- which is enough for "invoices" to meet "invoice" and
    does no damage to the identifiers and business terms it sees.
    """
    tokens = _WORD.findall((text or "").lower().replace("_", " "))
    return [_fold(t) for t in tokens]


def _fold(token: str) -> str:
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def phrase_in(question_words: Set[str], phrase: str) -> bool:
    """Whether every meaningful word of *phrase* occurs in the question."""
    meaningful = [w for w in words(phrase) if w not in _STOPWORDS]
    return bool(meaningful) and all(w in question_words for w in meaningful)


def match_phrases(question: str, phrases: Mapping[str, T]) -> Dict[str, T]:
    """The entries of *phrases* whose key the question uses."""
    present = set(words(question))
    return {p: v for p, v in phrases.items() if phrase_in(present, p)}


def tables_in_sql(sql: str) -> Set[str]:
    """Tables a statement reads, lower-cased, both bare and qualified.

    sqlglot when it parses; a FROM/JOIN regex when it does not, because a
    verified example in an exotic dialect is still worth ranking. CTE names
    are excluded -- they are not tables anyone can join to.
    """
    found: Set[str] = set()
    try:
        import sqlglot
        from sqlglot import exp

        ctes: Set[str] = set()
        for tree in sqlglot.parse(sql or ""):
            if tree is None:
                continue
            ctes.update(c.alias_or_name.lower() for c in tree.find_all(exp.CTE))
            for table in tree.find_all(exp.Table):
                name = (table.name or "").lower()
                if not name or name in ctes:
                    continue
                found.add(name)
                if table.db:
                    found.add(f"{table.db.lower()}.{name}")
        return found
    except Exception:
        pass
    for match in re.finditer(r"\b(?:from|join)\s+([\w.\"`\[\]]+)", sql or "", re.I):
        name = re.sub(r"[\"`\[\]]", "", match.group(1)).lower()
        found.add(name)
        found.add(name.split(".")[-1])
    return found


def overlap(example_tables: Iterable[str], selected: Iterable[str]) -> int:
    """How many selected tables an example touches, by bare or qualified name."""
    seen = set(example_tables)
    return sum(
        1
        for name in {s.lower() for s in selected}
        if name in seen or name.split(".")[-1] in seen
    )
