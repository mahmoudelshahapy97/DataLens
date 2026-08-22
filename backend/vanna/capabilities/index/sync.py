"""Bringing an index into step with its source, without rebuilding it.

Both knowledge and catalog stores used to do the same thing before every search::

    self.index.clear(tenant_id=tenant)
    self.index.add(documents_for_examples(candidates, tenant_id=tenant))

For the in-process BM25 index that is deliberate and correct: building it is
sub-millisecond, and the alternative -- an index that disagrees with the markdown
someone just edited -- is the failure those stores were designed to avoid.

Against a *persistent* index it is ruinous. Every question would delete the
tenant's vectors and re-embed the entire corpus to answer one query. The cost is
not a constant factor; it is a per-question embedding bill.

So the store no longer chooses. It calls :func:`sync_documents`, and the index
decides by whether it can report :meth:`SearchIndex.fingerprints`:

* it cannot -> clear and rebuild, exactly as before
* it can    -> compare content hashes, and touch only what changed

Nothing else in either store changes.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Union

from .base import IndexDocument, SearchIndex

logger = logging.getLogger(__name__)


def fingerprint(document: IndexDocument) -> str:
    """A stable hash of everything that would change the stored entry.

    Text and boost, because both affect retrieval. Not ``metadata``: it is
    carried through to the hit for the caller's convenience and changing it
    should not force a re-embedding, which is the expensive part.
    """
    digest = hashlib.sha256()
    digest.update(document.text.encode("utf-8"))
    digest.update(f"|{document.boost}".encode("utf-8"))
    return digest.hexdigest()[:32]


@dataclass
class SyncReport:
    """What a sync actually did. Returned so callers can log or assert on it."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    rebuilt: bool = False
    """True when the index could not report fingerprints and was rebuilt."""

    @property
    def changed(self) -> int:
        return self.added + self.updated + self.removed

    def __str__(self) -> str:
        if self.rebuilt:
            return f"rebuilt ({self.added} documents)"
        return (
            f"+{self.added} ~{self.updated} -{self.removed} "
            f"({self.unchanged} unchanged)"
        )


def sync_documents(
    index: SearchIndex,
    documents: Iterable[IndexDocument],
    *,
    tenant_id: str = "default",
    kinds: Optional[Union[str, Sequence[str]]] = None,
) -> SyncReport:
    """Make *index* match *documents* for one tenant, doing the least work.

    Args:
        index: The index to update.
        documents: The complete set that should be present for this tenant and
            these kinds. Completeness matters: anything absent within the scope
            is treated as deleted.
        tenant_id: Scope. Never crosses tenants.
        kinds: Which document kinds this call is authoritative for. **Getting
            this right is what stops one store deleting another's documents**:
            the knowledge store owns ``example``, the catalog owns ``table`` and
            ``column_values``, and they share an index. Passing None claims the
            whole tenant, which only a caller that really owns everything should
            do.

    Returns:
        A :class:`SyncReport`.
    """
    scope: Optional[List[str]]
    if kinds is None:
        scope = None
    elif isinstance(kinds, str):
        scope = [kinds]
    else:
        scope = list(kinds)
    items: List[IndexDocument] = list(documents)

    # Stamp each document with its hash so the index can store it and answer
    # `fingerprints()` later without re-reading the source.
    for document in items:
        document.metadata.setdefault("fingerprint", fingerprint(document))

    existing: Optional[Dict[str, str]] = None
    try:
        if scope is None:
            existing = index.fingerprints(tenant_id=tenant_id, kind=None)
        else:
            # One lookup per kind, merged. Keeps SearchIndex.fingerprints simple
            # -- it answers about one kind at a time -- while letting a caller
            # own several.
            merged: Dict[str, str] = {}
            for one in scope:
                part = index.fingerprints(tenant_id=tenant_id, kind=one)
                if part is None:
                    merged = None  # type: ignore[assignment]
                    break
                merged.update(part)
            existing = merged
    except Exception as exc:  # noqa: BLE001
        # An index that cannot be read cannot be diffed. Rebuilding is correct
        # and safe; failing the search because a store was briefly unreachable
        # is not.
        logger.warning(
            "Could not read fingerprints from %s (%s); rebuilding instead.",
            getattr(index, "name", type(index).__name__),
            type(exc).__name__,
        )

    if existing is None:
        # Rebuild. `clear` takes a tenant, not a kind, so an index that cannot
        # report fingerprints is one where kinds cannot be rebuilt separately --
        # true of LexicalIndex, where every store re-adds its own documents on
        # its next search anyway, and the rebuild is sub-millisecond.
        index.clear(tenant_id=tenant_id)
        index.add(items)
        return SyncReport(added=len(items), rebuilt=True)

    report = SyncReport()
    write: List[IndexDocument] = []

    for document in items:
        current = existing.pop(document.id, None)
        if current is None:
            write.append(document)
            report.added += 1
        elif current != document.metadata["fingerprint"]:
            write.append(document)
            report.updated += 1
        else:
            report.unchanged += 1

    # Whatever is left in `existing` is in the index and no longer in the
    # source. Removing it is the half that stops a deleted rule from being
    # retrieved forever -- worse than one that was never indexed, because
    # someone believes they deleted it.
    stale = list(existing)
    if stale:
        index.remove(stale)
        report.removed = len(stale)

    if write:
        index.add(write)

    if report.changed:
        logger.info(
            "Index sync tenant=%s kinds=%s: %s",
            tenant_id,
            ",".join(scope) if scope else "all",
            report,
        )
    return report
