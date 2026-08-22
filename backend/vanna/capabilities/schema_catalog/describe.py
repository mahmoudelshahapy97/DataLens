"""Render catalog metadata as text for an LLM prompt."""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from .models import RelationshipMetadata, TableMetadata

#: Below this many characters, send the whole schema instead of searching it.
#:
#: ~30,000 characters is roughly 8,000 tokens for English, which fits
#: comfortably in a modern context window alongside examples and history.
#:
#: The threshold is measured in **characters, not tokens**, on purpose:
#: character length is free to compute once the text exists, while accurate
#: token counting needs a per-provider tokenizer. The 4:1 ratio holds for
#: English; CJK text runs nearer 1.5:1, so a CJK-heavy schema switches to
#: search sooner than strictly necessary -- the conservative direction, which
#: is the right way for the estimate to be wrong.
SCHEMA_FULL_TEXT_THRESHOLD = 30_000


def describe_schema(
    tables: Sequence[TableMetadata],
    relationships: Optional[Iterable[RelationshipMetadata]] = None,
    *,
    include_columns: bool = True,
) -> str:
    """Render tables and their join paths as structured plain text.

    Relationships are rendered as a distinct section rather than folded into
    each table, so the model sees the join graph as a whole. That framing
    matters for multi-hop questions: a per-table view makes each edge look
    local, while the graph view makes the path from A to C through B visible.
    """
    if not tables:
        return ""

    lines: List[str] = []
    for table in sorted(tables, key=lambda t: t.qualified_name):
        lines.append(table.describe(include_columns=include_columns))
        lines.append("")

    rel_list = list(relationships or [])
    if rel_list:
        lines.append("### Relationships (join paths)")
        lines.extend(r.describe() for r in sorted(rel_list, key=lambda r: r.name))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def describe_table_names(tables: Sequence[TableMetadata]) -> str:
    """Render a compact table inventory with no column detail.

    Useful as a cheap first pass: show the model what exists, let it ask for
    the columns of the few tables it actually needs.
    """
    if not tables:
        return ""
    lines = ["Available tables:"]
    for table in sorted(tables, key=lambda t: t.qualified_name):
        entry = f"  - {table.qualified_name}"
        if table.description:
            entry += f": {table.description}"
        lines.append(entry)
    return "\n".join(lines)
