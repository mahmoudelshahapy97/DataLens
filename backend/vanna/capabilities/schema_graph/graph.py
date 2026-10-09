"""The join graph: tables as nodes, joinable column pairs as weighted edges.

Everything here is pure and synchronous -- no catalog, no database. ``build.py``
turns catalog metadata into a :class:`SchemaGraph`; this module answers
questions about it:

* **shortest_path** -- cheapest chain of joins between two tables, bounded in
  hops so two unrelated subject areas sharing a lookup table never look
  "connected".
* **steiner_tree** -- one set of joins connecting *all* the tables a query
  needs. Joining each table to the first one separately (what ``suggest_joins``
  used to do) can route two targets through different bridges and emit a join
  graph with a cycle in it; a tree cannot.
* **fan_traps** -- tables a join tree fans out from in two one-to-many
  directions. Aggregating across such a join multiplies rows (the "chasm trap"),
  the most common way a query that runs returns a number that is too big.

Edge weights make the choice between equally short paths deliberate rather than
alphabetical: a declared foreign key beats an inferred one, and a many-to-many
hop costs extra because it is the hop most likely to duplicate rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: Hops allowed before a path is judged too tenuous to suggest. Four already
#: means three tables the user never mentioned; beyond that a "join path" is
#: more likely to be two unrelated subject areas sharing a lookup table than a
#: route anyone wants joined.
MAX_HOPS = 4

#: Base cost of an edge by where it came from.
SOURCE_WEIGHTS: Dict[str, float] = {
    "declared": 1.0,  # a foreign key or a curated relationship
    "inferred": 1.5,  # guessed from naming; plausible, unconfirmed
}

#: Added to an edge whose cardinality is many-to-many.
MANY_TO_MANY_PENALTY = 2.0

_INVERSE = {
    "many_to_one": "one_to_many",
    "one_to_many": "many_to_one",
    "one_to_one": "one_to_one",
    "many_to_many": "many_to_many",
}


@dataclass(frozen=True, order=True)
class JoinEdge:
    """One joinable column pair, read from ``left`` towards ``right``.

    ``cardinality`` is relative to that direction: ``many_to_one`` means many
    ``left`` rows match one ``right`` row -- ``left`` holds the foreign key.
    """

    left: str
    left_column: str
    right: str
    right_column: str
    cardinality: str = "many_to_one"
    source: str = "declared"

    @property
    def weight(self) -> float:
        cost = SOURCE_WEIGHTS.get(self.source, SOURCE_WEIGHTS["inferred"])
        if self.cardinality == "many_to_many":
            cost += MANY_TO_MANY_PENALTY
        return cost

    def reversed(self) -> "JoinEdge":
        return JoinEdge(
            left=self.right,
            left_column=self.right_column,
            right=self.left,
            right_column=self.left_column,
            cardinality=_INVERSE.get(self.cardinality, self.cardinality),
            source=self.source,
        )

    @property
    def fans_out(self) -> bool:
        """True when walking this edge can multiply ``left``'s rows."""
        return self.cardinality in ("one_to_many", "many_to_many")


@dataclass
class SteinerResult:
    """A join tree plus the requested tables it could not reach.

    ``edges`` are ordered so each one's ``left`` is already joined when it is
    reached -- render them top to bottom as ``JOIN right ON ...`` after
    ``FROM anchor``.
    """

    anchor: str
    edges: List[JoinEdge] = field(default_factory=list)
    unreachable: List[str] = field(default_factory=list)

    @property
    def tables(self) -> List[str]:
        seen = [self.anchor]
        for edge in self.edges:
            if edge.right not in seen:
                seen.append(edge.right)
        return seen

    def bridges(self, requested: Iterable[str]) -> List[str]:
        """Tables in the tree that nobody asked for."""
        wanted = set(requested)
        return sorted(t for t in self.tables if t not in wanted)


