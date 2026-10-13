"""Views reach the prompt as views, with their definition.

Found live: the employees sample's `current_dept_emp` returns every employee's
*latest* department, former staff included, and the agent -- shown it as a plain
table -- took the name at its word and counted 300,024 "current" employees
instead of 240,124. A view's name can misstate what it returns; its definition
cannot.
"""

from __future__ import annotations

import sqlite3

import pytest

from vanna.capabilities.schema_catalog import SchemaScanner
from vanna.capabilities.schema_catalog.models import TableMetadata
from vanna.capabilities.schema_catalog.scanner import VIEW_DEFINITION_CHARS, _short
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.schema_catalog import LocalSchemaCatalog
from vanna.integrations.sqlite import SqliteRunner


def _context() -> ToolContext:
    return ToolContext(
        user=User(id="system", email="system@acme.test", tenant_id="acme"),
        conversation_id="scan",
        request_id="scan",
        tenant_id="acme",
        agent_memory=DemoAgentMemory(),
    )


@pytest.fixture
def warehouse(tmp_path):
    path = tmp_path / "hr.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE dept_emp (emp_no INTEGER, dept_no TEXT, to_date TEXT);
        CREATE VIEW current_dept_emp AS
            SELECT emp_no, dept_no, MAX(to_date) AS to_date
              FROM dept_emp GROUP BY emp_no;
        INSERT INTO dept_emp VALUES (1, 'd1', '9999-01-01'), (2, 'd2', '1999-01-01');
        """
    )
    conn.commit()
    conn.close()
    return str(path)


class TestScannerRecordsViews:
    async def test_a_view_is_recorded_with_its_definition(self, warehouse):
        catalog = LocalSchemaCatalog()
        await SchemaScanner(SqliteRunner(warehouse), dialect="sqlite").scan(
            _context(), catalog
        )
        tables = {t.table_name: t for t in await catalog.get_tables(_context())}

        assert not tables["dept_emp"].is_view
        view = tables["current_dept_emp"]
        assert view.is_view
        assert "MAX(to_date)" in view.metadata["view_definition"]
        assert "\n" not in view.metadata["view_definition"]

    async def test_the_prompt_says_view_and_shows_the_definition(self, warehouse):
        catalog = LocalSchemaCatalog()
        await SchemaScanner(SqliteRunner(warehouse), dialect="sqlite").scan(
            _context(), catalog
        )
        context = await catalog.get_context(_context(), "current employees")

        assert "### View: current_dept_emp" in context.text
        assert "Defined as: CREATE VIEW current_dept_emp" in context.text
        assert "### Table: dept_emp" in context.text


class TestDescribe:
    def _view(self, **kwargs) -> TableMetadata:
        return TableMetadata(
            table_name="current_dept_emp",
            schema_name="employees",
            metadata={"kind": "view", "view_definition": "SELECT ... latest ..."},
            **kwargs,
        )

    def test_a_human_description_wins_over_the_definition(self):
        text = self._view(description="Latest department, former staff included.").describe()
        assert text.startswith("### View: employees.current_dept_emp")
        assert "former staff included" in text
        assert "Defined as" not in text

    def test_definition_shown_when_nobody_described_it(self):
        assert "Defined as: SELECT ... latest ..." in self._view().describe()

    def test_tables_are_unchanged(self):
        text = TableMetadata(table_name="dept_emp").describe()
        assert text.startswith("### Table: dept_emp")
        assert "Defined as" not in text

    def test_definitions_are_capped(self):
        long = "SELECT " + "x, " * 1000
        capped = _short(long)
        assert len(capped) <= VIEW_DEFINITION_CHARS and capped.endswith("...")
        assert _short(None) is None
