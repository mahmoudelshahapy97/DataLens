"""Compiling semantic SQL to dialect SQL, with sqlglot.

    from vanna.semantic.compiler import compile_sql

    compiled = compile_sql("SELECT SUM(amount) FROM orders", manifest,
                           dialect="postgres")
"""

from .api import compile_sql
from .grain import GRANULARITIES, supported_dialects, truncate
from .models import CompiledSql, CompileWarning
from .rewriter import SemanticRewriter
from .traversal import TraversalPlan, alias_for_path, projected_name

__all__ = [
    "compile_sql",
    "SemanticRewriter",
    "CompiledSql",
    "CompileWarning",
    "TraversalPlan",
    "alias_for_path",
    "projected_name",
    "truncate",
    "GRANULARITIES",
    "supported_dialects",
]
