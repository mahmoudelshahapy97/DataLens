"""Records of what the agent produced, for quality analysis.

``ConversationStore`` models a *chat*: an ordered list of messages. That is the
right shape for replaying a conversation and the wrong shape for answering
questions about quality. Reconstructing "what fraction of generated SQL ran
successfully last week?" from message text is archaeology.

So generations are recorded separately and relationally. Three questions become
one query each:

* *Is the agent working?* -- success rate over time.
* *Where is it failing?* -- group failures by table or error kind.
* *What should become a golden example?* -- successful, positively-rated turns.

The two ``retrieved_*`` fields are the ones to notice. They record what context
the model was *given*, so an outcome can be attributed back to retrieval
quality. "Turns that retrieved example X fail 40% of the time" is directly
actionable -- retire example X. Without provenance, tuning retrieval is
guesswork dressed up as intuition.

Deliberately distinct from ``AuditLogger``. **Audit answers "who accessed
what, and were they allowed?" -- a security question. Generations answer "what
was produced, and was it right?" -- a quality question.** Keeping the split
clean stops both from becoming a dumping ground.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GenerationStatus(str, Enum):
    """Outcome of one SQL generation."""

    VALID = "valid"
    """Executed and returned results."""

    INVALID = "invalid"
    """The database rejected it."""

    REJECTED_BY_POLICY = "rejected_by_policy"
    """Blocked before execution. Tracked separately from INVALID because a
    rising rate here is a security signal, not a quality one."""

    TIMEOUT = "timeout"
    EMPTY = "empty"
    """Ran successfully and returned zero rows. Separated because it is the
    signature of an invented filter literal -- a query that is syntactically
    perfect and semantically wrong. Lumping it in with VALID hides the single
    most common wrong-answer mode."""


class Feedback(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class SqlGeneration(BaseModel):
    """One question, the SQL produced for it, and what happened."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # -- Scope
    tenant_id: str = "default"
    data_source_id: str = "default"
    user_id: str = ""

    # -- Correlation. Both already exist on ToolContext, so linking a
    #    generation back to its conversation costs nothing.
    conversation_id: str = ""
    request_id: str = ""

    # -- What happened
    question: str = ""
    sql: str = ""
    status: GenerationStatus = GenerationStatus.VALID
    error: Optional[str] = Field(
        default=None,
        description="Sanitized error. Never the raw driver message -- those "
        "carry query text and sometimes data values.",
    )
    error_kind: Optional[str] = Field(
        default=None, description="Classification from core.recovery.sql"
    )

    row_count: Optional[int] = None
    truncated: bool = False
    execution_ms: Optional[float] = None
    repair_attempts: int = 0

    # -- Cost
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None

    # -- Provenance: what the model was shown
    retrieved_example_ids: List[str] = Field(default_factory=list)
    retrieved_table_names: List[str] = Field(default_factory=list)
    retrieval_strategy: Optional[str] = Field(
        default=None, description="'full' or 'search' -- see SchemaContext"
    )

    # -- Human verdict
    feedback: Optional[Feedback] = None
    feedback_comment: Optional[str] = None

    created_at: datetime = Field(default_factory=_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """Ran without error. Note this includes EMPTY.

        Zero rows *is* a successful execution -- whether it is a correct
        answer is a different question, which is exactly why EMPTY is tracked
        as its own status rather than folded into either bucket.
        """
        return self.status in (GenerationStatus.VALID, GenerationStatus.EMPTY)

    @property
    def is_promotable(self) -> bool:
        """Whether this is a candidate for the verified example store.

        Requires all three of: it ran, it returned something, and a human said
        it was right. Any two without the third is not evidence of a good
        example -- a positively-rated query returning zero rows most likely
        means the user did not check.
        """
        return (
            self.status == GenerationStatus.VALID
            and self.feedback == Feedback.POSITIVE
            and bool(self.sql.strip())
        )


class GenerationStats(BaseModel):
    """Aggregates over a set of generations."""

    total: int = 0
    valid: int = 0
    invalid: int = 0
    empty: int = 0
    rejected_by_policy: int = 0
    timeout: int = 0
    positive_feedback: int = 0
    negative_feedback: int = 0
    total_cost_usd: float = 0.0
    avg_execution_ms: float = 0.0
    repair_rate: float = 0.0

    @property
    def success_rate(self) -> float:
        if not self.total:
            return 0.0
        return (self.valid + self.empty) / self.total

    def summary(self) -> str:
        return (
            f"{self.total} generations, {self.success_rate:.0%} ran successfully "
            f"({self.invalid} errors, {self.empty} returned nothing, "
            f"{self.rejected_by_policy} blocked by policy). "
            f"Feedback: {self.positive_feedback} up / "
            f"{self.negative_feedback} down. "
            f"Repair rate {self.repair_rate:.0%}."
        )
