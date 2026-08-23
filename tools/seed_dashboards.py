#!/usr/bin/env python3
"""Build dashboards for every workspace, from each workspace's own catalog.

``seed_demo_data.py`` fills one workspace with hand-written SQL. That does not
generalise: this deployment has nine workspaces bound to nine different databases,
and a query written for Chinook says nothing about Pagila. Writing nine sets of
SQL by hand would also rot the moment a schema changes.

So this reads what each workspace actually exposes -- ``GET /schema``, which is
the catalog the model is shown, not the database -- classifies the columns, and
generates queries from the shapes it finds:

* a count, for any table;
* a ranking, for a table with a categorical column and something to measure;
* a trend, for a table with a date;
* a share, for a categorical column with few distinct values;
* a relationship, for a table with two numeric columns;
* a sample, so the raw rows are one click away.

Nothing is assumed to work. **Every generated query is executed before it is used**
and dropped if it fails, so a dashboard here cannot contain a tile that errors on
the screen -- which is the state the seeded dashboards were in, and the reason the
generator validates rather than trusting its own SQL.

Names come from the schema endpoint, so a workspace with a semantic layer gets its
*model* names and a workspace without one gets its tables. That distinction is not
cosmetic: naming the physical table behind a model is refused by the SQL policy.

    python tools/seed_dashboards.py --url http://localhost:3000 \\
        --email demo@example.com --password ... [--tenant chinook] [--clean]

Postgres SQL. Every workspace in this deployment is bound to Postgres; a MySQL or
SQL Server workspace would need its own date-truncation and cast syntax, and this
would need a dialect switch rather than a patch.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from seed_demo_data import Client, _why  # noqa: E402 - sibling script, not a package

#: Plain lower-case identifiers only. Anything else would have to be quoted, and
#: quoting interacts badly with the semantic compiler's name matching -- skipping
#: an exotic name costs one tile, guessing at its quoting costs a broken dashboard.
SAFE = re.compile(r"^[a-z_][a-z0-9_]*$")

NUMERIC = ("int", "numeric", "decimal", "real", "double", "float", "money", "serial")
TEMPORAL = ("date", "time", "timestamp")
TEXTUAL = ("char", "text", "string", "varchar", "uuid", "enum")

#: Columns that are identifiers rather than measurements. Summing a primary key
#: produces a number with no meaning, which is worse than no tile.
KEY_LIKE = re.compile(r"(^id$|_id$|_key$|^key$|code$|zip|postal)")


# ----------------------------------------------------------------------
# Reading the catalog
# ----------------------------------------------------------------------


def kind_of(column: Dict[str, Any]) -> str:
    data_type = str(column.get("data_type") or "").lower()
    name = str(column.get("name") or "").lower()
    if any(token in data_type for token in TEMPORAL):
        return "time"
    if any(token in data_type for token in NUMERIC):
        # A key is numeric to the database and categorical to a person.
        return "key" if (column.get("is_primary_key") or KEY_LIKE.search(name)) else "number"
    if any(token in data_type for token in TEXTUAL):
        return "key" if KEY_LIKE.search(name) else "text"
    return "other"


class Table:
    """One table or model, with its columns sorted into the shapes we can plot."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.name = str(payload.get("name") or "")
        self.schema = payload.get("schema") or ""
        self.rows = payload.get("row_count_estimate")
        columns = [c for c in (payload.get("columns") or []) if SAFE.match(str(c.get("name", "")))]
        self.columns = columns
        self.numbers = [c["name"] for c in columns if kind_of(c) == "number"]
        self.times = [c["name"] for c in columns if kind_of(c) == "time"]
        self.texts = [c["name"] for c in columns if kind_of(c) == "text"]
        # Profiled low-cardinality columns are the good ones to split by: the scan
        # already counted the distinct values, so this is knowledge, not a guess.
        self.small = [
            c["name"] for c in columns
            if c.get("categories") and 1 < len(c["categories"]) <= 12
        ]

    @property
    def usable(self) -> bool:
        return bool(self.name) and SAFE.match(self.name) is not None and bool(self.columns)

    @property
    def ref(self) -> str:
        """How to name it in a FROM clause."""
        if self.schema and SAFE.match(str(self.schema)):
            return f"{self.schema}.{self.name}"
        return self.name

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")


def read_catalog(client: Client) -> List[Table]:
    status, payload = client.call("GET", "/api/vanna/v2/schema")
    if status != 200 or not isinstance(payload, dict):
        return []
    tables = [Table(t) for t in payload.get("tables") or []]
    usable = [t for t in tables if t.usable]
    # Biggest first: a dashboard about the largest tables is the one somebody
    # would have built, and a lookup table of four rows makes a dull chart.
    usable.sort(key=lambda t: (-(t.rows or 0), t.name))
    return usable


