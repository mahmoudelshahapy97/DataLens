"""Compiling semantic SQL into dialect SQL.

The shape of the output is one CTE per referenced model, each selecting that
model's columns -- physical, renamed, or computed -- out of its table, plus any
joins a relationship path needed. The user's query is then left almost exactly
as written, referring to those CTEs.

Why CTEs rather than inlining: the user's SQL keeps its structure, so what comes
back is recognisably what they wrote, and anything injected into a model (a row
rule, in P7) sits *inside* the CTE where a subquery or a UNION in the outer
query cannot get underneath it.

Order of operations matters and is not obvious:

1. Rewrite relationship paths **first**. ``qualify_columns`` treats
   ``orders.customer.region`` as an unknown identifier, so the paths have to
   become plain qualified columns before it runs.
2. Expand views verbatim. A view's statement is native SQL and is not ours to
   compile.
3. Collect model references, excluding CTE names the user defined -- otherwise a
   ``WITH orders AS (...)`` shadowing a model gets a second, conflicting CTE.
4. Build the model CTEs.
5. Render in the target dialect.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

import sqlglot
from sqlglot import exp

from ...core.errors import ErrorCode, ErrorPhase, VannaError
from ..models import Manifest, SemanticModel
from .models import CompiledSql, CompileWarning
from .traversal import TraversalPlan, projected_name, resolve_path

logger = logging.getLogger(__name__)

#: How deep a view may reference other views before we call it a loop.
_MAX_VIEW_DEPTH = 10


class SemanticRewriter:
    """Compiles semantic SQL for one manifest and dialect.

    Args:
        manifest: What the models mean.
        dialect: sqlglot dialect, used for both parsing and rendering. The
            semantic SQL a user writes is in their warehouse's dialect, so
            parsing with anything else mangles functions they legitimately used.
        fanout_guard: ``warn`` (default), ``reject``, or ``allow``. See
            :meth:`_check_fanout`.
    """

    def __init__(
        self,
        manifest: Manifest,
        *,
        dialect: str = "",
        fanout_guard: str = "warn",
    ) -> None:
        self.manifest = manifest
        self.dialect = dialect or None
        self.fanout_guard = fanout_guard

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def compile(self, sql: str) -> CompiledSql:
        """Compile one statement."""
        if not (sql or "").strip():
            raise VannaError(
                ErrorCode.INVALID_SQL,
                "No SQL to compile.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
            )

        try:
            statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s]
        except Exception as exc:
            raise VannaError(
                ErrorCode.INVALID_SQL,
                f"Could not parse the query: {exc}",
                phase=ErrorPhase.SEMANTIC_COMPILE,
                metadata={"semantic_sql": sql},
                cause=exc,
            )

        if len(statements) != 1:
            # Multiple statements are the stacked-injection shape; the policy
            # would reject them anyway, and compiling only the first would be
            # worse than refusing.
            raise VannaError(
                ErrorCode.INVALID_SQL,
                "Compile one statement at a time.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
            )

        ast = statements[0]
        warnings: List[CompileWarning] = []

        ast, used_views = self._expand_views(ast)
        user_ctes = self._user_cte_names(ast)
        plans: Dict[str, TraversalPlan] = {}

        ast = self._rewrite_relationship_paths(ast, user_ctes, plans)

        models = self._referenced_models(ast, user_ctes)
        # A path like `orders.customer.region` proves `orders` is in play even
        # if the FROM clause was written against something else entirely.
        models.update(plans)

        if not models:
            # Nothing semantic in it. Hand it back rendered for the target
            # dialect rather than inventing an empty WITH clause.
            return CompiledSql(
                sql=ast.sql(dialect=self.dialect, pretty=True),
                dialect=self.dialect or "",
                referenced_views=sorted(used_views),
            )

        ctes = []
        for name in sorted(models):
            model = self.manifest.model(name)
            if model is None:  # defensive: _referenced_models only yields models
                continue
            plan = plans.get(name, TraversalPlan())
            ctes.append((model.name, self._build_model_cte(model, plan)))
            warnings.extend(self._check_fanout(model, plan, ast))

        for name, select in ctes:
            ast = ast.with_(name, as_=select, copy=False)

        # Model CTEs go first. A non-recursive WITH may only reference CTEs
        # declared before it, so appending them left `WITH cheap AS (SELECT *
        # FROM tracks), tracks AS (...)` whenever the caller wrote their own CTE
        # over a model -- which Postgres rejects as an unknown table and the
        # column checker rejects as an unresolvable column.
        #
        # Reordered rather than inserted: `with_(append=False)` *replaces* the
        # clause, which silently dropped the caller's own CTE and turned the
        # error into "table `cheap` is not in the catalog".
        #
        # Safe as a stable partition: a model CTE reads a physical table and
        # never a user CTE, so hoisting one can never cross a dependency, and
        # user CTEs keep their order relative to each other.
        # `with_` in sqlglot 30, `with` in older ones. Reading only one of them
        # found nothing and reordered nothing, silently -- which looked exactly
        # like the reorder being unnecessary. Deliberately not `find(exp.With)`:
        # that would reach into a subquery's own WITH clause.
        with_node = (ast.args.get("with_") or ast.args.get("with")) if ctes else None
        if with_node is not None:
            declared = {name.lower() for name, _ in ctes}
            hoisted = [
                cte for cte in with_node.expressions
                if (cte.alias_or_name or "").lower() in declared
            ]
            rest = [
                cte for cte in with_node.expressions
                if (cte.alias_or_name or "").lower() not in declared
            ]
            with_node.set("expressions", hoisted + rest)

        return CompiledSql(
            sql=ast.sql(dialect=self.dialect, pretty=True),
            dialect=self.dialect or "",
            referenced_models=sorted(models),
            referenced_views=sorted(used_views),
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def _expand_views(self, ast: exp.Expression) -> Tuple[exp.Expression, Set[str]]:
        """Replace view references with their statements, as subqueries.

        A view's statement is native SQL written against real tables, so it is
        injected verbatim -- compiling it would mean re-interpreting SQL its
        author already made explicit.
        """
        used: Set[str] = set()

        for depth in range(_MAX_VIEW_DEPTH):
            replaced = False
            for table in list(ast.find_all(exp.Table)):
                name = table.name
                view = self.manifest.view(name) if name else None
                if view is None:
                    continue

                try:
                    inner = sqlglot.parse_one(view.statement, read=self.dialect)
                except Exception as exc:
                    raise VannaError(
                        ErrorCode.INVALID_MANIFEST,
                        f"view {view.name!r} does not parse: {exc}",
                        phase=ErrorPhase.SEMANTIC_COMPILE,
                        cause=exc,
                    )

                # Keep the alias the user wrote, or fall back to the view name
                # so downstream column qualifiers still resolve.
                alias = table.alias or view.name
                table.replace(exp.Subquery(this=inner, alias=exp.TableAlias(
                    this=exp.to_identifier(alias)
                )))
                used.add(view.name)
                replaced = True

            if not replaced:
                return ast, used

        raise VannaError(
            ErrorCode.INVALID_MANIFEST,
            "Views reference each other in a loop.",
            phase=ErrorPhase.SEMANTIC_COMPILE,
            hint=f"Gave up after {_MAX_VIEW_DEPTH} levels of expansion.",
        )

    # ------------------------------------------------------------------
    # Reference collection
    # ------------------------------------------------------------------

    @staticmethod
    def _user_cte_names(ast: exp.Expression) -> Set[str]:
        """CTE names the user defined.

        A ``WITH orders AS (...)`` shadows the model of the same name. Treating
        it as a model reference would add a second, conflicting CTE and produce
        a duplicate-name error nobody could explain.
        """
        return {
            cte.alias_or_name.lower()
            for cte in ast.find_all(exp.CTE)
            if cte.alias_or_name
        }

    @staticmethod
    def _sources_in_scope(ast: exp.Expression) -> Set[str]:
        """Every table name and alias the query itself introduces.

        Used to tell ``customer.region`` (a traversal) from ``c.region`` (a
        column of an aliased table). Anything in here is the user's own
        qualifier and must be left alone.
        """
        names: Set[str] = set()
        for table in ast.find_all(exp.Table):
            if table.name:
                names.add(table.name.lower())
            if table.alias:
                names.add(table.alias.lower())
        for subquery in ast.find_all(exp.Subquery):
            if subquery.alias:
                names.add(subquery.alias.lower())
        return names

    def _model_owning_handle(
        self, handle: str, candidates: Set[str]
    ) -> Optional[SemanticModel]:
        """The model whose relationship column is named ``handle``.

        None when no model has it, and also when *several* do -- an ambiguous
        short form must be rejected rather than resolved by picking one, since
        the two readings return different data.
        """
        lowered = handle.lower()
        owners = [
            model
            for name in candidates
            if (model := self.manifest.model(name)) is not None
            and any(c.name.lower() == lowered for c in model.relationship_columns)
        ]
        if len(owners) == 1:
            return owners[0]
        if len(owners) > 1:
            raise VannaError(
                ErrorCode.INVALID_SQL,
                f"{handle!r} is a relationship on more than one model in this "
                "query, so it is ambiguous.",
                phase=ErrorPhase.SEMANTIC_COMPILE,
                hint=f"Qualify it: {owners[0].name}.{handle}.<column>",
            )
        return None

    def _referenced_models(
        self, ast: exp.Expression, user_ctes: Set[str]
    ) -> Set[str]:
        found: Set[str] = set()
        for table in ast.find_all(exp.Table):
            name = table.name
            if not name or name.lower() in user_ctes:
                continue
            model = self.manifest.model(name)
            if model is not None:
                found.add(model.name)
        return found

    # ------------------------------------------------------------------
    # Relationship paths
    # ------------------------------------------------------------------

    def _rewrite_relationship_paths(
        self,
        ast: exp.Expression,
        user_ctes: Set[str],
        plans: Dict[str, TraversalPlan],
    ) -> exp.Expression:
        """Rewrite relationship paths to columns of the model's CTE.

        Two spellings, because both are natural and people use both:

        * ``orders.customer.region`` -- fully qualified, unambiguous.
        * ``customer.region`` -- the short form, which is what anyone actually
          writes when there is one table in the FROM clause. It *looks* exactly
          like ``table.column``, so it is only treated as a traversal when
          ``customer`` is not a table or alias in scope and is a relationship
          column on exactly one referenced model.
        """
        in_scope = self._sources_in_scope(ast)
        candidates = self._referenced_models(ast, user_ctes)

        for column in list(ast.find_all(exp.Column)):
            parts = [p.name for p in column.parts]

            if len(parts) >= 3:
                root_name, rest = parts[0], parts[1:]
                if root_name.lower() in user_ctes:
                    continue
                root = self.manifest.model(root_name)
            elif len(parts) == 2:
                handle = parts[0]
                if handle.lower() in in_scope or handle.lower() in user_ctes:
                    continue  # a real table or alias qualifier
                root = self._model_owning_handle(handle, candidates)
                rest = parts
            else:
                continue

            if root is None:
                continue

            plan = plans.setdefault(root.name, TraversalPlan())
            alias, leaf = resolve_path(self.manifest, root, rest, plan)

            # The join alias lives *inside* the model's CTE and is invisible to
            # the outer query, so the reference becomes a column *of the model*:
            # `orders.customer.region` -> `orders._rel_customer_region`, which
            # the CTE projects. Rewriting it to `_rel_customer.region` would
            # produce SQL referencing a table that is not in scope.
            projected = projected_name(alias, leaf)
            plan.project(alias, leaf)

            column.replace(
                exp.column(
                    exp.to_identifier(projected), table=exp.to_identifier(root.name)
                )
            )

        return ast

    # ------------------------------------------------------------------
    # Model CTEs
    # ------------------------------------------------------------------

    def _build_model_cte(
        self, model: SemanticModel, plan: TraversalPlan
    ) -> exp.Expression:
        """``SELECT <columns> FROM <source> [LEFT JOIN ...]`` for one model."""
        projections: List[str] = []

        for column in model.columns:
            if column.is_relationship:
                continue  # an edge, not a value
            expression = self._column_expression(model, column)
            projections.append(f"{expression} AS {self._quote(column.name)}")

        # Only the traversed columns are projected, not every column of every
        # joined model: a model with four relationships would otherwise carry a
        # hundred columns nobody asked for through every query.
        for alias, leaf in plan.projections():
            projections.append(
                f"{self._quote(alias)}.{self._quote(leaf)} AS "
                f"{self._quote(projected_name(alias, leaf))}"
            )

        body = (
            f"SELECT {', '.join(projections)} "
            f"FROM {model.source} AS {self._quote(model.name)}"
        )

        for step in plan.ordered:
            target = self.manifest.model(step.target_model)
            target_source = target.source if target else step.target_model
            body += (
                f" LEFT JOIN {target_source} AS {self._quote(step.alias)} "
                f"ON {step.condition}"
            )

        try:
            return sqlglot.parse_one(body, read=self.dialect)
        except Exception as exc:
            raise VannaError(
                ErrorCode.COMPILATION_FAILED,
                f"model {model.name!r} produced SQL that does not parse: {exc}",
                phase=ErrorPhase.SEMANTIC_COMPILE,
                metadata={"dialect_sql": body},
                hint="Usually a bad expression on a calculated column.",
                cause=exc,
            )

    def _column_expression(self, model: SemanticModel, column) -> str:
        """SQL for one column inside its model's CTE.

        Calculated expressions are written over the model's *own* column names,
        which are the physical names at this point, so they need no rewriting --
        only qualification, so that a join alias cannot capture them.
        """
        if column.is_calculated:
            if not column.expression:
                raise VannaError(
                    ErrorCode.INVALID_MANIFEST,
                    f"{model.name}.{column.name} is calculated but has no expression.",
                    phase=ErrorPhase.SEMANTIC_COMPILE,
                )
            return f"({self._qualify_bare(column.expression, model)})"

        source = column.expression or column.name
        if source == column.name:
            return f"{self._quote(model.name)}.{self._quote(column.name)}"
        return self._qualify_bare(source, model)

    def _qualify_bare(self, expression: str, model: SemanticModel) -> str:
        """Qualify this model's own column names inside an expression.

        Textual and word-bounded. A full parse would be more precise but needs
        the target dialect to be exactly right about function syntax, and the
        substitution -- a known column name to the same name with a table
        prefix -- is unambiguous without one.
        """
        result = expression
        for name in sorted((c.name for c in model.columns), key=len, reverse=True):
            result = re.sub(
                rf"(?<![\w.]){re.escape(name)}(?![\w(])",
                f"{self._quote(model.name)}.{self._quote(name)}",
                result,
            )
        return result

    def _quote(self, identifier: str) -> str:
        """Quote an identifier for the target dialect.

        Always quoting is what keeps a column called ``order`` or ``Region``
        working on Snowflake, which upper-cases anything unquoted, and on
        PostgreSQL, which lower-cases it.
        """
        return exp.to_identifier(identifier, quoted=True).sql(dialect=self.dialect)

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------

    def _check_fanout(
        self, model: SemanticModel, plan: TraversalPlan, ast: exp.Expression
    ) -> List[CompileWarning]:
        """Warn when an aggregate crosses an edge that multiplies rows.

        The failure this catches: ``SUM(customers.orders.amount)`` joins one
        customer to many orders, so the customer's row is repeated and any
        aggregate over the *customer* side is inflated. The SQL is valid, the
        number is wrong, and nothing in the result hints at it.

        Only warned about when the query actually aggregates -- listing rows
        across a one-to-many join is the normal, correct thing to do.
        """
        if not plan.fan_out_paths or self.fanout_guard == "allow":
            return []

        if not self._has_aggregate(ast):
            return []

        paths = ", ".join(".".join(p) for p in plan.fan_out_paths)
        message = (
            f"Aggregating across a one-to-many join ({model.name} -> {paths}) "
            f"repeats each {model.name} row once per match, which inflates "
            f"SUM, COUNT and AVG over {model.name}'s own columns."
        )

        if self.fanout_guard == "reject":
            raise VannaError(
                ErrorCode.COMPILATION_FAILED,
                message,
                phase=ErrorPhase.SEMANTIC_COMPILE,
                hint=(
                    "Aggregate the many side in a subquery first, or use a cube "
                    "measure, which encodes the correct grain."
                ),
            )

        return [
            CompileWarning(
                code="fanout",
                message=message,
                detail="Verify the total against a pre-aggregated query before trusting it.",
            )
        ]

    @staticmethod
    def _has_aggregate(ast: exp.Expression) -> bool:
        return any(ast.find_all(exp.AggFunc)) or bool(ast.find(exp.Group))
