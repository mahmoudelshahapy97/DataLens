"""Value dictionaries: what a column actually stores, and how a term reaches it.

The most common cause of wrong-but-valid SQL is an invented literal --
``WHERE status = 'active'`` against a column storing ``'ACTIVE'``. It parses,
validates, passes every permission check, and returns nothing.

:func:`match_value` fixes that with five tiers, cheapest first. :class:`ValueStore`
holds the dictionary it matches against, curated rather than merely observed:
sampling proposes, a person approves, and nothing untouched by a human reaches a
prompt.
"""

from .base import ValueStore, assemble_dictionary
from .matcher import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    TIER_CONFIDENCE,
    SemanticResolver,
    describe_matches,
    extract_candidate_terms,
    match_value,
    match_value_semantic,
    resolve_question,
)
from .models import (
    ColumnValues,
    MatchTier,
    ReviewStatus,
    SampledValue,
    ValueMatch,
    ValueSynonym,
)
from .sampling import (
    MAX_DICTIONARY_VALUES,
    MAX_VALUE_CHARS,
    is_sampleable,
    sample_column,
    sampleable_columns,
    samples_from_catalog,
)

__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "MAX_DICTIONARY_VALUES",
    "MAX_VALUE_CHARS",
    "TIER_CONFIDENCE",
    "ColumnValues",
    "MatchTier",
    "ReviewStatus",
    "SampledValue",
    "SemanticResolver",
    "ValueMatch",
    "ValueStore",
    "ValueSynonym",
    "assemble_dictionary",
    "describe_matches",
    "extract_candidate_terms",
    "is_sampleable",
    "match_value",
    "match_value_semantic",
    "resolve_question",
    "sample_column",
    "sampleable_columns",
    "samples_from_catalog",
]