class SchemaGraph:
    """Undirected join graph over one data source's (visible) tables.

    Undirected because a join is symmetric; every edge is stored once per
    direction with its cardinality flipped, because reading only the stored
    direction is how a path from the "one" side to the "many" side goes missing.
    """

    def __init__(self, tables: Iterable[str] = ()) -> None:
        self._tables: Set[str] = set(tables)
        self._adjacency: Dict[str, Dict[Tuple[str, str, str], JoinEdge]] = {}

    # -- construction --------------------------------------------------

    def add_table(self, name: str) -> None:
        self._tables.add(name)

    def add_edge(self, edge: JoinEdge) -> None:
        """Add *edge* in both directions; the cheaper duplicate wins."""
        if edge.left == edge.right:
            return  # self-joins are real, but never part of a path between two tables
        for e in (edge, edge.reversed()):
            self._tables.update((e.left, e.right))
            bucket = self._adjacency.setdefault(e.left, {})
            key = (e.right, e.left_column.lower(), e.right_column.lower())
            current = bucket.get(key)
            if current is None or e.weight < current.weight:
                bucket[key] = e

    # -- inspection ----------------------------------------------------

    @property
    def tables(self) -> Set[str]:
        return set(self._tables)

    def __contains__(self, table: object) -> bool:
        return table in self._tables

    def __bool__(self) -> bool:
        return bool(self._adjacency)

    def edges_from(self, table: str) -> List[JoinEdge]:
        """Outgoing edges, sorted so identical graphs give identical answers."""
        return sorted(self._adjacency.get(table, {}).values())

    def neighbours(self, table: str) -> List[str]:
        return sorted({e.right for e in self.edges_from(table)})

    # -- paths ---------------------------------------------------------

    def shortest_path(
        self, start: str, goal: str, *, max_hops: int = MAX_HOPS
    ) -> Optional[List[JoinEdge]]:
        """Cheapest chain of at most *max_hops* joins from *start* to *goal*."""
        if start == goal:
            return []
        paths = self._bounded_paths({start}, max_hops)
        return paths.get(goal)

    def steiner_tree(
        self, terminals: Sequence[str], *, max_hops: int = MAX_HOPS
    ) -> SteinerResult:
        """Approximate minimum join tree spanning *terminals*.

        Greedy (Takahashi-Matsuyama): start from the first terminal and keep
        attaching whichever remaining terminal is cheapest to reach from *any*
        table already in the tree. Within twice the optimum, exact for the two
        and three table cases that dominate real queries, and fast on a few
        hundred tables. Each attachment is bounded by *max_hops* on its own.
        """
        ordered: List[str] = []
        for t in terminals:
            if t not in ordered:
                ordered.append(t)
        if not ordered:
            raise ValueError("steiner_tree needs at least one terminal")

        result = SteinerResult(anchor=ordered[0])
        in_tree: Set[str] = {ordered[0]}
        remaining = [t for t in ordered[1:]]

        while remaining:
            paths = self._bounded_paths(in_tree, max_hops)
            best: Optional[Tuple[float, int, int, str]] = None
            for position, target in enumerate(remaining):
                if target in in_tree:
                    best = (0.0, 0, position, target)
                    break
                path = paths.get(target)
                if path is None:
                    continue
                key = (sum(e.weight for e in path), len(path), position, target)
                if best is None or key < best:
                    best = key
            if best is None:
                result.unreachable.extend(remaining)
                break
            target = best[3]
            for edge in paths.get(target) or []:
                if edge.right not in in_tree:
                    result.edges.append(edge)
                    in_tree.add(edge.right)
            remaining.remove(target)

        return result

    def _bounded_paths(
        self, sources: Set[str], max_hops: int
    ) -> Dict[str, List[JoinEdge]]:
        """Cheapest path of <= *max_hops* edges from any source to every table.

        Hop-bounded Bellman-Ford: one relaxation round per hop. Plain Dijkstra
        would find the cheapest path regardless of length and then have to throw
        it away for being too long, missing a slightly dearer path that fits.
        Ties break on fewer hops, then on the sorted edge order, so the answer
        is deterministic.
        """
        best: Dict[str, Tuple[float, int, List[JoinEdge]]] = {
            s: (0.0, 0, []) for s in sorted(sources)
        }
        frontier = dict(best)
        for _ in range(max_hops):
            improved: Dict[str, Tuple[float, int, List[JoinEdge]]] = {}
            for node in sorted(frontier):
                cost, hops, path = frontier[node]
                for edge in self.edges_from(node):
                    candidate = (cost + edge.weight, hops + 1, path + [edge])
                    target = edge.right
                    current = improved.get(target) or best.get(target)
                    if current is None or candidate[:2] < current[:2]:
                        improved[target] = candidate
            improved = {
                t: v for t, v in improved.items()
                if t not in best or v[:2] < best[t][:2]
            }
            if not improved:
                break
            best.update(improved)
            frontier = improved
        return {t: v[2] for t, v in best.items() if t not in sources}


def fan_traps(edges: Sequence[JoinEdge]) -> List[Tuple[str, List[str]]]:
    """Tables a join tree fans out from in two or more directions.

    ``customer`` joined to both ``invoice`` and ``support_ticket`` pairs every
    invoice with every ticket of the same customer, so ``SUM(invoice.total)``
    comes back multiplied by the ticket count. One fan-out per table is fine --
    that is an ordinary detail join; two is the trap.

    Returns ``(hub, [tables it fans out to])`` sorted by hub.
    """
    outgoing: Dict[str, Set[str]] = {}
    for edge in edges:
        for e in (edge, edge.reversed()):
            if e.fans_out:
                outgoing.setdefault(e.left, set()).add(e.right)
    return sorted(
        (hub, sorted(targets)) for hub, targets in outgoing.items() if len(targets) >= 2
    )
