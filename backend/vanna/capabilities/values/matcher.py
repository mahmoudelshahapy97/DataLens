"""Resolve a term the user typed to a value the column actually stores.

Five tiers, tried in order, stopping at the first that produces anything:

    exact -> normalized -> synonym -> fuzzy -> semantic

Tiers one to four are pure string work over an already-loaded dictionary: no
I/O, no model, no cost. Only the fifth needs an embedding, which is why it is
optional and why the caller passes its resolver in rather than this module
reaching for one. A deployment with no embedding provider simply has four tiers,
which is a supported state rather than a degraded one.

**This module decides nothing.** It returns candidates. The caller puts them in
front of the model, which can see the question, the column description and the
rest of the schema, and chooses. Resolution that silently rewrote a user's filter
would be a worse bug than the one it fixes -- "show me laptops" quietly becoming
``category = 'LAPTOP-PRO'`` is an answer to a question nobody asked.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Awaitable, Callable, Iterable, List, Optional, Sequence, Tuple

from .models import ColumnValues, MatchTier, ValueMatch

#: Confidence per tier. Fixed rather than computed, except for fuzzy and
#: semantic where the underlying score *is* the confidence. The gaps are
#: deliberate: a normalized match must always outrank the best possible fuzzy
#: one, or "laptops" -> "LAPTOP" could lose to a typo-similar neighbour.
TIER_CONFIDENCE = {
    MatchTier.EXACT: 1.0,
    MatchTier.NORMALIZED: 0.98,
    MatchTier.SYNONYM: 0.95,
}

#: Below this, a fuzzy or semantic candidate is noise. SequenceMatcher is
#: generous with short strings -- "id" and "idx" score 0.8 -- so this sits high
#: enough that a three-letter code cannot match an unrelated three-letter code.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

#: Fuzzy is capped below the synonym tier, so a lucky character overlap can
#: never outrank something a person actually declared.
_FUZZY_CEILING = 0.94

#: A question is not a value. Terms longer than this are sentences the extractor
#: mis-split, and matching them against a category code wastes work.
_MAX_TERM_CHARS = 80

#: Tokens, keeping internal hyphens so `SKU-123` stays one term. A plain
#: `[A-Za-z0-9_]+` splits it into `SKU` and `123`, which makes the code-shaped
#: branch below unreachable -- neither half is code-shaped on its own, and
#: neither matches a stored `SKU-123`.
_WORD = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")

#: Words that are grammar in every question. Small on purpose: this exists to
#: stop the obvious noise, not to be a linguistic model. A word that slips
#: through costs one dictionary scan, which is free.
_STOPWORDS = frozenset(
    """