# ----------------------------------------------------------------------
# Generating candidates
# ----------------------------------------------------------------------


class Candidate:
    def __init__(self, title: str, sql: str, kind: str, chart: Optional[dict] = None,
                 height: int = 5, width: int = 6) -> None:
        self.title = title
        self.sql = " ".join(sql.split())
        self.kind = kind
        self.chart = chart
        self.height = height
        self.width = width


def measure(table: Table) -> Tuple[str, str]:
    """What to plot on the y axis, and what to call it.

    A real measurement if the table has one, otherwise the row count -- "how many"
    is a legitimate question about any table, and a table with nothing to add up is
    the common case for a join table.
    """
    for name in table.numbers:
        return f"ROUND(SUM({name})::numeric, 2)", name
    return "COUNT(*)", "rows"


def candidates(table: Table) -> List[Candidate]:
    out: List[Candidate] = []
    ref, label = table.ref, table.label
    total, measure_name = measure(table)

    out.append(Candidate(
        f"{label.title()}", f"SELECT COUNT(*) AS rows FROM {ref}",
        kind="metric", width=3, height=3,
    ))

    split = (table.small or table.texts)[:1]
    if split:
        by = split[0]
        out.append(Candidate(
            f"{measure_name.replace('_', ' ').title()} by {by.replace('_', ' ')}",
            f"""SELECT {by} AS category, {total} AS {measure_name}
                FROM {ref} WHERE {by} IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 12""",
            kind="chart",
            chart={"type": "bar", "x": "category", "y": [measure_name]},
        ))
        if by in table.small:
            # A share only reads well with few slices, which is exactly what the
            # scan's low-cardinality profile tells us.
            out.append(Candidate(
                f"Share by {by.replace('_', ' ')}",
                f"""SELECT {by} AS category, COUNT(*) AS rows
                    FROM {ref} WHERE {by} IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC LIMIT 8""",
                kind="chart",
                chart={"type": "pie", "x": "category", "y": ["rows"]},
                width=5,
            ))

    if table.times:
        when = table.times[0]
        out.append(Candidate(
            f"{label.title()} over time",
            f"""SELECT TO_CHAR(DATE_TRUNC('month', {when}), 'YYYY-MM') AS month,
                       {total} AS {measure_name}
                FROM {ref} WHERE {when} IS NOT NULL
                GROUP BY 1 ORDER BY 1 LIMIT 60""",
            kind="chart",
            chart={"type": "line", "x": "month", "y": [measure_name]},
            width=7,
        ))
        out.append(Candidate(
            f"{label.title()} per month",
            f"""SELECT TO_CHAR(DATE_TRUNC('month', {when}), 'YYYY-MM') AS month,
                       COUNT(*) AS rows
                FROM {ref} WHERE {when} IS NOT NULL
                GROUP BY 1 ORDER BY 1 LIMIT 60""",
            kind="chart",
            chart={"type": "area", "x": "month", "y": ["rows"]},
            width=5,
        ))

    if len(table.numbers) >= 2:
        x, y = table.numbers[0], table.numbers[1]
        out.append(Candidate(
            f"{y.replace('_', ' ')} against {x.replace('_', ' ')}",
            f"""SELECT {x} AS {x}, {y} AS {y} FROM {ref}
                WHERE {x} IS NOT NULL AND {y} IS NOT NULL LIMIT 400""",
            kind="chart",
            chart={"type": "scatter", "x": x, "y": [y]},
        ))

    if split and table.numbers:
        by = split[0]
        pair = table.numbers[:2]
        out.append(Candidate(
            f"{label.title()} measures by {by.replace('_', ' ')}",
            f"""SELECT {by} AS category,
                       {", ".join(f"ROUND(SUM({n})::numeric, 2) AS {n}" for n in pair)}
                FROM {ref} WHERE {by} IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10""",
            kind="chart",
            chart={"type": "bar", "x": "category", "y": list(pair)},
        ))

    shown = [c["name"] for c in table.columns][:6]
    out.append(Candidate(
        f"{label.title()} sample",
        f"SELECT {', '.join(shown)} FROM {ref} LIMIT 25",
        kind="table", width=12,
    ))
    return out


# ----------------------------------------------------------------------
# Validating and laying out
# ----------------------------------------------------------------------


def runs(client: Client, sql: str) -> bool:
    """Execute it. A tile whose query fails is worse than a missing tile."""
    status, payload = client.call("POST", "/api/vanna/v2/run-sql", {"sql": sql})
    if status != 200:
        return False
    return bool(isinstance(payload, dict) and payload.get("rows"))


