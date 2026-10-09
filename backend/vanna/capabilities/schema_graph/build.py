"""Build a :class:`SchemaGraph` from catalog metadata.

Edges come from two sources, merged: curated ``RelationshipMetadata`` (the
scanner's foreign keys, or a semantic manifest's relationships) and the foreign
keys recorded on the columns themselves. Both already exist in the catalog;
nothing here scans the database.

The graph is built from whatever the catalog returns *for this caller*, so a
``GrantFilteredCatalog`` has already removed the tables a role cannot read --
a join path can never route through a table its reader is not allowed to see.
Building is linear in the number of columns and the catalog read dominates it,
so the graph is rebuilt per call rather than cached and invalidated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Tuple

from vanna.capabilities.schema_catalog.models import RelationshipMetadata, TableMetadata

from .graph import JoinEdge, SchemaGraph

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.capabilities.schema_catalog import SchemaCatalog
    from vanna.core.tool import ToolContext

_CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}


def table_lookup(tables: Iterable[TableMetadata]) -> Dict[str, str]:
    """Lower-cased bare *and* qualified name -> canonical qualified name.

    The model, the catalog and a manifest each spell table names their own way;
    this is the one place that reconciles them. A bare name shared by two
    schemas resolves to whichever table came first rather than to neither.
    """
    canonical: Dict[str, str] = {}
    for table in tables:
        qualified = table.qualified_name
        canonical[qualified.lower()] = qualified
    for table in tables:
        canonical.setdefault(table.table_name.lower(), table.qualified_name)
    return canonical


def build_schema_graph(
    tables: Sequence[TableMetadata],
    relationships: Iterable[RelationshipMetadata] = (),
) -> SchemaGraph:
    """Graph over *tables*; edges to tables outside the set are dropped."""
    canonical = table_lookup(tables)
    by_name = {t.qualified_name: t for t in tables}
    graph = SchemaGraph(by_name)

    for rel in relationships or []:
        if not getattr(rel, "is_usable", True):
            continue  # rejected, or an inferred guess too weak to act on
        source = canonical.get((rel.from_table or "").lower())
        target = canonical.get((rel.to_table or "").lower())
        if not (source and target):
            continue
        cardinality = rel.join_type if rel.join_type in _CARDINALITIES else "many_to_one"
        graph.add_edge(
            JoinEdge(
                left=source,
                left_column=rel.from_column,
                right=target,
                right_column=rel.to_column,
                cardinality=cardinality,
                source=_source_of(rel),
            )
        )

    for table in tables:
        for column in table.columns or []:
            fk = column.foreign_key
            if fk is None:
                continue
            target = canonical.get((fk.references_table or "").lower())
            if not target:
                continue
            holder_column = fk.column or column.name
            graph.add_edge(
                JoinEdge(
                    left=table.qualified_name,
                    left_column=holder_column,
                    right=target,
                    right_column=fk.references_column,
                    cardinality=_fk_cardinality(table, holder_column),
                    source="declared",
                )
            )

    return graph


async def load_schema_graph(
    catalog: "SchemaCatalog",
    context: "ToolContext",
    *,
    data_source_id: Optional[str] = None,
    tables: Optional[Sequence[TableMetadata]] = None,
) -> Tuple[SchemaGraph, List[TableMetadata]]:
    """Read the catalog for *context* and build its graph.

    Pass *tables* when the caller has already fetched them, to save a read.
    Relationships failing to load is not fatal: foreign keys on the columns
    still give a usable graph.
    """
    if tables is None:
        tables = await catalog.get_tables(context, data_source_id=data_source_id)
    try:
        relationships = await catalog.get_relationships(
            context, data_source_id=data_source_id
        )
    except Exception:
        relationships = []
    return build_schema_graph(tables, relationships), list(tables)


def _source_of(rel: RelationshipMetadata) -> str:
    """Edge weight class: an unconfirmed inferred join costs more.

    Once an admin accepts an inferred relationship it is as good as a declared
    foreign key -- somebody who knows the data has said so -- and weighs the
    same.
    """
    return "inferred" if getattr(rel, "is_unconfirmed", False) else "declared"


def _fk_cardinality(table: TableMetadata, column_name: str) -> str:
    """A foreign key is many-to-one unless it is also the table's whole key."""
    keys = [k.lower() for k in table.primary_keys]
    if keys == [column_name.lower()]:
        return "one_to_one"
    return "many_to_one"
