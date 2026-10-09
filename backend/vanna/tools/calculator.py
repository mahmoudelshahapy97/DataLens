"""Arithmetic, without a SQL round-trip and without `eval`.

The setup panel has claimed a calculator exists since before one did:
``DefaultWorkflowHandler._analyze_setup`` probes ``tool_names`` for
``"calculator"``/``"calc"``/``"calculate"``. This is the tool that makes that
claim true.

``ast.literal_eval`` cannot be the evaluator -- since Python 3.8 it rejects
``BinOp``, so ``literal_eval("2+2")`` raises. So this walks the AST itself with
a default-deny dispatch: only ``Expression``, a numeric ``Constant``, ``BinOp``
over the seven arithmetic operators, unary +/-, and ``Call`` to a whitelisted
function name are ever evaluated. ``Name`` and ``Attribute`` are both denied,
which is what makes ``__import__`` and ``().__class__`` unreachable
structurally rather than by blacklist.

Arithmetic runs in ``decimal.Decimal`` rather than ``float``, for two reasons
that both matter to a business user reading the answer: ``0.1 + 0.2`` must
print ``0.3``, not ``0.30000000000000004``; and rounding must be
``ROUND_HALF_UP`` rather than Python's default banker's rounding, because
``round(2.5) == 2`` reads as a bug, not a convention, to someone who was not
told about banker's rounding.
"""

from __future__ import annotations

import ast
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from typing import Type

from pydantic import BaseModel, Field

from vanna.components import RichTextComponent, SimpleTextComponent, UiComponent
from vanna.core.tool import Tool, ToolContext, ToolResult

#: Caps chosen so `9**9**9` (which would otherwise allocate gigabytes trying to
#: represent the result before any arithmetic trap can fire) is refused before
#: computing anything, not caught after.
_MAX_EXPONENT = 1_000
_MAX_BASE_FOR_LARGE_EXPONENT = 1_000_000

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: (a / b).to_integral_value(rounding=ROUND_HALF_UP),
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: _pow(a, b),
}

_FUNCTIONS = {
    "abs": lambda x: abs(x),
    "round": lambda x, *n: x.quantize(
        Decimal(1).scaleb(-int(n[0])) if n else Decimal(1),
        rounding=ROUND_HALF_UP,
    ),
    "min": lambda *xs: min(xs),
    "max": lambda *xs: max(xs),
}


class CalculatorError(Exception):
    """A refusal, always with a message safe to show the model verbatim."""


def _pow(base: Decimal, exponent: Decimal) -> Decimal:
    if exponent == exponent.to_integral_value() and abs(exponent) > _MAX_EXPONENT:
        raise CalculatorError(f"Exponent magnitude over {_MAX_EXPONENT} is not allowed.")
    if abs(base) > _MAX_BASE_FOR_LARGE_EXPONENT and abs(exponent) > 100:
        raise CalculatorError("Base is too large for that exponent.")
    return base**exponent


def _eval_node(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculatorError(f"{node.value!r} is not a number.")
        return Decimal(str(node.value))

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalculatorError(f"Operator {type(node.op).__name__} is not allowed.")
        return op(_eval_node(node.left), _eval_node(node.right))

    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
        raise CalculatorError(f"Operator {type(node.op).__name__} is not allowed.")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise CalculatorError("Only abs, round, min, and max may be called.")
        if node.keywords:
            raise CalculatorError("Keyword arguments are not allowed.")
        args = [_eval_node(a) for a in node.args]
        try:
            return _FUNCTIONS[node.func.id](*args)
        except (TypeError, ValueError) as e:
            raise CalculatorError(f"Invalid arguments to {node.func.id}: {e}") from e

    # Name, Attribute, Subscript, Lambda, comprehensions, and everything else
    # fall through to this and are refused -- denied by omission, not by
    # blacklist, which is what keeps `__import__` and `().__class__` reachable
    # by nothing this evaluator will walk into.
    raise CalculatorError(
        f"{type(node).__name__} is not a supported arithmetic construct."
    )


def evaluate(expression: str) -> Decimal:
    """Evaluate a whitelisted arithmetic expression.

    Raises CalculatorError for anything outside plain arithmetic, and the
    Decimal traps (Overflow, InvalidOperation, DivisionByZero) for the
    numeric edge cases those exist to catch.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise CalculatorError(f"Could not parse {expression!r} as arithmetic: {e}") from e
    except RecursionError as e:
        raise CalculatorError("Expression is nested too deeply.") from e

    with localcontext(Context(prec=28, traps=[Overflow, InvalidOperation, DivisionByZero])):
        try:
            return _eval_node(tree)
        except ZeroDivisionError as e:
            raise CalculatorError("Division by zero.") from e
        except (Overflow, InvalidOperation, DivisionByZero) as e:
            raise CalculatorError(f"Arithmetic error: {e}") from e
        except RecursionError as e:
            raise CalculatorError("Expression is nested too deeply.") from e


class CalculatorArgs(BaseModel):
    """Arguments for the calculator tool."""

    expression: str = Field(
        description="An arithmetic expression, e.g. '1250 * 1.075' or "
        "'round((42 - 17) / 3, 2)'. Supports + - * / // % ** and abs/round/min/max."
    )


class CalculatorTool(Tool[CalculatorArgs]):
    """Evaluates arithmetic so the model does not have to, or invent a SQL round-trip for it."""

    #: Not SQL-bearing. Without this, the policy's name-based fallback would
    #: treat `expression` as untouched (it is not in DEFAULT_SQL_FIELDS), but
    #: declaring it explicitly means a later rename cannot resurrect the trap.
    sql_argument_fields: tuple = ()

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return (
            "Evaluate an arithmetic expression exactly, without rounding "
            "error. Use this for math on numbers already in the conversation "
            "-- percentages, totals, unit conversions -- instead of writing "
            "SQL just to compute a number or doing the arithmetic yourself."
        )

    def get_args_schema(self) -> Type[CalculatorArgs]:
        return CalculatorArgs

    async def execute(
        self, context: ToolContext, args: CalculatorArgs
    ) -> ToolResult:
        try:
            result = evaluate(args.expression)
        except CalculatorError as e:
            return ToolResult(
                success=False,
                result_for_llm=str(e),
                ui_component=UiComponent(
                    rich_component=RichTextComponent(content=str(e), markdown=False),
                    simple_component=SimpleTextComponent(text=str(e)),
                ),
                error=str(e),
            )

        text = f"{args.expression} = {_format(result)}"
        return ToolResult(
            success=True,
            result_for_llm=text,
            ui_component=UiComponent(
                rich_component=RichTextComponent(content=text, markdown=False),
                simple_component=SimpleTextComponent(text=text),
            ),
            metadata={"result": str(result)},
        )


def _format(value: Decimal) -> str:
    normalized = value.normalize()
    # `normalize()` on an integral Decimal can fall back to scientific notation
    # (`Decimal('1E+2')`), which is correct but not how a person reads a total.
    if normalized == normalized.to_integral_value():
        return str(normalized.to_integral_value())
    return str(normalized)
