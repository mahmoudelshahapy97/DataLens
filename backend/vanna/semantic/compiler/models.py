"""What the compiler returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class CompileWarning:
    """Something compiled, but the answer may not mean what the reader thinks.

    Warnings are returned rather than logged because the only person who can
    judge them is the one reading the number. A fan-out warning that reaches a
    log and not the answer is a warning nobody sees.
    """

    code: str
    message: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.message}{(' ' + self.detail) if self.detail else ''}"


@dataclass
class CompiledSql:
    """Dialect SQL, plus what the compiler had to do to produce it."""

    sql: str
    dialect: str
    referenced_models: List[str] = field(default_factory=list)
    referenced_views: List[str] = field(default_factory=list)
    applied_row_rules: List[str] = field(default_factory=list)
    """Names of the row-level rules injected. Surfaced so an admin previewing a
    query can see that access control actually fired, rather than inferring it
    from the absence of rows."""
    dropped_columns: List[str] = field(default_factory=list)
    """Columns hidden by a column-level rule for this caller."""
    warnings: List[CompileWarning] = field(default_factory=list)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def warning_text(self) -> str:
        return "\n".join(str(w) for w in self.warnings)
