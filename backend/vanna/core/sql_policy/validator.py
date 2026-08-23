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
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

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


#: sqlglot marks a row-generating function used as a FROM source by parsing it
#: into a distinct class -- `generate_series(1, 10)` in a FROM becomes
#: `ExplodingGenerateSeries`. "Exploding" describes what sqlglot does with the
#: node, not anything the author typed, so it is stripped from the label.
_INTERNAL_LABEL_PREFIX = "exploding_"


def _safe_func_label(func: "exp.Func") -> str:
    """A function label safe to put in an error message and a log line.

    For ``exp.Anonymous`` the written name is authoritative. For concrete
    subclasses ``func.name`` may return the *first argument* rather than the
    function name -- ``exp.ReadCSV.name`` is the file path -- so the name must
    come from the class instead. Getting this wrong leaks the path into the error.

    Which part of the class matters. ``type(func).key`` is the class name run
    together, so a rejection told the user:

        The function 'explodinggenerateseries' may not be used as a query source.

    There is no such function. Nobody can grep for it, look it up, or connect it
    to the ``generate_series`` they wrote, and the message is the only thing they
    get -- the query is refused. ``sql_names()`` is sqlglot's own answer to "what
    is this called in SQL", so it is what gets printed.
    """
    from sqlglot import expressions as exp

    if isinstance(func, exp.Anonymous):
        return (func.name or "unknown").lower()

    names = getattr(type(func), "sql_names", None)
    try:
        label = (names() or [""])[0].lower() if names else ""
    except Exception:  # a dialect class with an unusual sql_names
        label = ""

    if not label:
        return type(func).key.lower()

    if label.startswith(_INTERNAL_LABEL_PREFIX):
        label = label[len(_INTERNAL_LABEL_PREFIX):]
    return label


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
        catalog_columns: Optional[Mapping[str, Mapping[str, AbstractSet[str]]]] = None,
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
            catalog_columns: ``{table: {column: {"read", "filter", "aggregate"}}}``
                -- what the caller may do with each column. Required by
                ``require_catalog_columns``. Must be built from the *same*
                filtered catalog the prompt was built from; see
                :class:`SqlPolicyToolRegistry`.
        """
        policy = policy or SqlPolicy()
        sqlglot = _sqlglot()
        from sqlglot import expressions as exp

        # -- Parse (fail closed) ------------------------------------------
        # A bare `Semicolon` node is punctuation, not a statement. sqlglot emits
        # one when anything follows the final terminator -- `SELECT 1;\n-- note`
        # parses as [Select, Semicolon], and an LLM ending its query with a
        # semicolon and a comment is entirely ordinary SQL. Counting that node
        # rejected the query twice over: once as "contains 2 statements", once as
        # "SEMICOLON statements are not permitted", neither of which describes
        # anything the author did wrong.
        try:
            statements = [
                s
                for s in sqlglot.parse(sql, dialect=dialect)
                if s and not isinstance(s, exp.Semicolon)
            ]
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
            if policy.require_catalog_columns and catalog_columns is not None:
                violations.extend(
                    self._check_columns(ast, catalog_columns, dialect)
                )
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
        catalog_columns: Optional[Mapping[str, Mapping[str, AbstractSet[str]]]] = None,
    ) -> None:
        """Like :meth:`validate` but raises :class:`SqlPolicyError`."""
        violations = self.validate(
            sql,
            dialect=dialect,
            policy=policy,
            catalog_tables=catalog_tables,
            catalog_columns=catalog_columns,
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

    # ------------------------------------------------------------------
    # Column authority
    # ------------------------------------------------------------------

    #: Positions where a column acts as a predicate. Filtering leaks a column one
    #: comparison at a time even when its value is never displayed, which is why
    #: this is a permission separate from reading it.
    #:
    #: ``Order`` is here rather than under projection because ordering by a column
    #: ranks every row by it, and ``Qualify`` because it is a WHERE for window
    #: results.
    _FILTER_CONTEXTS = ("Where", "Having", "Join", "Order", "Qualify")

    #: Positions where a column is consumed in bulk rather than row by row.
    #:
    #: ``Distinct`` and ``Window`` are not decoration: ``SELECT DISTINCT ON (note)``
    #: and ``OVER (PARTITION BY note)`` both partition by a column without naming it
    #: in an aggregate function, and a set holding only ``AggFunc`` and ``Group``
    #: lets each of them through.
    _AGGREGATE_CONTEXTS = ("AggFunc", "Group", "Distinct", "Window")

    @staticmethod
    def _qualify_schema(
        catalog_columns: Mapping[str, Mapping[str, AbstractSet[str]]],
    ) -> dict:
        """Shape the catalog the way sqlglot's qualifier wants it.

        A dotted catalog key is a nesting, not a name: ``sales.orders`` has to
        become ``{"sales": {"orders": {...}}}``, or the qualifier looks for a table
        literally called ``sales.orders`` and resolves nothing.
        """
        keys = [str(k) for k in catalog_columns]

        # sqlglot infers one depth for the whole mapping, so `{"chinook": {"artist":
        # ...}, "artist": ...}` resolves nothing at all -- not the qualified entry,
        # not the bare one. A caller supplying both spellings of the same table
        # (which is the natural thing to do, and what the table check wants) would
        # silently disable column enforcement's ability to resolve anything, and
        # every query would be rejected as unresolvable.
        #
        # Qualified wins: it is the more specific statement of where the table is.
        qualified_tails = {k.rsplit(".", 1)[-1].lower() for k in keys if "." in k}

        schema: dict = {}
        for table_key in keys:
            parts = [part for part in table_key.split(".") if part]
            if not parts:
                continue
            if len(parts) == 1 and parts[0].lower() in qualified_tails:
                continue
            node = schema
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = dict.fromkeys(catalog_columns[table_key], "UNKNOWN")
        return schema

    #: Clauses that may refer to a SELECT-list alias by name. Postgres resolves an
    #: ORDER BY / GROUP BY name against the output list before the table's columns,
    #: and HAVING/QUALIFY follow the projection for the same reason.
    _ALIAS_CONTEXTS = ("Order", "Having", "Qualify", "Group")

    @classmethod
    def _is_output_alias(cls, column: "exp.Column") -> bool:
        """Whether this bare name is one of its own query's output aliases.

        Scoped deliberately: the alias has to be defined by the very SELECT whose
        ORDER BY (or GROUP BY, HAVING, QUALIFY) the reference sits in. A name that
        merely happens to match an alias somewhere else in the statement is still
        an unattributed column, and still refused.
        """
        from sqlglot import expressions as exp

        name = (column.name or "").lower()
        if not name:
            return False

        node = column.parent
        clause = None
        while node is not None:
            if type(node).__name__ in cls._ALIAS_CONTEXTS:
                clause = node
                break
            if isinstance(node, exp.Select):
                # Reached the query without passing through one of those clauses,
                # so the reference is in the projection or a predicate, where a
                # bare name means a column.
                return False
            node = node.parent

        if clause is None:
            return False

        select = clause.parent
        if not isinstance(select, exp.Select):
            return False

        return any(
            (projection.alias_or_name or "").lower() == name
            for projection in select.expressions
        )

    def _check_columns(
        self,
        ast: "exp.Expression",
        catalog_columns: Mapping[str, Mapping[str, AbstractSet[str]]],
        dialect: Optional[str],
    ) -> List[PolicyViolation]:
        """Every column reference must be permitted for the way it is used.

        This is what stops a column permission being advisory. The prompt already
        omits a column the caller may not read, but "the model was not told about
        it" is not a control: a model can name a column it inferred, a repair pass
        can reintroduce one, and ``/run-sql`` takes SQL straight from a person.

        The work is done by sqlglot's qualifier, which is the only reason this is
        tractable. It expands ``SELECT *`` into real columns -- so a star never has
        to be refused outright -- resolves aliases to their base tables, and reaches
        through CTEs and subqueries to the physical column underneath.

        Fail closed twice: a query the qualifier cannot resolve is rejected rather
        than skipped, and so is a column whose table cannot be determined. An
        unresolvable reference is not evidence that it was harmless.
        """
        from sqlglot import expressions as exp
        from sqlglot.optimizer.qualify import qualify

        try:
            # On a copy: the caller's AST is used elsewhere, and qualification
            # rewrites it substantially.
            qualified = qualify(
                ast.copy(),
                schema=self._qualify_schema(catalog_columns),
                dialect=dialect,
            )
        except Exception as exc:
            return [
                PolicyViolation(
                    code=ViolationCode.COLUMN_UNRESOLVED,
                    message=(
                        "The query references a column that could not be resolved "
                        "against the tables available to you."
                    ),
                    detail=type(exc).__name__,
                )
            ]

        # A CTE name is a legitimate source that is not a catalog table. The columns
        # selected *inside* the CTE are checked against the real table there, so
        # skipping references to the CTE itself skips no permission.
        cte_names = {
            (cte.alias_or_name or "").lower()
            for cte in qualified.find_all(exp.CTE)
            if cte.alias_or_name
        }

        aliases: dict = {}
        for table in qualified.find_all(exp.Table):
            key = f"{table.db}.{table.name}" if table.db else table.name
            aliases[(table.alias or table.name).lower()] = key.lower()

        lookup = {
            str(table).lower(): {
                str(column).lower(): set(uses) for column, uses in columns.items()
            }
            for table, columns in catalog_columns.items()
        }

        # A query may name a table unqualified even when the catalog key carries a
        # schema, and the qualifier does not always supply the missing part. Without
        # this, `SELECT salary FROM orders` resolved to no catalog entry, fell
        # through the "the table check will report it" branch below, and executed --
        # a bypass of the whole check, reachable by deleting one word.
        #
        # An ambiguous bare name maps to nothing rather than to a guess: two schemas
        # can each hold an `orders`, and picking one would apply the wrong table's
        # permissions. Same rule as EffectiveGrants.table().
        bare: dict = {}
        for key in list(lookup):
            tail = key.rsplit(".", 1)[-1]
            if tail == key:
                continue
            bare[tail] = None if tail in bare else key
        for tail, key in bare.items():
            if key is not None and tail not in lookup:
                lookup[tail] = lookup[key]

        violations: List[PolicyViolation] = []
        seen: Set[Tuple[str, str, str]] = set()

        for column in qualified.find_all(exp.Column):
            source = (column.table or "").lower()
            if source in cte_names:
                continue

            # `ORDER BY revenue` names the output of `sum(...) AS revenue`, not a
            # column of any table, and the qualifier leaves it exactly as written.
            # Treating that as an unattributable column refused the single most
            # common analytical shape there is -- "top N by something" -- on every
            # workspace, which is what every dashboard tile with a ranking in it
            # was hitting. Skipping it skips no permission, for the same reason the
            # CTE skip above does not: the expression it names is in the SELECT
            # list, where its own columns are checked.
            if not source and self._is_output_alias(column):
                continue

            if not source:
                # Nothing to check it against. Must not be waved through: an
                # attribution the qualifier could not make is exactly the case an
                # attacker would aim for.
                violations.append(
                    PolicyViolation(
                        code=ViolationCode.COLUMN_UNRESOLVED,
                        message=(
                            f"The column '{column.name}' could not be attributed to "
                            "a table, so its permissions could not be checked."
                        ),
                        detail=column.name,
                    )
                )
                continue

            table_key = aliases.get(source, source)
            if table_key in cte_names:
                continue

            columns = lookup.get(table_key)
            if columns is None:
                # Fail closed. This used to `continue`, on the reasoning that
                # _check_tables would report the unknown table -- but that check
                # accepts a bare name whenever the catalog lists one, so a table it
                # waved through and this one could not resolve left the column
                # unchecked and the query allowed.
                self._note(
                    violations,
                    seen,
                    (table_key, "", "resolve"),
                    ViolationCode.COLUMN_UNRESOLVED,
                    f"Columns of '{table_key}' could not be resolved, so their "
                    "permissions could not be checked.",
                    table_key,
                )
                continue

            name = column.name.lower()
            granted = columns.get(name)
            if granted is None:
                self._note(
                    violations,
                    seen,
                    (table_key, name, "exists"),
                    ViolationCode.COLUMN_NOT_IN_CATALOG,
                    f"The column '{column.name}' is not available on "
                    f"'{table_key}' and cannot be queried.",
                    f"{table_key}.{name}",
                )
                continue

            for use, code, phrasing in self._required_uses(column):
                if use in granted:
                    continue
                self._note(
                    violations,
                    seen,
                    (table_key, name, use),
                    code,
                    f"You do not have permission to {phrasing} the column "
                    f"'{column.name}' on '{table_key}'.",
                    f"{table_key}.{name}",
                )

        return violations

    def expand_stars(
        self,
        sql: str,
        *,
        dialect: Optional[str],
        catalog_columns: Mapping[str, Mapping[str, AbstractSet[str]]],
    ) -> Optional[str]:
        """Rewrite ``SELECT *`` into the columns the caller may actually read.

        Returns the rewritten statement, or None if there was no star to expand
        (in which case the original is already exactly what was checked).

        **This is a security fix, not a convenience.** Validation runs against a
        qualified copy of the tree, where the qualifier has expanded ``*`` using
        the caller's filtered catalog -- so the check sees only permitted columns
        and passes. The database, handed the original text, expands the same star
        against the *physical* table and returns every column in it. A revoked
        column came back with its values, in a query the policy had just approved.

        Expanding the star before execution closes the gap by making the statement
        say what the validator understood it to say. Only statements containing a
        star are touched: rewriting every query would put the qualifier's output
        in front of users for no benefit, and it is a much larger behavioural
        change than this needs to be.
        """
        from sqlglot import expressions as exp

        try:
            ast = _sqlglot().parse_one(sql, dialect=dialect)
        except Exception:
            return None
        if ast is None or next(ast.find_all(exp.Star), None) is None:
            return None

        try:
            from sqlglot.optimizer.qualify import qualify

            qualified = qualify(
                ast, schema=self._qualify_schema(catalog_columns), dialect=dialect
            )
        except Exception:
            # The caller has already validated; a failure here means we cannot
            # produce a safe rewrite, and executing the original star is exactly
            # what must not happen.
            return None

        if next(qualified.find_all(exp.Star), None) is not None:
            # A star the qualifier could not expand -- `SELECT *` over a table
            # valued function, say. Leaving it would execute unexpanded.
            return None

        return qualified.sql(dialect=dialect, comments=False)

    @staticmethod
    def _note(
        violations: List[PolicyViolation],
        seen: Set[Tuple[str, str, str]],
        key: Tuple[str, str, str],
        code: ViolationCode,
        message: str,
        detail: str,
    ) -> None:
        """Record a violation once. A column named five times is one problem."""
        if key in seen:
            return
        seen.add(key)
        violations.append(PolicyViolation(code=code, message=message, detail=detail))

    @classmethod
    def _required_uses(
        cls, column: "exp.Column"
    ) -> List[Tuple[str, ViolationCode, str]]:
        """What this one reference needs, decided by where it sits.

        Walks every ancestor rather than inspecting the parent: ``WHERE UPPER(x) =
        'A'`` puts a function between the column and the clause, and a parent-only
        check sees the function and concludes nothing.
        """
        required = [("read", ViolationCode.COLUMN_NOT_ALLOWED, "read")]
        in_filter = False
        in_aggregate = False

        node = column.parent
        while node is not None:
            kind = type(node).__name__
            if kind in cls._FILTER_CONTEXTS:
                in_filter = True
            if kind in cls._AGGREGATE_CONTEXTS:
                in_aggregate = True
            node = node.parent

        if in_filter:
            required.append(
                ("filter", ViolationCode.COLUMN_FILTER_NOT_ALLOWED, "filter on")
            )
        if in_aggregate:
            required.append(
                ("aggregate", ViolationCode.COLUMN_AGGREGATE_NOT_ALLOWED, "aggregate")
            )
        return required


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
