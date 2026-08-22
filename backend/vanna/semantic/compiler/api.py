"""The compiler's public entry point."""

from __future__ import annotations

from typing import Optional

from ..models import Manifest
from .models import CompiledSql
from .rewriter import SemanticRewriter


def compile_sql(
    sql: str,
    manifest: Manifest,
    *,
    dialect: str = "",
    fanout_guard: str = "warn",
) -> CompiledSql:
    """Compile semantic SQL to dialect SQL.

    Args:
        sql: A single statement, written against model and view names.
        manifest: What those names mean.
        dialect: sqlglot dialect for both parsing and rendering.
        fanout_guard: ``warn`` (default) attaches a warning when an aggregate
            crosses a one-to-many join; ``reject`` refuses; ``allow`` says
            nothing. There is no option that silently emits a wrong number.

    Example::

        compiled = compile_sql(
            "SELECT customer.region, SUM(amount) FROM orders GROUP BY 1",
            manifest, dialect="postgres",
        )
        compiled.sql          # WITH orders AS (...) SELECT ...
        compiled.warnings     # fan-out, if any
    """
    return SemanticRewriter(
        manifest, dialect=dialect, fanout_guard=fanout_guard
    ).compile(sql)
