"""Curated knowledge: verified examples and business rules.

Two stores with deliberately different trust levels, kept separate from
``AgentMemory``:

===============  ==================  ==========================  ================
                 AgentMemory         ExampleStore                InstructionStore
===============  ==================  ==========================  ================
Written by       the agent           humans, or agent on          humans
                                     verified success
Trust            advisory            authoritative                authoritative
Validated        no                  yes (parses, tables exist)   no
Retrieval        similarity          similarity                   **scope rules**
===============  ==================  ==========================  ================

That last row is the important one. A rule like "always exclude test accounts"
must apply to *every* query, not only the ones that happen to embed near it.
Storing such a rule in a similarity-ranked memory means it silently stops
applying whenever five other memories score higher -- a correctness bug wearing
a retrieval feature's clothing. Instructions resolve by scope instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class ExampleStatus(str, Enum):
    """Trust level of a stored NL->SQL pair."""

    CANDIDATE = "candidate"
    """Captured automatically from a successful execution. Usable as a weak
    signal, but unreviewed -- 'it ran' is not 'it was right'."""

    VERIFIED = "verified"
    """Reviewed by a human, or promoted after confirmed-correct results.
    Safe to present to the model as an authoritative pattern."""

    REJECTED = "rejected"
    """Reviewed and found wrong. Retained rather than deleted so the same bad
    pattern is not re-captured on the next run."""


class Example(BaseModel):
    """A question paired with the SQL that correctly answers it.

    Few-shot examples in the target dialect are the highest-leverage accuracy
    intervention available in text-to-SQL -- well above prompt wording.
    """

    id: str = Field(default_factory=_new_id)
    question: str
    sql: str
    status: ExampleStatus = ExampleStatus.CANDIDATE

    tenant_id: str = "default"
    data_source_id: str = "default"

    tables: List[str] = Field(
        default_factory=list,
        description=(
            "Tables the SQL references, extracted at write time. Enables the "
            "table-boosting trick: an example that answered a similar question "
            "is strong evidence about which tables matter, so its tables can be "
            "pinned into the schema context for free."
        ),
    )
    tags: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    verified_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        """Render for a prompt."""
        return f"Question: {self.question}\nSQL:\n{self.sql}"


class ExampleHit(BaseModel):
    """A retrieved example and its relevance."""

    example: Example
    score: float = 1.0


class InstructionScope(str, Enum):
    """Determines when an instruction applies.

    Scope, not similarity, is what makes a universal rule universal.
    """

    GLOBAL = "global"
    """Applies to every query for the tenant."""

    DATA_SOURCE = "data_source"
    """Applies to every query against one data source."""

    TABLE = "table"
    """Applies when a specific table is in the selected schema context."""

    GROUP = "group"
    """Applies to users in a specific group -- the natural way to express
    per-audience data policy in prose."""


class InstructionOrigin(str, Enum):
    """Where a rule came from, which decides who is allowed to change it."""

    TENANT = "tenant"
    """Authored in this workspace. Fully owned by it."""

    LIBRARY = "library"
    """Copied from a starter pack. Owned by the workspace once copied, and
    editable -- the pack is a starting point, not a subscription."""

    PLATFORM = "platform"
    """The deployment-wide baseline. Never owned by a workspace and never
    stored per tenant, so editing it reaches every workspace at once."""


class Instruction(BaseModel):
    """A durable business rule injected into the prompt.

    Examples: "monetary amounts are stored in cents", "always exclude rows
    where is_test is true", "fiscal year starts in February".
    """

    id: str = Field(default_factory=_new_id)
    text: str
    scope: InstructionScope = InstructionScope.GLOBAL
    scope_ref: Optional[str] = Field(
        default=None,
        description="Data source id, table name, or group name -- required for "
        "every scope except GLOBAL.",
    )

    tenant_id: str = "default"
    priority: int = Field(
        default=0,
        description="Higher wins when the token budget forces truncation, and "
        "orders the rendered list. Does not detect contradictions -- two rules "
        "that conflict stay a human problem.",
    )
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # -- provenance ----------------------------------------------------
    #
    # Typed fields rather than entries in `metadata`, for two reasons. The
    # markdown backend drops `metadata` on write, so provenance would not
    # survive a round trip; and `locked` decides whether a delete is refused,
    # which is not something an untyped dict an API payload can populate
    # should be deciding.
    origin: InstructionOrigin = InstructionOrigin.TENANT
    locked: bool = Field(
        default=False,
        description="Set on platform rules. A locked rule cannot be edited or "
        "deleted by the workspace it applies to.",
    )
    source_pack: Optional[str] = Field(
        default=None,
        description="Id of the starter pack a LIBRARY rule was copied from.",
    )
    updated_at: Optional[datetime] = None

    def applies_to(
        self,
        *,
        data_source_id: Optional[str] = None,
        tables: Optional[List[str]] = None,
        user_groups: Optional[List[str]] = None,
    ) -> bool:
        """Whether this instruction applies in the given situation."""
        if not self.enabled:
            return False
        if self.scope == InstructionScope.GLOBAL:
            return True
        if self.scope == InstructionScope.DATA_SOURCE:
            return self.scope_ref == data_source_id
        if self.scope == InstructionScope.TABLE:
            if not tables or not self.scope_ref:
                return False
            wanted = self.scope_ref.lower()
            # Match bare or qualified names: a rule about `orders` should fire
            # whether the context lists `orders` or `public.orders`.
            return any(
                t.lower() == wanted or t.lower().endswith(f".{wanted}")
                for t in tables
            )
        if self.scope == InstructionScope.GROUP:
            return bool(user_groups) and self.scope_ref in (user_groups or [])
        return False