def lay_out(tiles: Sequence[Candidate]) -> List[Dict[str, Any]]:
    """Pack the accepted tiles into the 12-column grid, left to right."""
    out: List[Dict[str, Any]] = []
    x = y = 0
    row_height = 0
    for index, tile in enumerate(tiles):
        width = min(tile.width, 12)
        if x + width > 12:
            x, y = 0, y + (row_height or tile.height)
            row_height = 0
        body: Dict[str, Any] = {
            "id": f"t{index}",
            "kind": tile.kind,
            "title": tile.title,
            "query": {"source": "sql", "sql": tile.sql},
            "grid": {"x": x, "y": y, "width": width, "height": tile.height},
        }
        if tile.chart:
            body["chart"] = tile.chart
        out.append(body)
        x += width
        row_height = max(row_height, tile.height)
    return out


def build(client: Client, workspace: str, limit: int) -> List[Dict[str, Any]]:
    tables = read_catalog(client)
    if not tables:
        return []

    overview: List[Candidate] = []
    per_table: Dict[str, List[Candidate]] = {}

    for table in tables[:limit]:
        accepted = [c for c in candidates(table) if runs(client, c.sql)]
        if not accepted:
            continue
        per_table[table.name] = accepted
        overview.extend(c for c in accepted if c.kind == "metric")

    if not per_table:
        return []

    boards: List[Dict[str, Any]] = []

    # One overview of the whole workspace: the counts, then the best chart from
    # each table. "Best" is just the first accepted one -- they are generated in
    # descending order of how much they say.
    highlights = [
        next((c for c in accepted if c.kind == "chart"), None)
        for accepted in per_table.values()
    ]
    tiles = overview[:8] + [c for c in highlights if c is not None][:8]
    if tiles:
        boards.append({
            "title": f"{workspace.title()} overview",
            "description": "Generated from this workspace's catalog: how much of "
                           "everything there is, and the shape of each table.",
            "tiles": lay_out(tiles),
        })

    # Then one per table, for the tables that produced real charts.
    for name, accepted in per_table.items():
        charts = [c for c in accepted if c.kind == "chart"]
        if len(charts) < 2:
            continue
        boards.append({
            "title": f"{name.replace('_', ' ').title()}",
            "description": f"Everything the catalog supports plotting about {name}.",
            "tiles": lay_out(accepted),
        })
    return boards


# ----------------------------------------------------------------------
# Driving
# ----------------------------------------------------------------------


def workspaces(client: Client) -> List[str]:
    status, payload = client.call("GET", "/api/vanna/v2/me")
    if status != 200 or not isinstance(payload, dict):
        raise SystemExit(f"could not read memberships: {_why(payload)}")
    return sorted(payload.get("memberships") or [])


def clean(client: Client) -> int:
    status, payload = client.call("GET", "/api/vanna/v2/dashboards")
    if status != 200 or not isinstance(payload, dict):
        return 0
    removed = 0
    for board in payload.get("dashboards") or []:
        code, _ = client.call("DELETE", f"/api/vanna/v2/dashboards/{board['id']}")
        removed += 1 if code == 200 else 0
    return removed


def seed(base: str, email: str, password: str, tenants: List[str],
         wipe: bool, limit: int) -> int:
    failures = 0
    for tenant in tenants:
        client = Client(base, tenant)
        client.sign_in(email, password)
        print(f"\n=== {tenant}")
        if wipe:
            print(f"  removed {clean(client)} existing dashboard(s)")

        boards = build(client, tenant, limit)
        if not boards:
            print("  nothing to build -- no catalog, or nothing plottable in it")
            failures += 1
            continue

        made = charts = tiles = 0
        for board in boards:
            status, payload = client.call("POST", "/api/vanna/v2/dashboards", board)
            if status not in (200, 201):
                print(f"  refused '{board['title']}': {_why(payload)}")
                failures += 1
                continue
            made += 1
            tiles += len(board["tiles"])
            charts += sum(1 for t in board["tiles"] if t["kind"] == "chart")
            print(f"  built '{board['title']}' "
                  f"({len(board['tiles'])} tiles, "
                  f"{sum(1 for t in board['tiles'] if t['kind'] == 'chart')} charts)")
        print(f"  {made} dashboards, {tiles} tiles, {charts} charts")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--tenant", action="append",
                        help="Repeatable. Defaults to every workspace you belong to.")
    parser.add_argument("--clean", action="store_true",
                        help="Delete existing dashboards first.")
    parser.add_argument("--tables", type=int, default=6,
                        help="How many of the largest tables to build from.")
    args = parser.parse_args()

    tenants = args.tenant
    if not tenants:
        probe = Client(args.url)
        probe.sign_in(args.email, args.password)
        tenants = workspaces(probe)
        print(f"workspaces: {', '.join(tenants)}")

    failures = seed(args.url, args.email, args.password, tenants, args.clean, args.tables)
    print(f"\n{'ok' if not failures else str(failures) + ' problem(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
