"""The workspace's curated knowledge, linked to tables for retrieval.

Implements :class:`vanna.capabilities.schema_graph.SchemaKnowledge` over the
stores the console already writes:

* **Business domains** (``domain_store``). A question that uses a domain's name
  or one of its glossary terms points at that domain's tables. A term whose
  definition names a table explicitly -- ``revenue: SUM(invoice.total)`` --
  points at that table first, because it is the more specific claim.
* **Semantic cubes** (the manifest, when the workspace has one). Naming a cube,
  a measure or a dimension ("revenue", "invoice count") points at the model the
  cube is built on.
* **Core columns** (``catalog_store``). The columns an admin marked core, for
  whichever tables retrieval selected.

Hints only *add* tables to the search path's selection; they never remove one,
and a hinted table the caller cannot read is dropped by the catalog the same
way any other unreadable table is.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from vanna.capabilities.schema_graph import SchemaKnowledge
from vanna.capabilities.schema_graph.knowledge import phrase_in, words
from vanna.core.grants import normalize_table

logger = logging.getLogger("vanna.knowledge_links")

#: Dotted identifiers in a glossary definition: ``invoice.total`` or
#: ``chinook.invoice``. Bare words are not treated as table references --
#: "order" in a sentence is English far more often than it is a table.
_DOTTED = re.compile(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b")

#: Hints passed to retrieval, at most. A question matching half the glossary
#: is matching on common words, and hinting every table would undo search.
MAX_HINTS = 8


def match_domains(
    domains: Sequence[Dict[str, Any]], question: str
) -> List[Tuple[Dict[str, Any], List[str]]]:
    """Enabled domains the question touches, with the glossary terms it used.

    A domain is touched when the question uses its name or any of its terms.
    Order follows *domains*; terms are returned in glossary order.
    """
    present: Set[str] = set(words(question))
    matched: List[Tuple[Dict[str, Any], List[str]]] = []
    for domain in domains or []:
        if not domain.get("is_enabled"):
            continue
        terms = [
            term
            for term in sorted((domain.get("terminology") or {}).keys())
            if phrase_in(present, term)
        ]
        if terms or phrase_in(present, str(domain.get("name") or "")):
            matched.append((domain, terms))
    return matched


def tables_named_in(text: str) -> List[str]:
    """Candidate table names from dotted identifiers in *text*.

    ``invoice.total`` yields ``invoice`` (table.column) and ``invoice.total``
    (schema.table); retrieval resolves whichever exists and ignores the rest.
    """
    found: List[str] = []
    for left, right in _DOTTED.findall(text or ""):
        for candidate in (left, f"{left}.{right}"):
            if candidate.lower() not in found:
                found.append(candidate.lower())
    return found


class WorkspaceKnowledge(SchemaKnowledge):
    """Domains, cubes and core columns for one workspace and data source."""

    def __init__(
        self,
        *,
        tenant_id: str,
        data_source_id: str,
        domains: Any = None,
        catalog_store: Any = None,
        manifest: Any = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.data_source_id = data_source_id
        self.domains = domains
        self.catalog_store = catalog_store
        self.manifest = manifest

    async def table_hints(self, context: Any, question: str) -> List[str]:
        hints: List[str] = []

        def add(names: Sequence[str]) -> None:
            for name in names:
                key = str(name).lower()
                if key and key not in hints:
                    hints.append(key)

        if self.domains is not None:
            try:
                domains = await self.domains.list_domains(
                    self.tenant_id, data_source_id=self.data_source_id
                )
            except Exception as exc:
                logger.debug("Domains unavailable for hints: %s", exc)
                domains = []
            for domain, terms in match_domains(domains, question):
                glossary = domain.get("terminology") or {}
                for term in terms:
                    add(tables_named_in(str(glossary.get(term) or "")))
                add(domain.get("tables") or [])

        add(self._cube_tables(question))
        return hints[:MAX_HINTS]

    def _cube_tables(self, question: str) -> List[str]:
        """Models behind the cubes whose name, measures or dimensions are named."""
        cubes = getattr(self.manifest, "cubes", None) or []
        present = set(words(question))
        found: List[str] = []
        for cube in cubes:
            phrases = [cube.name]
            phrases += [m.name for m in cube.measures]
            phrases += [d.name for d in cube.dimensions]
            phrases += [t.name for t in cube.time_dimensions]
            if any(phrase_in(present, p) for p in phrases):
                found.append(cube.base_object)
        return found

    async def core_columns(
        self, context: Any, tables: Sequence[Any]
    ) -> Dict[str, List[str]]:
        getter = getattr(self.catalog_store, "get_core_columns_map", None)
        if getter is None or not tables:
            return {}
        keys = {normalize_table(t.qualified_name): t.qualified_name for t in tables}
        stored = await getter(self.tenant_id, self.data_source_id, list(keys))
        return {keys[k]: list(v) for k, v in (stored or {}).items() if k in keys and v}


def manifest_of(catalog: Any) -> Optional[Any]:
    """The semantic manifest behind a catalog, if any.

    ``GrantFilteredCatalog`` passes unknown attributes through to what it
    wraps, so this reaches a ``SemanticSchemaCatalog`` under the guard.
    """
    return getattr(catalog, "manifest", None)
