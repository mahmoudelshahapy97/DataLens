"""Putting resolved values in front of the model, before it writes the filter.

The alternative is a tool call: the model guesses `'laptop'`, calls something to
check, learns the value is `'LAPTOP'`, and tries again. That works and costs a
round trip every time -- and only happens when the model already suspects it is
wrong, which is exactly when it usually does not.

Resolution is cheap enough to do unconditionally. Four of the five tiers are
string comparisons over a dictionary already in memory, so the hints go in the
prompt whether or not the model would have asked. The tool stays as the explicit
escape hatch for a value the question never named.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, Sequence

from ...core.enhancer import LlmContextEnhancer, system_prompt_context
from .matcher import DEFAULT_CONFIDENCE_THRESHOLD, describe_matches, resolve_question

if TYPE_CHECKING:  # pragma: no cover
    from ...core.user import User

logger = logging.getLogger("vanna.values.enhancer")


class ValueResolvingEnhancer(LlmContextEnhancer):
    """Appends the real spelling of values a question named.

    ``store`` supplies approved dictionaries; ``catalog`` is the fallback for a
    deployment with no curation yet, since a scanned catalog already records
    low-cardinality values. ``inner`` is the enhancer that would otherwise have
    been used and runs first, so schema context comes before value hints --
    the hints only make sense once the columns are on the page.
    """

    def __init__(
        self,
        *,
        store: Any = None,
        catalog: Any = None,
        data_source_id: Optional[str] = None,
        semantic_resolver: Any = None,
        inner: Optional[LlmContextEnhancer] = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        max_matches: int = 20,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.data_source_id = data_source_id
        self.semantic_resolver = semantic_resolver
        self.inner = inner
        self.confidence_threshold = confidence_threshold
        self.max_matches = max_matches

    async def enhance_system_prompt(
        self, system_prompt: str, user_message: str, user: "User"
    ) -> str:
        if self.inner is not None:
            system_prompt = await self.inner.enhance_system_prompt(
                system_prompt, user_message, user
            )

        try:
            dictionaries = await self._dictionaries(user, user_message)
            if not dictionaries:
                return system_prompt
            matches = await resolve_question(
                user_message,
                dictionaries,
                resolver=self.semantic_resolver,
                confidence_threshold=self.confidence_threshold,
                max_matches=self.max_matches,
            )
        except Exception as exc:
            # A prompt without value hints still produces an answer; a raised
            # exception produces nothing. This is an accuracy aid, not a control.
            logger.debug("Value resolution skipped: %s", exc)
            return system_prompt

        if not matches:
            return system_prompt
        return f"{system_prompt}\n\n## Values\n\n{describe_matches(matches)}"

    async def enhance_user_message(
        self, message: str, user: "User", **kwargs: Any
    ) -> str:
        if self.inner is not None and hasattr(self.inner, "enhance_user_message"):
            return await self.inner.enhance_user_message(message, user, **kwargs)
        return message

    def __getattr__(self, name: str) -> Any:
        """Pass anything else through to the wrapped enhancer."""
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    # -- dictionaries --------------------------------------------------

    async def _dictionaries(self, user: "User", question: str) -> Sequence:
        """Approved dictionaries, or the catalog's own categories as a fallback.

        The store wins when it has anything: a curated dictionary reflects a
        decision, while the catalog's categories are whatever the scanner
        happened to see.
        """
        context = system_prompt_context(user)

        if self.store is not None:
            tables = await self._table_names(context, question)
            if tables:
                dictionaries = await self.store.dictionaries_for_tables(
                    context, tables=tables, data_source_id=self.data_source_id
                )
                if dictionaries:
                    return dictionaries

        if self.catalog is None:
            return []

        from .models import ColumnValues
        from .sampling import sampleable_columns

        tables = await self.catalog.get_tables(
            context, data_source_id=self.data_source_id
        )
        dictionaries = []
        for table in tables or []:
            schema = getattr(table, "schema_name", None)
            name = getattr(table, "table_name", "")
            qualified = f"{schema}.{name}" if schema else name
            for column in sampleable_columns(table):
                if getattr(column, "categories", None):
                    dictionaries.append(ColumnValues.from_column(qualified, column))
        return dictionaries

    async def _table_names(self, context: Any, question: str) -> list:
        if self.catalog is None:
            return []
        try:
            tables = await self.catalog.get_tables(
                context, data_source_id=self.data_source_id
            )
        except Exception:
            return []
        names = []
        for table in tables or []:
            schema = getattr(table, "schema_name", None)
            name = getattr(table, "table_name", "")
            names.append(f"{schema}.{name}" if schema else name)
        return names
