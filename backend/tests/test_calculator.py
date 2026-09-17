"""The calculator: a whitelisted-AST evaluator, not `eval`.

`_analyze_setup` has claimed a calculator tool exists since before one did
(`vanna/core/workflow/default.py:311-313`). These tests are less about
arithmetic and more about the denial surface: every construct here reaches
Python's real `__import__`/`os` if the walker ever falls back to a blacklist
instead of staying default-deny.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vanna.core.registry import ToolRegistry
from vanna.core.tool import ToolCall, ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.tools.calculator import CalculatorError, CalculatorTool, evaluate


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id="acme"),
        conversation_id="c1",
        request_id="r1",
        agent_memory=DemoAgentMemory(),
    )


class TestArithmetic:
    @pytest.mark.parametrize(
        "expression, expected",
        [
            ("0.1 + 0.2", "0.3"),
            ("round(2.5)", "3"),
            ("round(2.567, 2)", "2.57"),
            ("2 + 2", "4"),
            ("10 // 3", "3"),
            ("2 ** 10", "1024"),
            ("-5 + 3", "-2"),
            ("abs(-7)", "7"),
            ("min(3, 1, 2)", "1"),
            ("max(3, 1, 2)", "3"),
        ],
    )
    def test_evaluates_correctly(self, expression, expected):
        assert str(evaluate(expression).normalize()).lstrip("+") == expected or str(
            evaluate(expression)
        ) == expected


class TestDeniedConstructs:
    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('echo hi')",
            "().__class__",
            "().__class__.__bases__",
            "lambda: 1",
            "[x for x in range(10)]",
            "9**9**9",
            "1/0",
            "1" + "+1" * 2000,
            "open('x')",
        ],
    )
    def test_refuses(self, expression):
        with pytest.raises(CalculatorError):
            evaluate(expression)


class TestToolSurface:
    @pytest.fixture
    def registry(self):
        registry = ToolRegistry()
        registry.register_local_tool(CalculatorTool(), [])
        return registry

    def test_no_sql_argument_fields(self):
        assert CalculatorTool().sql_argument_fields == ()

    async def test_execute_success(self, registry):
        result = await registry.execute(
            ToolCall(id="1", name="calculator", arguments={"expression": "2 + 2"}),
            _context(),
        )
        assert result.success
        assert "4" in result.result_for_llm

    async def test_execute_denied_construct_fails_gracefully(self, registry):
        result = await registry.execute(
            ToolCall(
                id="1",
                name="calculator",
                arguments={"expression": "__import__('os')"},
            ),
            _context(),
        )
        assert not result.success

    async def test_invalid_expression_type_rejected_by_schema(self):
        with pytest.raises(ValidationError):
            CalculatorTool().get_args_schema()(expression=123)
