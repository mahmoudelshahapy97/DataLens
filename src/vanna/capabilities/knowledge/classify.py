"""Decide whether a query is worth remembering.

The feedback loop captures a candidate example whenever a user rates an answer
positively. Left unfiltered that fills the store with noise, because a large
share of positively-rated turns are people *looking around*:

    SELECT * FROM orders LIMIT 10

That is a perfectly good thing to ask and a worthless few-shot example. It
teaches no join, no filter convention, no business rule -- and because
retrieval returns a fixed top-k, every trivial example that scores well is one
genuinely useful example that does not get shown.

So the test is structural: does the query do any *work*? A bare projection with
no WHERE, GROUP BY, HAVING, aggregate, join, or CTE is a peek, not analysis.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def is_exploratory(sql: Optional[str], dialect: Optional[str] = None) -> bool:
    """True if *sql* is a bare look-around rather than an analytical query.

    Exploratory means a single SELECT with no WHERE, GROUP BY, HAVING, join,
    CTE, or aggregate. A LIMIT is neither required nor disqualifying -- plenty
    of real queries end in LIMIT, and plenty of peeks do not.

    Top-level clauses are inspected via the statement's own arguments rather
    than a tree-wide search, so a WHERE inside a subquery does not make the
    outer query look analytical. Aggregates are the exception: an aggregate
    anywhere means real computation is happening.

    Unparseable input returns False -- "not exploratory". This function gates
    *discarding* data, so the safe direction on uncertainty is to keep it.
    """
    if not isinstance(sql, str) or not sql.strip():
        return False

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:  # pragma: no cover - sqlglot is a core dependency
        return False

    try:
        statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s]
    except Exception:
        return False

    if len(statements) != 1:
        return False

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        return False

    # A CTE means the author structured the problem -- never exploratory.
    if statement.args.get("with") or statement.args.get("with_"):
        return False

    for clause in ("where", "group", "having", "qualify", "distinct"):
        if statement.args.get(clause) is not None:
            return False

    joins = statement.args.get("joins")
    if joins:
        return False

    # Any aggregate anywhere signals real computation.
    if statement.find(exp.AggFunc) is not None:
        return False

    # A window function is analysis even without a GROUP BY.
    if statement.find(exp.Window) is not None:
        return False

    return True


def is_worth_saving(
    question: Optional[str],
    sql: Optional[str],
    *,
    dialect: Optional[str] = None,
    min_question_words: int = 3,
) -> bool:
    """Whether a question/SQL pair should become a candidate example.

    Three filters, cheapest first:

    * the SQL must exist;
    * the question must be a real question -- "ok", "thanks", and "yes" get
      rated positively all the time and are not questions about data;
    * the query must do some work (see :func:`is_exploratory`).
    """
    if not sql or not sql.strip():
        return False
    if not question or len(question.split()) < min_question_words:
        return False
    if is_exploratory(sql, dialect):
        logger.debug("Skipping exploratory query as an example candidate")
        return False
    return True
