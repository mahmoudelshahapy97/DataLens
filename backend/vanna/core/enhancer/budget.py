"""Token-budgeted prompt assembly.

Once instructions, examples, schema, memories, and history all compete for one
context window, the failure mode without a budget is a hard provider error at
the worst possible moment -- or silent truncation by whichever layer hits its
limit first. An explicit budget turns an outage into a graceful degradation.

The key distinction, borrowed from SQLChat's prompt assembly, is between
**display order** and **fill order**. What the model should read first is not
what you should give up last. Sections are rendered in a fixed, readable order,
but filled in priority order, so squeezing the budget drops the least valuable
content rather than the content that happens to be at the end.

Truncation is always **whole-item**: drop an entire example or table, never
half of one. A half-rendered table schema is worse than an absent one -- it
invites the model to invent the columns it cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: Fallback when no tokenizer is available. Four characters per token is a
#: reasonable English average. Deliberately crude: an accurate count needs a
#: per-provider tokenizer, and Vanna supports eight providers.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text*."""
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass
class Section:
    """One block of prompt content competing for the budget."""

    name: str
    title: str
    items: List[str]
    priority: int = 0
    """Fill order. Higher fills first and survives truncation longest."""

    display_order: int = 0
    """Render order. Independent of priority."""

    preamble: str = ""
    """Optional line between the heading and the items."""

    def render(self, items: Optional[List[str]] = None) -> str:
        chosen = self.items if items is None else items
        if not chosen:
            return ""
        parts = [self.title]
        if self.preamble:
            parts.append(self.preamble)
        parts.extend(chosen)
        return "\n".join(parts)


@dataclass
class BudgetPolicy:
    """How much of the context window each section may claim.

    Allocations are fractions of the usable budget. Anything a section does not
    use spills to the next section in priority order, so a tenant with no
    instructions automatically gets a larger schema section rather than wasting
    the allowance.
    """

    total_tokens: int = 128_000
    reserve_for_response: int = 4_096
    reserve_for_history: int = 8_192

    allocations: Dict[str, float] = field(
        default_factory=lambda: {
            "instructions": 0.10,
            "examples": 0.25,
            "schema": 0.50,
            "memories": 0.15,
        }
    )

    @property
    def usable_tokens(self) -> int:
        """Budget available to retrieved context."""
        return max(
            0,
            self.total_tokens - self.reserve_for_response - self.reserve_for_history,
        )

    def allocation_for(self, section: str) -> int:
        return int(self.usable_tokens * self.allocations.get(section, 0.0))


@dataclass
class AssemblyResult:
    """Assembled prompt text plus what it cost and what was dropped."""

    text: str
    tokens_used: int
    section_tokens: Dict[str, int] = field(default_factory=dict)
    truncated_sections: List[str] = field(default_factory=list)
    dropped_items: Dict[str, int] = field(default_factory=dict)
    #: How sources chose what they contributed -- e.g. the schema strategy and
    #: which tables were added as join bridges. For previews and debugging;
    #: never rendered into the prompt.
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def was_truncated(self) -> bool:
        return bool(self.truncated_sections)


def assemble(
    sections: List[Section],
    policy: BudgetPolicy,
    *,
    count_tokens: Optional[Callable[[str], int]] = None,
) -> AssemblyResult:
    """Fill sections by priority, render them by display order.

    Args:
        sections: Candidate sections, each with its items already rendered.
        policy: Budget and per-section allocations.
        count_tokens: Token counter. Defaults to the character heuristic; pass
            the provider's real tokenizer when accuracy matters.

    Returns:
        The assembled text plus per-section accounting, so callers can emit
        metrics and notice when a section is being silently squeezed.
    """
    counter = count_tokens or estimate_tokens

    result = AssemblyResult(text="", tokens_used=0)
    kept: Dict[str, List[str]] = {}
    spare = 0

    for section in sorted(sections, key=lambda s: -s.priority):
        if not section.items:
            kept[section.name] = []
            continue

        allowance = policy.allocation_for(section.name) + spare
        overhead = counter(section.title) + counter(section.preamble)
        remaining = allowance - overhead

        chosen: List[str] = []
        used = overhead
        dropped = 0
        for item in section.items:
            cost = counter(item) + 1
            if cost <= remaining:
                chosen.append(item)
                remaining -= cost
                used += cost
            else:
                # Whole items only -- never emit a partial table or example.
                dropped += 1

        kept[section.name] = chosen
        result.section_tokens[section.name] = used if chosen else 0
        if dropped:
            result.truncated_sections.append(section.name)
            result.dropped_items[section.name] = dropped

        # Hand the leftover to the next-priority section.
        spare = max(0, remaining)

    rendered = [
        s.render(kept.get(s.name, []))
        for s in sorted(sections, key=lambda s: s.display_order)
    ]
    result.text = "\n\n".join(part for part in rendered if part)
    result.tokens_used = sum(result.section_tokens.values())
    return result
