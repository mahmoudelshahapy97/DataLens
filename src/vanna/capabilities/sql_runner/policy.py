"""Execution-time limits for SQL queries.

Distinct from ``vanna.core.sql_policy``, which decides whether a query is
*allowed to run at all*. This module bounds what a query is allowed to *cost*
once it has been permitted: how many rows it may return, how long it may run,
and how much memory its result may occupy.

Both layers are needed. A perfectly legitimate ``SELECT * FROM events`` passes
every safety check and will still exhaust memory against a billion-row table.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ExecutionPolicy(BaseModel):
    """Resource limits applied to a single query execution.

    Defaults are sized for an interactive analytics chat: enough rows to build
    a chart, few enough to keep a runaway query from taking the process down.

    Example::

        # Interactive default
        ExecutionPolicy()

        # A reporting tool that legitimately needs bigger extracts
        ExecutionPolicy(max_rows=50_000, timeout_seconds=300)
    """

    max_rows: int = Field(
        default=1000,
        gt=0,
        description="Rows returned to the caller. The runner fetches one extra "
        "row to detect truncation, then discards it.",
    )

    hard_max_rows: int = Field(
        default=10_000,
        gt=0,
        description="Ceiling a caller-supplied max_rows is clamped to, so a "
        "per-request override cannot escalate into an unbounded fetch.",
    )

    timeout_seconds: int = Field(
        default=60,
        gt=0,
        description="Wall-clock limit for one execution. Applied client-side "
        "always, and server-side too on engines that support it.",
    )

    max_result_bytes: int = Field(
        default=50 * 1024 * 1024,
        gt=0,
        description="Approximate in-memory ceiling for a result frame. Guards "
        "against few-but-enormous rows (blobs, long text) that slip past a row "
        "cap.",
    )

    apply_server_side_timeout: bool = Field(
        default=True,
        description="Also set the engine's native statement timeout. A "
        "client-side timeout abandons the client but leaves the warehouse "
        "burning CPU, so prefer server-side where available.",
    )

    def effective_max_rows(self, requested: Optional[int] = None) -> int:
        """Resolve the row cap for a request, clamped to ``hard_max_rows``."""
        if requested is None or requested <= 0:
            return min(self.max_rows, self.hard_max_rows)
        return min(requested, self.hard_max_rows)


class ExecutionResult(BaseModel):
    """Metadata describing how an execution went.

    Kept separate from the DataFrame so callers can report truncation honestly.
    Silently returning a truncated frame is how a model ends up confidently
    summarising the first 1,000 rows of a 4-million-row table as if it were the
    whole answer.
    """

    row_count: int
    truncated: bool = False
    execution_ms: float = 0.0
    timed_out: bool = False

    model_config = {"extra": "allow"}