a an and any are as at be been but by can could did do does for from
get give had has have how i if in into is it its just like made make many
me most much my no not of on one only or our out over please show so some
than that the their them then there these they this those to top total
us was we were what when where which who why will with would you your
count sum avg average min max list find get number rows table data
    """.split()  # noqa: SIM905 - a readable block beats 100 quoted literals
)

#: Below this a lowercase word is too short to be a value worth testing.
_MIN_WORD_CHARS = 3

#: Straight and curly quote pairs. The curly ones are deliberate rather than a
#: paste artefact: they arrive constantly from anyone typing in a word processor
#: or on a phone, and a question quoting 'Rugged Laptop' with smart quotes has to
#: match the same way one with straight quotes does.
_QUOTES = "'\"‘’“”"
_QUOTED = re.compile(rf"[{_QUOTES}]([^{_QUOTES}]{{1,80}})[{_QUOTES}]")
_CODE_LIKE = re.compile(
    r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-_]{1,}$|^[A-Za-z]+[-_][A-Za-z0-9\-_]+$"
)

#: Given a term and a column, return that column's values ordered by semantic
#: closeness with a score in [0, 1]. Injected rather than imported so this module
#: stays free of the index layer.
SemanticResolver = Callable[
    [str, ColumnValues], Awaitable[Sequence[Tuple[str, float]]]
]


def extract_candidate_terms(question: str) -> List[str]:
    """Pull the parts of a question that could name a stored value.

    Deliberately narrow. A false positive costs one dictionary scan, which is
    free; a term list containing every word would put most of the question into
    the prompt's value hints and drown the signal they exist to add.
    """
    terms: List[str] = []
    seen = set()

    def add(candidate: str) -> None:
        text = candidate.strip()
        if not text or len(text) > _MAX_TERM_CHARS:
            return
        folded = text.casefold()
        if folded in seen:
            return
        seen.add(folded)
        terms.append(text)

    for match in _QUOTED.finditer(question or ""):
        add(match.group(1))

    words = _WORD.findall(question or "")
    for index, word in enumerate(words):
        if len(word) < 2:
            continue
        if (word.isupper() and word.isalpha()) or _CODE_LIKE.match(word):
            add(word)
        elif word[:1].isupper() and index > 0:
            # Sentence-initial capitals are grammar, not emphasis -- which is
            # why index 0 is skipped rather than the word being lowercased.
            add(word)
        elif (
            len(word) >= _MIN_WORD_CHARS
            and word.isalpha()
            and word.casefold() not in _STOPWORDS
        ):
            # Ordinary lowercase content words. Without these the headline case
            # -- "how many laptop sales" reaching `category = 'LAPTOP'` -- is
            # missed entirely, because nobody capitalises or quotes the value
            # they are asking about. The cost is bounded and small: a term that
            # matches nothing costs one pass over a dictionary already in
            # memory, and `resolve_question` caps how many are tried.
            add(word)
    return terms


def match_value(
    term: str,
    column: ColumnValues,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[ValueMatch]:
    """Tiers one to four. Pure, synchronous, and the whole matcher when no
    embedding provider is configured."""
    if column.is_empty:
        return []
    folded = (term or "").strip().casefold()
    if not folded:
        return []

    for tier in (_exact, _normalized, _synonym):
        matches = tier(term, folded, column)
        if matches:
            return _ranked(matches)

    return _ranked(_fuzzy(term, folded, column, confidence_threshold))


async def match_value_semantic(
    term: str,
    column: ColumnValues,
    resolver: SemanticResolver,
    *,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> List[ValueMatch]:
    """All five tiers.

    The semantic pass runs only when the cheap ones found nothing, so an exact
    hit never pays for an embedding.
    """
    matches = match_value(term, column, confidence_threshold=confidence_threshold)
    if matches:
        return matches

    try:
        scored = await resolver(term, column)
    except Exception:
        # An embedding backend that is down costs this tier, not the request.
        return []

    return _ranked(
        ValueMatch(
            term=term,
            value=value,
            tier=MatchTier.SEMANTIC,
            confidence=round(score, 3),
            column=column.qualified_name,
        )
        for value, score in scored
        if score >= confidence_threshold
    )


async def resolve_question(
    question: str,
    columns: Iterable[ColumnValues],
    *,
    resolver: Optional[SemanticResolver] = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    max_terms: int = 8,
    max_matches: int = 20,
) -> List[ValueMatch]:
    """Every value hint a question earns, across every column offered.

    Bounded twice over, because both bounds protect the prompt rather than the
    clock: a question mentioning fifteen capitalised words should not contribute
    fifteen sets of hints, and a term matching forty values in one column has
    found a column that is not enum-like and should not be hinted at all.
    """
    columns = list(columns)
    if not columns:
        return []

    results: List[ValueMatch] = []
    for term in extract_candidate_terms(question)[:max_terms]:
        for column in columns:
            if resolver is not None:
                matches = await match_value_semantic(
                    term, column, resolver, confidence_threshold=confidence_threshold
                )
            else:
                matches = match_value(
                    term, column, confidence_threshold=confidence_threshold
                )
            for match in matches:
                results.append(
                    match
                    if match.column
                    else match.model_copy(update={"column": column.qualified_name})
                )

    return _ranked(results)[:max_matches]


def describe_matches(matches: Sequence[ValueMatch]) -> str:
    """A prompt-ready rendering, grouped by column.

    States the term and the stored spelling together, because the model has to
    understand this as "the value you want is written like this" rather than as
    a list of strings to choose from.
    """
    if not matches:
        return ""

    by_column: dict = {}
    for match in matches:
        by_column.setdefault(match.column or "", []).append(match)

    lines = []
    for column, found in sorted(by_column.items()):
        pairs = ", ".join(
            # Compared exactly, not casefolded. Case *is* the difference in the
            # commonest instance of this problem -- a question saying "laptop"
            # against a column storing "LAPTOP" -- and folding it away would
            # hide the one thing worth telling the model.
            f"{m.term!r} is stored as {m.value!r}" if m.term != m.value
            else f"{m.value!r}"
            for m in found
        )
        lines.append(f"- {column}: {pairs}")
    return (
        "Values in this question that exist in the data, with their exact "
        "spelling. Filter on the stored spelling, not on what the user typed:\n"
        + "\n".join(lines)
    )


# ----------------------------------------------------------------------
# Tiers
# ----------------------------------------------------------------------


def _exact(term: str, folded: str, column: ColumnValues) -> List[ValueMatch]:
    return [
        _match(term, value, MatchTier.EXACT, column)
        for value in column.values
        if value == term
    ]


def _normalized(term: str, folded: str, column: ColumnValues) -> List[ValueMatch]:
    """Case, and a trailing plural. The two ways the same word gets written."""
    singular = _singular(folded)
    matches = []
    for value in column.values:
        candidate = value.casefold()
        if folded in (candidate, _singular(candidate)) or singular == candidate:
            matches.append(_match(term, value, MatchTier.NORMALIZED, column))
    return matches


def _synonym(term: str, folded: str, column: ColumnValues) -> List[ValueMatch]:
    """Tenant-authored equivalence: declared synonyms, and the label of a code."""
    matches: List[ValueMatch] = []
    known = {value.casefold(): value for value in column.values}

    for canonical, terms in column.synonyms.items():
        if folded != canonical.casefold() and not any(
            folded == t.casefold() for t in terms
        ):
            continue
        value = known.get(canonical.casefold())
        if value is not None:
            matches.append(_match(term, value, MatchTier.SYNONYM, column))

    # "cancelled orders" should reach the code `C` when someone wrote
    # {"C": "Cancelled"}. The label is the human-facing half of the pair, so it
    # is the half a question will use.
    for code, label in column.labels.items():
        if label.casefold() != folded:
            continue
        value = known.get(code.casefold())
        if value is not None and not any(m.value == value for m in matches):
            matches.append(_match(term, value, MatchTier.SYNONYM, column))
    return matches


def _fuzzy(
    term: str, folded: str, column: ColumnValues, threshold: float
) -> List[ValueMatch]:
    """Character similarity, for typos and near-spellings."""
    matches = []
    for value in column.values:
        ratio = SequenceMatcher(None, folded, value.casefold()).ratio()
        if ratio >= threshold:
            matches.append(
                ValueMatch(
                    term=term,
                    value=value,
                    tier=MatchTier.FUZZY,
                    confidence=round(min(ratio, _FUZZY_CEILING), 3),
                    column=column.qualified_name,
                )
            )
    return matches


def _match(
    term: str, value: str, tier: MatchTier, column: ColumnValues
) -> ValueMatch:
    return ValueMatch(
        term=term,
        value=value,
        tier=tier,
        confidence=TIER_CONFIDENCE[tier],
        column=column.qualified_name,
    )


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def _ranked(matches: Iterable[ValueMatch]) -> List[ValueMatch]:
    # Value as the tiebreaker rather than input order, so the same question
    # against the same dictionary always produces the same prompt. Context that
    # varies run to run makes a failure impossible to reproduce.
    return sorted(matches, key=lambda m: (-m.confidence, m.column or "", m.value))
