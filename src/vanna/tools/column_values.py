"""Check whether a value actually exists in a column before filtering on it.

The single most common text-to-SQL failure is not a syntax error -- it is a
query that runs perfectly and returns nothing, because the model guessed the
literal. It writes ``WHERE status = 'active'`` against a column storing
``'ACTIVE'``, or ``WHERE country = 'USA'`` where the data says
``'United States'``. Nothing errors. The user is told there are zero results,
which is a wrong answer wearing a correct answer's clothes.

Two defences, and they compose:

* **Scan time** -- ``SchemaScanner`` captures the real values of enum-like
  columns into the catalog, so they are already in the prompt. Free, but only
  covers low-cardinality columns.
* **Query time** -- this tool, for the high-cardinality ones. A customer name
  or product SKU can never be enumerated into a prompt, so the model has to be
  able to look it up.

Matching is deliberately fuzzy in two directions at once: a substring search in
the database (catching ``'united states'`` inside ``'United States of
America'``) and a similarity ranking in Python (catching typos and word-order
differences that SQL ``LIKE`` cannot).
"""

from __future__ import annotations

import difflib
import re
from typing import List, Optional, Type

from pydantic import BaseModel, Field

from vanna.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from vanna.components import (
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Conservative identifier pattern. Table and column names reach this tool as
#: model-generated strings and are interpolated into SQL -- they cannot be
#: bound as parameters, because a parameter marker is not valid in an
#: identifier position. So they are validated against this instead of escaped.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class ColumnValuesArgs(BaseModel):
    """Arguments for the check_column_values tool."""

    table: str = Field(description="Table name, optionally schema-qualified")
    column: str = Field(description="Column to search")
    value: str = Field(
        description="The value you intend to filter on. Real values resembling "
        "this one are returned."
    )


class CheckColumnValuesTool(Tool[ColumnValuesArgs]):
    """Finds real column values resembling a proposed filter value.

    Args:
        sql_runner: Executes the lookup. Its read-only enforcement and
            execution limits apply.
        max_results: Values returned to the model.
        scan_limit: Rows pulled for similarity ranking when the substring
            search finds nothing. Bounded because this runs a ``DISTINCT`` over
            a potentially large column.
        similarity_threshold: Minimum ratio for a fuzzy match, 0-1.
    """

    def __init__(
        self,
        sql_runner: SqlRunner,
        *,
        max_results: int = 20,
        scan_limit: int = 500,
        similarity_threshold: float = 0.4,
    ) -> None:
        self.sql_runner = sql_runner
        self.max_results = max_results
        self.scan_limit = scan_limit
        self.similarity_threshold = similarity_threshold

    @property
    def name(self) -> str:
        return "check_column_values"

    @property
    def description(self) -> str:
        return (
            "Check which real values exist in a column before filtering on it. "
            "Use this whenever you are about to write WHERE <text column> = "
            "'<some value>' and the column's exact values were not already "
            "shown to you. Returns the actual values that resemble yours."
        )

    def get_args_schema(self) -> Type[ColumnValuesArgs]:
        return ColumnValuesArgs

    async def execute(
        self, context: ToolContext, args: ColumnValuesArgs
    ) -> ToolResult:
        table, column = args.table.strip(), args.column.strip()

        # Identifiers are interpolated, so they must be validated. A parameter
        # placeholder cannot stand in for a table or column name in any SQL
        # dialect, which is why this is a whitelist rather than escaping.
        for label, identifier in (("table", table), ("column", column)):
            if not _IDENTIFIER_RE.match(identifier):
                message = (
                    f"{label.capitalize()} name {identifier!r} is not a valid "
                    "identifier."
                )
                return self._failure(message)

        try:
            matches = await self._substring_matches(context, table, column, args.value)
            source = "substring match"

            if not matches:
                matches = await self._fuzzy_matches(context, table, column, args.value)
                source = "closest values by similarity"
        except Exception as e:
            return self._failure(f"Could not read values from {table}.{column}: {e}")

        if not matches:
            text = (
                f"No values resembling {args.value!r} exist in {table}.{column}. "
                "Do not filter on that value -- it would return zero rows and "
                "look like a real (empty) answer. Either confirm the intended "
                "value with the user, or widen the query."
            )
            return ToolResult(
                success=True,
                result_for_llm=text,
                ui_component=UiComponent(
                    rich_component=RichTextComponent(content=text, markdown=False),
                    simple_component=SimpleTextComponent(text=text),
                ),
                metadata={"matches": [], "table": table, "column": column},
            )

        listed = "\n".join(f"  - {m!r}" for m in matches)
        exact = any(m == args.value for m in matches)
        guidance = (
            f"{args.value!r} exists exactly; use it as written."
            if exact
            else (
                f"{args.value!r} does NOT appear exactly. Use one of the values "
                "above verbatim, matching its capitalisation and spacing."
            )
        )
        text = (
            f"Values in {table}.{column} ({source}):\n{listed}\n\n{guidance}"
        )

        return ToolResult(
            success=True,
            result_for_llm=text,
            ui_component=UiComponent(
                rich_component=RichTextComponent(
                    content=f"Checked {len(matches)} values in {table}.{column}",
                    markdown=False,
                ),
                simple_component=SimpleTextComponent(
                    text=f"Checked {len(matches)} values in {table}.{column}"
                ),
            ),
            metadata={
                "matches": matches,
                "exact_match": exact,
                "table": table,
                "column": column,
            },
        )

    # ------------------------------------------------------------------
    # Lookup strategies
    # ------------------------------------------------------------------

    async def _substring_matches(
        self, context: ToolContext, table: str, column: str, value: str
    ) -> List[str]:
        """Case-insensitive containment search, done in the database.

        The needle is escaped for LIKE and embedded as a quoted literal.
        Single quotes are doubled -- the SQL standard escape -- and the value
        has already passed through the policy validator by the time a query
        built here runs.
        """
        needle = value.strip().lower().replace("'", "''")
        needle = needle.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        sql = (
            f"SELECT DISTINCT {column} AS v FROM {table} "
            f"WHERE LOWER(CAST({column} AS VARCHAR)) LIKE '%{needle}%' "
            f"LIMIT {self.max_results}"
        )
        rows = await self._rows(context, sql)
        return [str(r["v"]) for r in rows if r.get("v") is not None]

    async def _fuzzy_matches(
        self, context: ToolContext, table: str, column: str, value: str
    ) -> List[str]:
        """Rank a bounded sample by string similarity.

        Catches what LIKE cannot: transpositions, misspellings, and reordered
        words. Bounded by ``scan_limit`` so it stays cheap on a large column.
        """
        sql = (
            f"SELECT DISTINCT {column} AS v FROM {table} "
            f"WHERE {column} IS NOT NULL LIMIT {self.scan_limit}"
        )
        rows = await self._rows(context, sql)
        target = value.strip().lower()

        scored = []
        for row in rows:
            raw = row.get("v")
            if raw is None:
                continue
            candidate = str(raw)
            ratio = self._similarity(candidate, target)
            if ratio >= self.similarity_threshold:
                scored.append((ratio, candidate))

        scored.sort(key=lambda pair: -pair[0])
        return [value for _, value in scored[: self.max_results]]

    @staticmethod
    def _similarity(candidate: str, target: str) -> float:
        """Best score across whole-string, per-token, and acronym matching.

        Whole-string similarity alone is too blunt for the cases that matter.
        ``'USA'`` against ``'United States of America'`` scores about 0.15 --
        far below any usable threshold -- yet it is exactly the substitution a
        model makes and exactly the one worth catching. Three views fix that:

        * **whole string** -- catches typos ("Germny" / "Germany")
        * **per token** -- catches partial names ("States" / "United States")
        * **acronym** -- catches initialisms ("USA" / "United States of America")
        """
        text = candidate.strip().lower()
        best = difflib.SequenceMatcher(None, text, target).ratio()

        tokens = [t for t in re.split(r"[^a-z0-9]+", text) if t]
        for token in tokens:
            best = max(best, difflib.SequenceMatcher(None, token, target).ratio())

        # Acronym: first letters of each word, ignoring short joining words
        # ("of", "the") that are conventionally dropped from initialisms.
        if len(tokens) > 1:
            significant = [t for t in tokens if len(t) > 2]
            acronym = "".join(t[0] for t in significant)
            if acronym:
                best = max(
                    best, difflib.SequenceMatcher(None, acronym, target).ratio()
                )
        return best

    async def _rows(self, context: ToolContext, sql: str) -> List[dict]:
        df = await self.sql_runner.run_sql(RunSqlToolArgs(sql=sql), context)
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    @staticmethod
    def _failure(message: str) -> ToolResult:
        return ToolResult(
            success=False,
            result_for_llm=message,
            ui_component=UiComponent(
                rich_component=RichTextComponent(content=message, markdown=False),
                simple_component=SimpleTextComponent(text=message),
            ),
            error=message,
        )
