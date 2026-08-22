"""What a column actually stores, and how a term reached it.

The catalog says ``products.category`` is text. It does not say the values are
spelled ``LAPTOP``. A model shown only the schema writes ``WHERE category =
'laptop'``, which parses, validates, passes every permission check, and returns
nothing -- the failure that reads to a user as "it doesn't work" rather than as a
near miss. These are the types for fixing that.

A value dictionary is **curated, not merely observed**. Sampling proposes; a
person approves. That is the same shape :mod:`vanna.capabilities.knowledge` uses
for examples, and for the same reason: a value read out of a production column
may be a customer's name, and putting it in a prompt is a disclosure decision
rather than a caching one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


class ReviewStatus(str, Enum):
    """Whether a sampled value may be shown to the model.

    New values arrive ``PENDING`` and are inert. Nothing reaches a prompt until
    somebody says it may -- so turning sampling on cannot, by itself, start
    leaking column contents into an LLM request.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MatchTier(str, Enum):
    """How a term was matched, in descending order of certainty.

    The order is the design. Each tier is strictly less certain than the one
    above it, so reaching a lower one is always a fallback and a confident match
    is never displaced by a speculative one.
    """

    EXACT = "exact"
    NORMALIZED = "normalized"
    """Case, and a trailing plural. The two ways the same word gets written."""

    SYNONYM = "synonym"
    """Tenant-authored equivalence. Outranks any amount of string similarity
    because a person who knows the domain typed it."""

    FUZZY = "fuzzy"
    """Character similarity, for typos and near-spellings."""

    SEMANTIC = "semantic"
    """Embedding proximity. Last, because it is the only tier that costs a
    round trip and the only one that can relate two strings sharing no
    characters -- which is powerful and correspondingly easy to be wrong with."""


class ValueMatch(BaseModel):
    """One candidate spelling, and how it was reached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    term: str = Field(description="What the user typed.")
    value: str = Field(description="What the column actually stores.")
    tier: MatchTier
    confidence: float = Field(ge=0.0, le=1.0)
    column: Optional[str] = Field(
        default=None, description="Qualified column this value belongs to."
    )


class SampledValue(BaseModel):
    """One distinct value observed in a column, awaiting or holding review."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = "default"
    data_source_id: str = "default"
    table: str
    column: str
    value: str
    occurrences: Optional[int] = None
    status: ReviewStatus = ReviewStatus.PENDING
    reviewed_by: Optional[str] = None
    first_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


class ValueSynonym(BaseModel):
    """A term a person declared equivalent to a stored value.

    This is what replaces the hardcoded English word lists a prototype ships
    with. ``label`` is the human-facing half of a code: an administrator writing
    ``value='C', label='Cancelled'`` is what lets a question about cancelled
    orders reach the code ``C``, which no amount of string similarity would find.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = "default"
    data_source_id: str = "default"
    table: str
    column: str
    value: str = Field(description="The stored value this term maps to.")
    terms: List[str] = Field(default_factory=list, max_length=50)
    label: Optional[str] = Field(
        default=None, description="A readable name for a coded value."
    )

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


class ColumnValues(BaseModel):
    """One column's approved dictionary, ready to match against.

    Assembled from approved :class:`SampledValue` rows plus any
    :class:`ValueSynonym` entries, or built directly from a catalog column's
    ``categories``. The matcher takes only this -- no store, no session -- which
    is what keeps it a pure function.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    qualified_name: str
    values: Tuple[str, ...] = ()
    #: Stored value -> terms a person declared equivalent to it.
    synonyms: Dict[str, Tuple[str, ...]] = Field(default_factory=dict)
    #: Stored code -> readable label.
    labels: Dict[str, str] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.values

    @classmethod
    def from_column(cls, table: str, column) -> "ColumnValues":
        """Build from a catalog column that already carries its categories.

        Lets value resolution work on a freshly scanned catalog with no
        curation at all -- the scanner records low-cardinality values, and those
        are exactly the ones worth matching against.
        """
        return cls(
            qualified_name=f"{table}.{column.name}",
            values=tuple(getattr(column, "categories", None) or ()),
        )
