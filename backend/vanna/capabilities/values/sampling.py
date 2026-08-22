"""Deciding which columns deserve a value dictionary, and reading one.

Not every column does. A dictionary of every distinct customer name is a
disclosure with no benefit -- the model will never usefully match a term against
four million values, and sampling them means reading four million rows into a
store that gets shown in prompts. What earns a dictionary is a column that is
*enum-like*: a small closed set the user is going to name in words.

The test is deliberately conservative. A column that is skipped costs one class
of near-miss, which the fuzzy tier partly covers anyway; a column that is
sampled when it should not have been costs a privacy incident.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from .models import SampledValue

if TYPE_CHECKING:  # pragma: no cover
    from ...core.tool import ToolContext

logger = logging.getLogger("vanna.values.sampling")

#: Above this many distinct values a column is not a closed set, whatever its
#: type says.
MAX_DICTIONARY_VALUES = 200

#: A value longer than this is prose -- a description, an address, a note. It is
#: not something a user names in a question, and it is the shape most likely to
#: carry personal data.
MAX_VALUE_CHARS = 100

#: Types that can hold a closed set. Deliberately excludes free text on some
#: engines? No -- text is exactly where enum-like values live (`'ACTIVE'`), so
#: the cardinality check does that work instead.
_SAMPLEABLE_TYPES = re.compile(
    r"char|text|string|enum|varchar|nvarchar|citext|name|uuid|bool", re.IGNORECASE
)

#: Column names that should never be sampled, whatever their cardinality. A
#: two-employee company has a `salary` column with two distinct values, and it
#: still must not end up in a prompt.
_SENSITIVE_NAMES = re.compile(
    r"password|passwd|secret|token|api_?key|salt|hash|"
    r"ssn|social_security|tax_id|national_id|passport|"
    r"credit_card|card_number|cvv|iban|account_number|routing|"
    r"salary|compensation|dob|date_of_birth|"
    r"email|phone|mobile|address|postcode|zip|latitude|longitude",
    re.IGNORECASE,
)


def is_sampleable(column: Any, *, row_count: Optional[int] = None) -> bool:
    """Whether a column should get a value dictionary.

    Uses what the catalog already knows. A scanner that profiled cardinality has
    set ``low_cardinality`` and possibly ``categories``, and that answer is
    better than anything inferable from the type alone.
    """
    name = getattr(column, "name", "") or ""
    if _SENSITIVE_NAMES.search(name):
        return False
    if getattr(column, "is_generated", False):
        return False
    if getattr(column, "is_primary_key", False):
        # A key is unique by definition, so its dictionary is the table.
        return False

    categories = getattr(column, "categories", None)
    if categories is not None:
        return 0 < len(categories) <= MAX_DICTIONARY_VALUES
    if getattr(column, "low_cardinality", False):
        return True

    data_type = getattr(column, "data_type", "") or ""
    return bool(_SAMPLEABLE_TYPES.search(data_type))


def sampleable_columns(table: Any) -> List[Any]:
    """Every column of one table worth a dictionary."""
    return [
        column
        for column in (getattr(table, "columns", None) or [])
        if is_sampleable(column, row_count=getattr(table, "row_count_estimate", None))
    ]


def samples_from_catalog(
    tables: Sequence[Any],
    *,
    tenant_id: str = "default",
    data_source_id: str = "default",
) -> List[SampledValue]:
    """Turn a scanned catalog's recorded categories into pending samples.

    The cheapest possible sampling pass: the scanner already read these values
    while profiling cardinality, so this costs no query at all. They still
    arrive ``PENDING`` -- the scanner having seen a value is not the same as
    somebody agreeing it may be shown to a model.
    """
    samples: List[SampledValue] = []
    for table in tables or []:
        schema = getattr(table, "schema_name", None)
        name = getattr(table, "table_name", "")
        qualified = f"{schema}.{name}" if schema else name
        for column in sampleable_columns(table):
            for value in getattr(column, "categories", None) or ():
                text = str(value)
                if not text or len(text) > MAX_VALUE_CHARS:
                    continue
                samples.append(
                    SampledValue(
                        tenant_id=tenant_id,
                        data_source_id=data_source_id,
                        table=qualified,
                        column=column.name,
                        value=text,
                    )
                )
    return samples


async def sample_column(
    runner: Any,
    context: "ToolContext",
    *,
    table: str,
    column: str,
    tenant_id: str = "default",
    data_source_id: str = "default",
    limit: int = MAX_DICTIONARY_VALUES,
    dialect: Optional[str] = None,
) -> List[SampledValue]:
    """Read one column's distinct values from the source.

    Identifiers are quoted through sqlglot rather than interpolated: a column
    name cannot be a bind parameter in any dialect, so the only safe path is to
    render it as an identifier node. ``limit + 1`` so a column that is over the
    ceiling is recognised as over it rather than silently truncated to exactly
    the ceiling.
    """
    from sqlglot import exp

    from ..sql_runner import RunSqlToolArgs

    quoted_table = exp.to_table(table).sql(dialect=dialect, identify=True)
    quoted_column = exp.to_identifier(column, quoted=True).sql(dialect=dialect)
    sql = (
        f"SELECT DISTINCT {quoted_column} AS value FROM {quoted_table} "
        f"WHERE {quoted_column} IS NOT NULL LIMIT {int(limit) + 1}"
    )

    try:
        frame = await runner.run_sql(RunSqlToolArgs(sql=sql), context)
    except Exception as exc:
        # One unreadable column must not cost the whole sampling pass. A
        # permission this caller lacks is the common case, and it is not an error.
        logger.debug("Could not sample %s.%s: %s", table, column, exc)
        return []

    values = [str(v) for v in frame.iloc[:, 0].tolist()] if not frame.empty else []
    if len(values) > limit:
        # Over the ceiling means this is not a closed set. Recording a truncated
        # dictionary would be worse than recording none: the model would treat
        # a partial list as exhaustive and rule out values that exist.
        logger.debug(
            "%s.%s has more than %d distinct values; not a dictionary",
            table, column, limit,
        )
        return []

    return [
        SampledValue(
            tenant_id=tenant_id,
            data_source_id=data_source_id,
            table=table,
            column=column,
            value=value,
        )
        for value in values
        if value and len(value) <= MAX_VALUE_CHARS
    ]
