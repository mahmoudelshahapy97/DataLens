"""Which tables feed which saved queries, dashboards and reports.

## Derived, not recorded

The obvious design -- and the one QueryLite ships -- is a `lineage_edges` table
written by a recorder called from wherever SQL is saved. Its recorder is never
called from anywhere, so the table is always empty and the graph always renders
nothing. That is not an unlucky bug; it is the failure mode the design invites.
A recorder is a second write that has to be remembered at every call site, and
forgetting it produces silence rather than an error.

So there is no table here. The graph is computed from rows the system already
keeps for their own reasons:

    saved_queries.sql            the SQL, verbatim
    dashboards.document          tile queries: inline SQL, saved-query refs, cubes
    report_schedules.dashboard_id  which dashboards are on a schedule
    generations.retrieved_table_names  which tables answered real questions

It cannot go stale, because there is nothing to keep in step. Deleting a saved
query removes it from the graph by virtue of the row being gone. The cost is that
the graph is recomputed per request rather than read from an index -- acceptable
at the scale these tables reach, and the point at which it stops being acceptable
is a point at which a materialised view is the answer, not a recorder.

## Extraction

`sqlglot` is already a pinned dependency and is dialect-aware, so it is what
parses the SQL. Regex extraction -- again, what QueryLite does -- gets CTEs
wrong (it reports the CTE's *name* as a table), gets quoted identifiers wrong,
and reports `orders` from the string literal `'orders'`. Each of those puts a
table in the graph that is not there, which is worse than a missing edge: an
impact analysis that lists tables nobody uses is one nobody trusts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .db import SCHEMA

logger = logging.getLogger("vanna.lineage")


def tables_in(sql: str, dialect: Optional[str] = None) -> Set[str]:
    """Physical tables a statement reads, casefolded.

    CTE names are excluded: `WITH recent AS (SELECT ... FROM orders) SELECT * FROM
    recent` reads `orders`, and reporting `recent` as a table would put a name in
    the graph that exists nowhere in the warehouse.

    A statement that will not parse yields nothing rather than raising. Lineage is
    a reporting feature; one unparseable saved query must not take the whole graph
    down with it.
    """
    if not sql or not sql.strip():
        return set()

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - sqlglot is pinned in requirements
        logger.warning("sqlglot is not installed; lineage is unavailable.")
        return set()

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - any parse failure means "no edges"
        logger.debug("Could not parse for lineage: %s", exc)
        return set()

    found: Set[str] = set()
    for statement in statements:
        if statement is None:
            continue

        # Every name introduced by a WITH, at any depth. Collected first so the
        # table walk below can skip them.
        cte_names = {
            cte.alias_or_name.casefold()
            for cte in statement.find_all(exp.CTE)
            if cte.alias_or_name
        }

        for table in statement.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            key = name.casefold()
            if key in cte_names:
                continue
            # Qualified where the SQL qualified it, so `sales.orders` and
            # `public.orders` are not silently merged into one node.
            parts = [p for p in (table.db, table.name) if p]
            found.add(".".join(parts).casefold())

    return found


# ----------------------------------------------------------------------
# The graph
# ----------------------------------------------------------------------


class Lineage:
    """Builds the table -> asset graph for one workspace."""

    def __init__(self, db: Any, directory: Any) -> None:
        self.db = db
        self.directory = directory

    async def graph(
        self, tenant_id: str, *, dialect: Optional[str] = None
    ) -> Dict[str, Any]:
        """Nodes and edges, ready to draw.

        Node ids are prefixed by kind (``table:orders``, ``saved:abc``) because a
        saved query and a table can share a name and a graph that merges them
        draws an edge that does not exist.
        """
        saved = await self.directory.list_saved(tenant_id)
        dashboards = await self.directory.list_dashboards(tenant_id)
        schedules = await self._schedules(tenant_id)

        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, str]] = []

        def node(kind: str, key: str, label: str) -> str:
            node_id = f"{kind}:{key}"
            nodes.setdefault(node_id, {"id": node_id, "kind": kind, "label": label})
            return node_id

        def edge(source: str, target: str, relation: str) -> None:
            edges.append({"source": source, "target": target, "relation": relation})

        # --- saved queries -------------------------------------------
        saved_tables: Dict[str, Set[str]] = {}
        for row in saved or []:
            saved_id = str(row["id"])
            target = node("saved", saved_id, row.get("title") or "Saved query")
            found = tables_in(row.get("sql") or "", dialect)
            saved_tables[saved_id] = found
            for table in found:
                edge(node("table", table, table), target, "read_by")

        # --- dashboards ----------------------------------------------
        for row in dashboards or []:
            document = row.get("document") or {}
            dashboard_id = str(row["id"])
            dash_node = node("dashboard", dashboard_id, document.get("title") or row.get("title") or "Dashboard")

            for tile in document.get("tiles") or []:
                query = tile.get("query") or {}
                source = query.get("source")

                if source == "saved":
                    saved_id = str(query.get("saved_query_id") or "")
                    if not saved_id:
                        continue
                    # Through the saved query, not around it: that is the whole
                    # reason a SavedQueryRef is the preferred tile shape -- fixing
                    # the SQL in one place fixes every dashboard that uses it, and
                    # the graph should show that dependency.
                    edge(node("saved", saved_id, saved_id), dash_node, "shown_on")
                elif source == "sql":
                    for table in tables_in(query.get("sql") or "", dialect):
                        edge(node("table", table, table), dash_node, "read_by")
                elif source == "cube":
                    cube = str(query.get("cube") or "")
                    if cube:
                        edge(node("cube", cube, cube), dash_node, "shown_on")

        # --- reports --------------------------------------------------
        for row in schedules:
            report_node = node("report", str(row["id"]), row.get("name") or "Report")
            edge(node("dashboard", str(row["dashboard_id"]), str(row["dashboard_id"])),
                 report_node, "scheduled_as")

        # Labels for nodes that were only ever referenced, never defined -- a
        # saved query a dashboard points at that has since been deleted. Left in
        # the graph rather than dropped: a dangling reference is exactly what
        # somebody looking at lineage wants to find.
        for node_id, data in nodes.items():
            if data["kind"] == "saved" and data["label"] == node_id.split(":", 1)[1]:
                data["label"] = "(deleted saved query)"
                data["missing"] = True

        return {
            "nodes": sorted(nodes.values(), key=lambda n: (n["kind"], n["label"])),
            "edges": edges,
            "counts": {
                "tables": sum(1 for n in nodes.values() if n["kind"] == "table"),
                "saved": sum(1 for n in nodes.values() if n["kind"] == "saved"),
                "dashboards": sum(1 for n in nodes.values() if n["kind"] == "dashboard"),
                "reports": sum(1 for n in nodes.values() if n["kind"] == "report"),
            },
        }

    async def impact(
        self, tenant_id: str, table: str, *, dialect: Optional[str] = None
    ) -> Dict[str, Any]:
        """What breaks if this table changes.

        Transitive: a table feeds a saved query, which is shown on a dashboard,
        which is on a schedule. Stopping at the first hop answers a question
        nobody asked -- the useful answer is "these four reports go out wrong on
        Monday".
        """
        wanted = table.casefold()
        graph = await self.graph(tenant_id, dialect=dialect)

        by_source: Dict[str, List[Dict[str, str]]] = {}
        for edge in graph["edges"]:
            by_source.setdefault(edge["source"], []).append(edge)

        labels = {n["id"]: n for n in graph["nodes"]}
        start = f"table:{wanted}"

        if start not in labels:
            # Try an unqualified match: somebody types `orders`, the graph holds
            # `sales.orders`. Reported as a list rather than guessing one.
            candidates = [
                n["id"] for n in graph["nodes"]
                if n["kind"] == "table" and n["label"].rsplit(".", 1)[-1] == wanted
            ]
            if len(candidates) != 1:
                return {
                    "table": table,
                    "found": False,
                    "candidates": [labels[c]["label"] for c in candidates],
                    "affected": [],
                }
            start = candidates[0]

        seen: Set[str] = set()
        order: List[str] = []
        queue = [start]
        while queue:
            current = queue.pop(0)
            for edge in by_source.get(current, []):
                target = edge["target"]
                if target in seen:
                    continue
                seen.add(target)
                order.append(target)
                queue.append(target)

        return {
            "table": labels[start]["label"],
            "found": True,
            "candidates": [],
            "affected": [
                {
                    "id": node_id,
                    "kind": labels[node_id]["kind"],
                    "label": labels[node_id]["label"],
                }
                for node_id in order
                if node_id in labels
            ],
        }

    async def _schedules(self, tenant_id: str) -> List[Dict[str, Any]]:
        """Report schedules, if the table is there.

        Tolerant of its absence so lineage works on a deployment that has not run
        migration 0015 yet -- the graph is simply one layer shorter.
        """
        try:
            rows = await self.db.fetch_all(
                f"SELECT id, name, dashboard_id FROM {SCHEMA}.report_schedules "
                "WHERE tenant_id = %s",
                (tenant_id,),
            )
            return list(rows or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("No report schedules for lineage: %s", type(exc).__name__)
            return []
