"""AST-based SQL policy validation.

Validation operates on a parsed syntax tree, never on the query text. That
distinction is the entire point: text-level checks (keyword scans, regexes) are
defeated by comments, casing, whitespace, unicode look-alikes, string literals
that happen to contain keywords, and nesting. A tree cannot be fooled by
formatting, and a column legitimately named ``merge`` is a column, not a
``MERGE`` statement.

Two properties are load-bearing:

**Fail closed.** A statement that will not parse is rejected, not waved
through. If we cannot analyse it, we cannot vouch for it.

**Never echo the offending expression.** Violation messages name the function
or table and stop there. Function arguments are exactly where file paths, URLs,
and connection strings live, so quoting the expression back into an error
message -- which is then logged, and often shown to the user -- would leak the
target the check exists to protect.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Iterable, List, Optional, Set, Tuple

from .data_readers import (
    DATA_READER_FUNCTIONS,
    GENERATOR_FUNCTIONS,
    ROW_EXPANSION_FUNCTIONS,
)
from .models import PolicyViolation, SqlPolicy, SqlPolicyError, ViolationCode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlglot import expressions as exp


class SqlParseUnavailable(RuntimeError):
    """Raised when sqlglot is not installed.

    Deliberately fatal rather than degrading to "allow everything": silently
    disabling the safety layer because a dependency is missing is precisely the
    failure mode that produces an incident nobody can explain afterwards.
    """


def _sqlglot():  # type: ignore[no-untyped-def]
    try:
        import sqlglot

        return sqlglot
    except ImportError as e:  # pragma: no cover - environment dependent
        raise SqlParseUnavailable(
            "sqlglot is required for SQL policy validation. "
            "Install it with: pip install sqlglot"
        ) from e


# Dialects probed when canonicalising a function name. sqlglot may map the same
# name onto different AST classes per dialect -- `version()` becomes
# exp.CurrentVersion in postgres/mysql/duckdb but stays exp.Anonymous in
# oracle/tsql -- so matching on the written name alone catches only some of
# them. Probing collects every class key a name can land on.
_PROBE_DIALECTS: Tuple[Optional[str], ...] = (
    None,
    "postgres",
    "mysql",
    "tsql",
    "oracle",
    "bigquery",
    "snowflake",
    "clickhouse",
    "trino",
    "duckdb",
)


@functools.lru_cache(maxsize=256)
def _canonical_names(names: frozenset) -> frozenset:
    """Expand function names to also cover sqlglot's canonical class keys.

    ``read_csv`` may parse to ``exp.ReadCSV`` (class key ``readcsv``) on duckdb
    but remain ``exp.Anonymous(name="read_csv")`` elsewhere. Matching on both
    the written name and the class key means one name list works across every
    dialect. Results are cached because probing 10 dialects x 3 arities per
    name is far too slow to repeat per query.
    """
    sqlglot = _sqlglot()
    from sqlglot import expressions as exp

    expanded: Set[str] = {n.lower() for n in names}
    for name in list(expanded):
        for dialect in _PROBE_DIALECTS:
            # Probe several arities: some functions only resolve to their
            # concrete subclass when given arguments (duckdb `read_csv()` with
            # no args fails to parse at all).
            for probe in (f"SELECT {name}()", f"SELECT {name}('x')",
                          f"SELECT {name}(1, 2)"):
                try:
                    ast = sqlglot.parse_one(probe, dialect=dialect)
                except Exception:
                    continue
                if ast is None:
                    continue
                first = next(ast.find_all(exp.Func), None)
                if first is not None and not isinstance(first, exp.Anonymous):
                    expanded.add(type(first).key.lower())
    return frozenset(expanded)


def _func_keys(func: "exp.Func") -> Tuple[str, str]:
    """Return ``(written_name, class_key)`` for matching a function call.

    Anonymous functions carry the name the user wrote; concrete subclasses
    carry a stable class key. Checking both against a canonicalised set means
    one list matches regardless of how the dialect parsed the call.
    """
    return (func.name or "").lower(), type(func).key.lower()


def _safe_func_label(func: "exp.Func") -> str:
    """A function label safe to put in an error message and a log line.

    For ``exp.Anonymous`` the written name is authoritative. For concrete
    subclasses ``func.name`` may return the *first argument* rather than the
    function name -- ``exp.ReadCSV.name`` is the file path -- so the class key
    is used instead. Getting this wrong leaks the path into the error.
    """
    from sqlglot import expressions as exp

    if isinstance(func, exp.Anonymous):
        return (func.name or "unknown").lower()
    return type(func).key.lower()


def resolve_table_name(name: str, quoted: bool, known: Iterable[str]) -> Optional[str]:
    """Resolve a SQL identifier to a catalog table name, or None.

    Follows SQL identifier semantics rather than doing a naive comparison: a
    quoted identifier must match case-sensitively, while an unquoted one prefers
    an exact match but falls back to a case-insensitive scan. Skipping this makes
    strict mode reject ``SELECT * FROM Orders`` against a catalog holding
    ``orders``, which reads as a bug to every user who hits it.
    """
    known_set = known if isinstance(known, (set, frozenset)) else set(known)
    if name in known_set:
        return name
    if quoted:
        return None
    lowered = name.lower()
    for candidate in known_set:
        if candidate.lower() == lowered:
            return candidate
    return None


class SqlPolicyValidator:
    """Validates SQL against a :class:`SqlPolicy`.

    Stateless and cheap to construct; parsing dominates at roughly 1-10 ms per
    query, which is noise next to an LLM round trip.

        validator = SqlPolicyValidator()
        violations = validator.validate(sql, dialect="postgres", policy=policy)
        if violations:
            ...  # reject
    """

    def validate(
        self,
        sql: str,
        *,
        dialect: Optional[str] = None,
        policy: Optional[SqlPolicy] = None,
        catalog_tables: Optional[Iterable[str]] = None,
    ) -> List[PolicyViolation]:
        """Return every violation found. Empty list means the query is allowed.

        All checks run rather than stopping at the first failure, so a caller
        can show the model everything wrong in one repair round trip instead of
        peeling problems off one at a time.

        Args:
            sql: The statement to validate.
            dialect: sqlglot dialect name. Improves parse fidelity; when None,
                sqlglot's generic dialect is used.
            policy: Rules to apply. Defaults to the strict read-only policy.
            catalog_tables: Known table names, required by
                ``require_catalog_tables``.
        """
        policy = policy or SqlPolicy()
        sqlglot = _sqlglot()
        from sqlglot import expressions as exp

        # -- Parse (fail closed) ------------------------------------------
        try:
            statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s]
        except Exception as e:
            return [
                PolicyViolation(
                    code=ViolationCode.UNPARSEABLE,
                    message=(
                        "The query could not be parsed and was rejected. "
                        "Check the SQL syntax for this dialect."
                    ),
                    detail=type(e).__name__,
                )
            ]

        if not statements:
            return [
                PolicyViolation(
                    code=ViolationCode.UNPARSEABLE,
                    message="No executable statement was found in the query.",
                )
            ]

        violations: List[PolicyViolation] = []

        # -- Stacked statements -------------------------------------------
        # `SELECT 1; DROP TABLE users` -- reject regardless of policy mode.
        # Even under read_write, one call should carry one statement.
        if len(statements) > 1:
            violations.append(
                PolicyViolation(
                    code=ViolationCode.MULTIPLE_STATEMENTS,
                    message=(
                        f"The query contains {len(statements)} statements. "
                        "Only one statement may be executed per call."
                    ),
                )
            )

        for ast in statements:
            violations.extend(self._check_statement_kind(ast, policy))
            violations.extend(self._check_functions(ast, policy))
            violations.extend(self._check_sources(ast, policy))
            if policy.require_catalog_tables and catalog_tables is not None:
                violations.extend(self._check_tables(ast, catalog_tables))
            if policy.max_joins is not None:
                join_count = len(list(ast.find_all(exp.Join)))
                if join_count > policy.max_joins:
                    violations.append(
                        PolicyViolation(
                            code=ViolationCode.TOO_MANY_JOINS,
                            message=(
                                f"The query uses {join_count} joins, exceeding "
                                f"the limit of {policy.max_joins}."
                            ),
                        )
                    )

        return violations

    def validate_or_raise(
        self,
        sql: str,
        *,
        dialect: Optional[str] = None,
        policy: Optional[SqlPolicy] = None,
        catalog_tables: Optional[Iterable[str]] = None,
    ) -> None:
        """Like :meth:`validate` but raises :class:`SqlPolicyError`."""
        violations = self.validate(
            sql, dialect=dialect, policy=policy, catalog_tables=catalog_tables
        )
        if violations:
            raise SqlPolicyError(violations)

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_statement_kind(
        self, ast: "exp.Expression", policy: SqlPolicy
    ) -> List[PolicyViolation]:
        """Enforce the allowed statement types at the AST root.

        This is what makes a read-only policy actually read-only, and it is
        strictly stronger than scanning for dangerous keywords: the check is on
        the root node's *type*, so it cannot be confused by a keyword appearing
        in a string literal, a comment, or an identifier.
        """
        from sqlglot import expressions as exp

        if not policy.allowed_statements:
            return []  # empty set == no restriction (permissive policy)

        # Unwrap the wrappers a SELECT may legitimately arrive inside.
        node = ast
        while isinstance(node, (exp.Subquery, exp.Paren)):
            inner = node.this
            if inner is None:
                break
            node = inner

        kind_map = {
            exp.Select: "SELECT",
            exp.Union: "UNION",
            exp.Except: "EXCEPT",
            exp.Intersect: "INTERSECT",
            exp.Insert: "INSERT",
            exp.Update: "UPDATE",
            exp.Delete: "DELETE",
            exp.Drop: "DROP",
            exp.Create: "CREATE",
            exp.Alter: "ALTER",
            exp.Merge: "MERGE",
            exp.Command: "COMMAND",
        }

        # A `WITH ... SELECT` parses as the inner statement carrying a `with`
        # arg, so classify by the statement and treat WITH as an allowed
        # wrapper when the policy permits it.
        kind: Optional[str] = None
        for node_type, label in kind_map.items():
            if isinstance(node, node_type):
                kind = label
                break

        if kind is None:
            # An unrecognised root is not something we can vouch for.
            kind = type(node).key.upper()

        allowed = {s.upper() for s in policy.allowed_statements}
        if kind in allowed:
            return []

        # `exp.Command` is sqlglot's catch-all for statements it does not model
        # (SET, GRANT, CALL, VACUUM, ...). Under read-only these are all
        # rejected, which is the intent.
        return [
            PolicyViolation(
                code=ViolationCode.STATEMENT_NOT_ALLOWED,
                message=(
                    f"{kind} statements are not permitted by the current policy. "
                    f"Allowed: {', '.join(sorted(allowed))}."
                ),
                detail=kind,
            )
        ]

    def _check_functions(
        self, ast: "exp.Expression", policy: SqlPolicy
    ) -> List[PolicyViolation]:
        """Reject denied functions and data readers in every AST position.

        Walking the whole tree -- rather than only the FROM/JOIN slot -- is what
        closes the projection, subquery, and nested-argument bypasses.
        """
        from sqlglot import expressions as exp

        violations: List[PolicyViolation] = []
        denied = policy.effective_denied_functions
        if not denied:
            return violations

        canonical_denied = _canonical_names(frozenset(denied))
        canonical_readers = (
            _canonical_names(DATA_READER_FUNCTIONS)
            if policy.block_data_readers
            else frozenset()
        )

        seen: Set[str] = set()
        for func in ast.find_all(exp.Func):
            raw, key = _func_keys(func)
            if raw not in canonical_denied and key not in canonical_denied:
                continue

            label = _safe_func_label(func)
            if label in seen:
                continue
            seen.add(label)

            is_reader = raw in canonical_readers or key in canonical_readers
            if is_reader:
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.DATA_READER_BLOCKED,
                        message=(
                            f"The function '{label}' is not permitted. Reading "
                            "files, URLs, or external databases from SQL is "
                            "blocked in all query positions."
                        ),
                        detail=label,
                    )
                )
            else:
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.FUNCTION_DENIED,
                        message=f"The function '{label}' is on the denied list.",
                        detail=label,
                    )
                )
        return violations

    def _check_sources(
        self, ast: "exp.Expression", policy: SqlPolicy
    ) -> List[PolicyViolation]:
        """Reject table-valued functions used as query sources.

        Sources reached via JOIN and via LATERAL both need unwrapping -- a TVF
        smuggled in through ``CROSS JOIN UNNEST(...)`` or ``LATERAL FLATTEN(...)``
        produces a different node shape than one in a plain FROM, and checking
        only FROM misses both.
        """
        from sqlglot import expressions as exp

        violations: List[PolicyViolation] = []
        allowed_generators = (
            _canonical_names(frozenset(policy.allowed_source_functions))
            if policy.allowed_source_functions
            else frozenset()
        )
        canonical_generators = _canonical_names(GENERATOR_FUNCTIONS)
        canonical_expansions = _canonical_names(ROW_EXPANSION_FUNCTIONS)

        for clause in ast.find_all(exp.From, exp.Join, exp.Lateral):
            source = clause.this
            # Peel the wrappers a source can arrive inside. A table-valued
            # function in FROM parses as an *unnamed* exp.Table wrapping the
            # call (`generate_series(1,10)` -> Table(name='',
            # this=ExplodingGenerateSeries)), including when it carries an
            # alias, so an exp.Func check alone never fires. Unwrapping only a
            # name-less Table is important: a real table reference must keep
            # flowing to the catalog check instead of being inspected here.
            for _ in range(4):
                if isinstance(source, exp.Alias):
                    source = source.this
                elif isinstance(source, exp.Lateral):
                    source = source.this
                elif isinstance(source, exp.Table) and not source.name:
                    inner = source.this
                    if inner is None:
                        break
                    source = inner
                elif isinstance(source, exp.Subquery) and isinstance(
                    source.this, exp.Func
                ):
                    source = source.this
                else:
                    break

            if not isinstance(source, exp.Func):
                continue

            raw, key = _func_keys(source)

            # Row expansion over an in-scope column reads nothing external.
            if raw in canonical_expansions or key in canonical_expansions:
                continue

            # Generators: blocked by default (unbounded range == DoS), opt-in.
            is_generator = raw in canonical_generators or key in canonical_generators
            if is_generator and (
                raw in allowed_generators or key in allowed_generators
            ):
                continue

            label = _safe_func_label(source)
            violations.append(
                PolicyViolation(
                    code=ViolationCode.SOURCE_FUNCTION_NOT_ALLOWED,
                    message=(
                        f"The function '{label}' may not be used as a query "
                        "source. Queries must read from tables."
                    ),
                    detail=label,
                )
            )
        return violations

    def _check_tables(
        self, ast: "exp.Expression", catalog_tables: Iterable[str]
    ) -> List[PolicyViolation]:
        """Strict mode: every table must resolve to the catalog.

        CTE names defined in the query are legitimate references and must be
        excluded, or every ``WITH`` query would be rejected.
        """
        from sqlglot import expressions as exp

        known = set(catalog_tables)
        violations: List[PolicyViolation] = []

        cte_names = {
            (cte.alias_or_name or "").lower()
            for cte in ast.find_all(exp.CTE)
            if cte.alias_or_name
        }

        seen: Set[str] = set()
        for table in ast.find_all(exp.Table):
            name = table.name
            if not name or name.lower() in cte_names or name in seen:
                continue
            seen.add(name)

            quoted = (
                bool(table.this.quoted)
                if isinstance(table.this, exp.Identifier)
                else False
            )
            if resolve_table_name(name, quoted, known) is not None:
                continue

            violations.append(
                PolicyViolation(
                    code=ViolationCode.TABLE_NOT_IN_CATALOG,
                    message=(
                        f"The table '{name}' is not present in the schema "
                        "catalog and cannot be queried."
                    ),
                    detail=name,
                )
            )
        return violations


# ----------------------------------------------------------------------
# Limit injection (used by the runner at execution time)
# ----------------------------------------------------------------------


def apply_row_limit(sql: str, limit: int, dialect: Optional[str] = None) -> str:
    """Return *sql* with a row limit applied, preserving any tighter existing one.

    Parses and regenerates rather than appending text, so the limit lands in the
    right place for the dialect and cannot corrupt a query that already has a
    ``LIMIT``/``OFFSET``. An existing limit *smaller* than *limit* is left alone
    -- the caller asked for at most N rows, and a query asking for fewer already
    satisfies that.

    Returns the input unchanged when it cannot be parsed or is not a SELECT;
    callers should have rejected such statements via the validator already.
    """
    sqlglot = _sqlglot()
    from sqlglot import expressions as exp

    try:
        ast = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return sql
    if ast is None:
        return sql

    if not isinstance(ast, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return sql

    existing = ast.args.get("limit")
    if existing is not None:
        try:
            current = int(existing.expression.this)
            if current <= limit:
                return sql  # already at least as tight
        except (AttributeError, TypeError, ValueError):
            # Non-literal limit (a parameter or expression) -- leave it alone
            # rather than risk changing the query's meaning.
            return sql

    try:
        return ast.limit(limit).sql(dialect=dialect)
    except Exception:
        return sql


def has_row_limit(sql: str, dialect: Optional[str] = None) -> bool:
    """True if *sql* already carries a LIMIT clause."""
    sqlglot = _sqlglot()

    try:
        ast = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return False
    return ast is not None and ast.args.get("limit") is not None
