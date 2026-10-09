"""Schema graph: the catalog's tables and joins as a weighted graph.

The database schema already *is* a knowledge graph -- tables are nodes, foreign
keys are edges -- and it is exact, so it is read from the catalog rather than
extracted by a model. This package answers the questions that need the graph
as a whole: how to connect several tables in one query, and where a join will
multiply rows.

    from vanna.capabilities.schema_graph import load_schema_graph

    graph, tables = await load_schema_graph(catalog, context)
    tree = graph.steiner_tree(["chinook.invoice_line", "chinook.artist"])
    tree.bridges(["chinook.invoice_line", "chinook.artist"])
    # ['chinook.album', 'chinook.track']
"""

from .build import build_schema_graph, load_schema_graph, table_lookup
from .graph import (
    MAX_HOPS,
    JoinEdge,
    SchemaGraph,
    SteinerResult,
    fan_traps,
)
from .knowledge import SchemaKnowledge, match_phrases, tables_in_sql

__all__ = [
    "MAX_HOPS",
    "JoinEdge",
    "SchemaGraph",
    "SchemaKnowledge",
    "SteinerResult",
    "build_schema_graph",
    "fan_traps",
    "load_schema_graph",
    "match_phrases",
    "table_lookup",
    "tables_in_sql",
]
