"""Interfaces for the curated knowledge stores."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional

from .models import (
    Example,
    ExampleHit,
    ExampleStatus,
    Instruction,
    InstructionScope,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

logger = logging.getLogger(__name__)


def extract_tables(sql: str, dialect: Optional[str] = None) -> List[str]:
    """Extract table names referenced by *sql*.

    CTE names are excluded -- they are query-local aliases, not real tables, and
    including them would pollute the table-boosting signal with names that do
    not exist in the catalog.

    Returns an empty list when the SQL will not parse; callers treat this as
    "unknown", not "no tables".
    """
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except ImportError:  # pragma: no cover - environment dependent
        return []

    try:
        ast = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []
    if ast is None:
        return []

    cte_names = {
        (cte.alias_or_name or "").lower()
        for cte in ast.find_all(exp.CTE)
        if cte.alias_or_name
    }

    tables = []
    for table in ast.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in cte_names:
            continue
        qualified = f"{table.db}.{name}" if table.db else name
        if qualified not in tables:
            tables.append(qualified)
    return tables


def validate_sql_syntax(sql: str, dialect: Optional[str] = None) -> Optional[str]:
    """Return an error message if *sql* will not parse, else None.

    Run at write time so a broken example can never enter the store. Vanna's
    existing ``save_question_tool_args`` writes whatever the model claims
    worked, with no verification -- which is how a store poisons its own
    retrieval.
    """
    try:
        import sqlglot
    except ImportError:  # pragma: no cover - environment dependent
        return None  # cannot validate; do not block

    try:
        parsed = sqlglot.parse(sql, dialect=dialect)
    except Exception as e:
        return f"SQL could not be parsed: {e}"
    if not parsed or all(s is None for s in parsed):
        return "SQL contains no executable statement"
    return None


class ExampleStore(ABC):
    """Stores verified question -> SQL pairs for few-shot retrieval.

    Implementations **must** scope every read and write to
    ``context.tenant_id``. An example carries table names, column names, and
    business logic; leaking one across tenants leaks all three.
    """

    @abstractmethod
    async def add(
        self,
        context: "ToolContext",
        question: str,
        sql: str,
        *,
        status: ExampleStatus = ExampleStatus.CANDIDATE,
        data_source_id: str = "default",
        tags: Optional[List[str]] = None,
        dialect: Optional[str] = None,
    ) -> Example:
        """Store an example. Implementations should validate the SQL first."""

    @abstractmethod
    async def search(
        self,
        context: "ToolContext",
        question: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
        verified_only: bool = False,
        data_source_id: Optional[str] = None,
    ) -> List[ExampleHit]:
        """Return examples relevant to *question*, most relevant first."""

    @abstractmethod
    async def list_all(
        self,
        context: "ToolContext",
        *,
        status: Optional[ExampleStatus] = None,
        data_source_id: Optional[str] = None,
    ) -> List[Example]:
        """List stored examples, for review interfaces."""

    @abstractmethod
    async def set_status(
        self,
        context: "ToolContext",
        example_id: str,
        status: ExampleStatus,
        *,
        actor: Optional[str] = None,
    ) -> bool:
        """Promote or reject an example. Returns False when not found."""

    @abstractmethod
    async def delete(self, context: "ToolContext", example_id: str) -> bool:
        """Delete an example. Returns False when not found."""


class InstructionStore(ABC):
    """Stores durable business rules resolved by scope, not similarity."""

    @abstractmethod
    async def add(
        self,
        context: "ToolContext",
        instruction: Instruction,
    ) -> Instruction:
        """Store an instruction, stamped with the caller's tenant."""

    @abstractmethod
    async def resolve(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        tables: Optional[List[str]] = None,
    ) -> List[Instruction]:
        """Return every instruction applying to this request, highest priority first.

        Note this takes no query text: applicability is decided by scope, which
        is the entire point.
        """

    @abstractmethod
    async def list_all(self, context: "ToolContext") -> List[Instruction]:
        """List all of the tenant's instructions, including disabled ones."""

    @abstractmethod
    async def set_enabled(
        self, context: "ToolContext", instruction_id: str, enabled: bool
    ) -> bool:
        """Enable or disable an instruction. Returns False when not found."""

    @abstractmethod
    async def delete(self, context: "ToolContext", instruction_id: str) -> bool:
        """Delete an instruction. Returns False when not found."""

    async def update(
        self,
        context: "ToolContext",
        instruction_id: str,
        *,
        text: Optional[str] = None,
        scope: Optional[InstructionScope] = None,
        scope_ref: Optional[str] = None,
        priority: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Instruction]:
        """Patch an instruction in place. Returns None when not found.

        Every argument is optional so a caller can change one field without
        restating the rest -- a whole-object PUT lets a stale client silently
        revert somebody else's edit to a field it never displayed.

        Concrete rather than abstract on purpose. ``InstructionStore`` is public
        API with implementations outside this repository, and a new
        ``@abstractmethod`` would stop every one of them constructing. A store
        that cannot patch says so here, and the route turns it into a 501.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot update instructions in place"
        )
