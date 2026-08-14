"""Presenting the semantic layer through the catalog interface.

The agent already knows how to read a :class:`SchemaCatalog`: the retrieval
enhancer calls ``get_context``, the schema tools call ``get_tables`` and
``search_tables``, and the repair strategy calls ``get_table`` to name real
columns in a hint. Rather than teach all of that about manifests, this presents
a manifest *as* a catalog.

Consequences worth stating, because they are the point:

* ``get_context``'s full-vs-search switch, the token budget, and
  ``RetrievalContextEnhancer`` all keep working with no changes.
* The model is shown ``amount`` with its expression, not ``amount_cents``.
* When a model backs a physical table, **the physical table is hidden**. Showing
  both invites the model to query the raw table, which bypasses the semantic
  layer -- and, once row-level rules exist, bypasses those too.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

from ...semantic.models import Manifest, SemanticColumn, SemanticModel
from .base import SchemaCatalog
from .models import (
    CatalogStatus,
    ColumnMetadata,
    ForeignKey,
    RelationshipMetadata,
    TableMetadata,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...core.tool.models import ToolContext

logger = logging.getLogger(__name__)


class SemanticSchemaCatalog(SchemaCatalog):
    """A read-only catalog view over a manifest, backed by a physical catalog.

    Args:
        manifest: What the deployment declares.
        physical: The scanned catalog. Consulted for two things only -- the
            observed values a scan profiled, and the tables no model covers.
            Never re-profiled here; ``scanner.py`` already did that work with
            sensitive-column and free-text exclusion applied.
        expose_unmodelled: Whether tables with no model still appear. False by
            default: a half-modelled database that shows both layers teaches the
            model to reach for whichever it saw last.
    """

    def __init__(
        self,
        manifest: Manifest,
        *,
        physical: Optional[SchemaCatalog] = None,
        expose_unmodelled: bool = False,
    ) -> None:
        self.manifest = manifest
        self.physical = physical
        self.expose_unmodelled = expose_unmodelled

    @property
    def index(self):
        """The search index of the catalog underneath, if there is one.

        Wrapping a catalog hid its ``index`` attribute, which is how
        ``/schema`` came to report ``index_backend: none`` for every workspace
        with a semantic layer -- while the log said ``hybrid(lexical+qdrant)``.
        Reporting the effective backend exists precisely so a silent downgrade
        is visible, so a wrapper that breaks it defeats the purpose.
        """
        return getattr(self.physical, "index", None)

    # ------------------------------------------------------------------
    # Projection
    # ------------------------------------------------------------------

    def _column(
        self, column: SemanticColumn, *, profiled: Optional[ColumnMetadata]
    ) -> ColumnMetadata:
        """One semantic column, as catalog metadata."""
        description = column.description
        if column.is_calculated and column.expression:
            # The expression belongs in the description or the model recomputes
            # the value from the underlying columns and gets a different answer.
            computed = f"Computed as {column.expression}."
            description = f"{description} {computed}".strip() if description else computed

        return ColumnMetadata(
            name=column.name,
            data_type=column.type,
            nullable=not column.not_null,
            is_primary_key=column.is_primary_key,
            description=description,
            # Carried from the scan, not recomputed.
            categories=column.categories or (profiled.categories if profiled else None),
            low_cardinality=bool(
                column.categories or (profiled.low_cardinality if profiled else False)
            ),
            sample_values=(
                column.sample_values or (profiled.sample_values if profiled else None)
            ),
        )

    def _table(
        self, model: SemanticModel, *, profiled: Optional[TableMetadata], tenant: str
    ) -> TableMetadata:
        by_name: Dict[str, ColumnMetadata] = {}
        if profiled is not None:
            by_name = {c.name.lower(): c for c in profiled.columns}

        columns = [
            self._column(column, profiled=by_name.get(column.name.lower()))
            for column in model.visible_columns
        ]

        # Relationship handles are surfaced as pseudo-columns so the model can
        # see that `orders.customer.region` is available at all. Typed by the
        # related model's name, which is what the compiler expects.
        for handle in model.relationship_columns:
            columns.append(
                ColumnMetadata(
                    name=handle.name,
                    data_type=handle.type,
                    description=(
                        f"Related {handle.type}. Read its fields as "
                        f"{handle.name}.<column>."
                    ),
                )
            )

        return TableMetadata(
            table_name=model.name,
            schema_name=None,  # models are referenced unqualified
            description=model.description,
            columns=columns,
            row_count_estimate=profiled.row_count_estimate if profiled else None,
            tenant_id=tenant,
            status=CatalogStatus.SCANNED,
            metadata={"layer": "semantic", "source": model.source},
        )

    @staticmethod
    def _tenant(context: "ToolContext") -> str:
        return getattr(context, "tenant_id", None) or "default"

    async def _profiled(self, context: "ToolContext") -> Dict[str, TableMetadata]:
        """Physical tables, keyed by lowered name. Empty when unavailable."""
        if self.physical is None:
            return {}
        try:
            tables = await self.physical.get_tables(context)
        except Exception as exc:  # a scan outage must not blank the catalog
            logger.warning("Physical catalog unavailable: %s", exc)
            return {}

        keyed: Dict[str, TableMetadata] = {}
        for table in tables:
            keyed[table.table_name.lower()] = table
            keyed[table.qualified_name.lower()] = table
        return keyed

    def _covered(self) -> set:
        """Physical names a model already stands in for."""
        names = set()
        for model in self.manifest.models:
            names.add(model.name.lower())
            if model.table_reference:
                names.add(model.table_reference.lower())
                names.add(model.table_reference.split(".")[-1].lower())
        return names

    # ------------------------------------------------------------------
    # SchemaCatalog
    # ------------------------------------------------------------------

    async def get_tables(
        self,
        context: "ToolContext",
        *,
        schema: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        tenant = self._tenant(context)
        profiled = await self._profiled(context)

        tables = [
            self._table(
                model,
                profiled=profiled.get(
                    (model.table_reference or model.name).lower()
                ) or profiled.get(model.name.lower()),
                tenant=tenant,
            )
            for model in self.manifest.models
        ]

        for view in self.manifest.views:
            tables.append(
                TableMetadata(
                    table_name=view.name,
                    description=view.description or "Saved query.",
                    tenant_id=tenant,
                    status=CatalogStatus.SCANNED,
                    metadata={"layer": "semantic", "kind": "view"},
                )
            )

        if self.expose_unmodelled and profiled:
            covered = self._covered()
            seen = set()
            for key, table in profiled.items():
                if table.table_name.lower() in covered or id(table) in seen:
                    continue
                seen.add(id(table))
                tables.append(table)

        return tables

    async def get_table(
        self,
        context: "ToolContext",
        name: str,
        *,
        data_source_id: Optional[str] = None,
    ) -> Optional[TableMetadata]:
        # Unqualified match first: models are referenced by bare name, and a
        # `schema.table` lookup for a model should still find it.
        bare = name.split(".")[-1]
        for table in await self.get_tables(context):
            if table.table_name.lower() in (name.lower(), bare.lower()):
                return table
        return None

    async def search_tables(
        self,
        context: "ToolContext",
        query: str,
        *,
        limit: int = 10,
        data_source_id: Optional[str] = None,
    ) -> List[TableMetadata]:
        """Keyword overlap over names, descriptions, and observed values.

        Matching on a column's *values* is what makes "cancelled orders" find
        the orders model when the word never appears in a name -- the scan
        already recorded that `status` contains `CANCELLED`.
        """
        terms = {t for t in _tokenise(query) if len(t) > 2}
        if not terms:
            return (await self.get_tables(context))[:limit]

        scored = []
        for table in await self.get_tables(context):
            haystack = {*_tokenise(table.table_name), *_tokenise(table.description or "")}
            values: set = set()
            for column in table.columns:
                haystack |= _tokenise(column.name)
                haystack |= _tokenise(column.description or "")
                for value in (column.categories or [])[:50]:
                    values |= _tokenise(str(value))

            score = len(terms & haystack) * 2 + len(terms & values)
            if score:
                scored.append((score, table))

        scored.sort(key=lambda pair: (-pair[0], pair[1].table_name))
        return [table for _, table in scored[:limit]]

    async def get_relationships(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> List[RelationshipMetadata]:
        """Declared joins, projected into the catalog's shape.

        The catalog's model is a single column pair, so a composite condition
        cannot be represented faithfully; it is carried in ``description`` where
        it still reaches the prompt.
        """
        tenant = self._tenant(context)
        out: List[RelationshipMetadata] = []

        for relationship in self.manifest.relationships:
            left, right = relationship.models
            from_column, to_column = _split_condition(relationship.condition)
            out.append(
                RelationshipMetadata(
                    name=relationship.name,
                    from_table=left,
                    from_column=from_column or "?",
                    to_table=right,
                    to_column=to_column or "?",
                    join_type=relationship.join_type.value.lower(),
                    description=(
                        relationship.description
                        or f"ON {relationship.condition}"
                    ),
                    tenant_id=tenant,
                )
            )
        return out

    # -- writes ---------------------------------------------------------
    #
    # A manifest is authored in git and compiled by `vanna project build`. A
    # scanner writing into it at runtime would silently overwrite a reviewed
    # business definition, so writes go to the physical catalog or nowhere.

    async def upsert_tables(
        self, context: "ToolContext", tables: List[TableMetadata]
    ) -> None:
        if self.physical is not None:
            await self.physical.upsert_tables(context, tables)
        else:
            logger.debug("No physical catalog; discarding %d scanned tables", len(tables))

    async def upsert_relationships(
        self, context: "ToolContext", relationships: List[RelationshipMetadata]
    ) -> None:
        if self.physical is not None:
            await self.physical.upsert_relationships(context, relationships)

    async def clear(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
    ) -> int:
        if self.physical is not None:
            return await self.physical.clear(context, data_source_id=data_source_id)
        return 0


def _tokenise(text: str) -> set:
    """Lowercase word tokens, with snake_case split into parts."""
    import re

    tokens = set()
    for word in re.findall(r"[A-Za-z0-9]+", (text or "").lower()):
        tokens.add(word)
        tokens.update(part for part in word.split("_") if part)
    return tokens


def _split_condition(condition: str):
    """Best-effort ``a.x = b.y`` -> ``("x", "y")``.

    Only used to fill the catalog's single-column shape. A composite condition
    yields ``(None, None)`` and travels in the description instead of being
    misrepresented as one pair.
    """
    import re

    matches = re.findall(
        r"([A-Za-z_][\w]*)\s*\.\s*([A-Za-z_][\w]*)\s*=\s*([A-Za-z_][\w]*)\s*\.\s*([A-Za-z_][\w]*)",
        condition or "",
    )
    if len(matches) != 1:
        return None, None
    _, left_column, _, right_column = matches[0]
    return left_column, right_column
