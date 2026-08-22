"""The value dictionary store: sampled, reviewed, then usable.

Sampling proposes and a person approves. That split is the whole reason this is
a store rather than a cache. A distinct value read out of a production column may
be a customer's name or an internal code nobody meant to publish, and putting it
in a prompt is a disclosure decision -- so new values arrive ``PENDING`` and are
invisible to :meth:`dictionary_for` until somebody says otherwise. Turning
sampling on cannot, by itself, start sending column contents to a model.

The same shape as :class:`vanna.capabilities.knowledge.ExampleStore`, and for the
same reason: both hold material that reaches a prompt and therefore both need a
human in the path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional, Sequence

from .models import ColumnValues, ReviewStatus, SampledValue, ValueSynonym

if TYPE_CHECKING:  # pragma: no cover
    from ...core.tool import ToolContext


class ValueStore(ABC):
    """Sampled column values and their tenant-authored synonyms.

    Implementations must filter every read by ``context.tenant_id`` and stamp
    every write with it. A value dictionary is tenant data twice over -- it is
    read from their database and it is shown in their prompts.
    """

    @abstractmethod
    async def record_samples(
        self,
        context: "ToolContext",
        values: Sequence[SampledValue],
    ) -> int:
        """Store newly observed values, leaving existing reviews alone.

        Returns how many were new. Re-sampling a column must not reset a
        decision somebody already made about a value still present -- otherwise
        every scan silently un-approves the dictionary.
        """

    @abstractmethod
    async def list_samples(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        table: Optional[str] = None,
        column: Optional[str] = None,
        status: Optional[ReviewStatus] = None,
        limit: int = 500,
    ) -> List[SampledValue]:
        """Sampled values, for a review screen or for assembling a dictionary."""

    @abstractmethod
    async def set_status(
        self,
        context: "ToolContext",
        *,
        table: str,
        column: str,
        values: Sequence[str],
        status: ReviewStatus,
        actor: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ) -> int:
        """Approve or reject values. Returns how many changed."""

    @abstractmethod
    async def set_synonym(
        self, context: "ToolContext", synonym: ValueSynonym
    ) -> None:
        """Declare terms equivalent to a stored value, or label a code."""

    @abstractmethod
    async def list_synonyms(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        table: Optional[str] = None,
    ) -> List[ValueSynonym]:
        """Tenant-authored equivalences."""

    async def dictionary_for(
        self,
        context: "ToolContext",
        *,
        table: str,
        column: str,
        data_source_id: Optional[str] = None,
    ) -> ColumnValues:
        """One column's **approved** dictionary, ready to match against.

        Provided rather than abstract: assembling approved samples and synonyms
        into a :class:`ColumnValues` is the same work for every backend, and
        doing it once here is what guarantees no implementation accidentally
        includes a pending value.
        """
        approved = await self.list_samples(
            context,
            data_source_id=data_source_id,
            table=table,
            column=column,
            status=ReviewStatus.APPROVED,
        )
        synonyms = await self.list_synonyms(
            context, data_source_id=data_source_id, table=table
        )
        return assemble_dictionary(
            f"{table}.{column}",
            approved,
            [s for s in synonyms if s.column.casefold() == column.casefold()],
        )

    async def dictionaries_for_tables(
        self,
        context: "ToolContext",
        *,
        tables: Sequence[str],
        data_source_id: Optional[str] = None,
    ) -> List[ColumnValues]:
        """Every approved dictionary across the given tables, in one pass.

        The retrieval path needs all of them at once, and asking column by
        column turns one query into fifty.
        """
        wanted = {t.casefold() for t in tables}
        samples = [
            s
            for s in await self.list_samples(
                context, data_source_id=data_source_id, status=ReviewStatus.APPROVED
            )
            if s.table.casefold() in wanted
        ]
        synonyms = [
            s
            for s in await self.list_synonyms(context, data_source_id=data_source_id)
            if s.table.casefold() in wanted
        ]

        grouped: dict = {}
        for sample in samples:
            grouped.setdefault(sample.key, []).append(sample)

        dictionaries = []
        for key, values in grouped.items():
            dictionaries.append(
                assemble_dictionary(
                    key, values, [s for s in synonyms if s.key == key]
                )
            )
        return dictionaries


def assemble_dictionary(
    qualified_name: str,
    samples: Sequence[SampledValue],
    synonyms: Sequence[ValueSynonym],
) -> ColumnValues:
    """Fold approved samples and synonyms into a matchable dictionary.

    A synonym for a value that is not approved is dropped rather than promoting
    it: declaring a nickname for something must not be a way to publish it.
    """
    values = tuple(dict.fromkeys(s.value for s in samples))
    known = {v.casefold() for v in values}

    declared: dict = {}
    labels: dict = {}
    for synonym in synonyms:
        if synonym.value.casefold() not in known:
            continue
        if synonym.terms:
            declared[synonym.value] = tuple(
                dict.fromkeys(declared.get(synonym.value, ()) + tuple(synonym.terms))
            )
        if synonym.label:
            labels[synonym.value] = synonym.label

    return ColumnValues(
        qualified_name=qualified_name,
        values=values,
        synonyms=declared,
        labels=labels,
    )
