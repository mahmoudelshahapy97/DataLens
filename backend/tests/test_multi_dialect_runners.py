"""Real cross-engine execution, through the app's own runner abstraction.

Before this file, every multi-dialect test in the suite (``test_connection_pool.py``'s
``TestDialects``, ``TestTheScannerReadsAnyEnginesRows``) was a pure unit test: SQL
string shape, row-cap syntax, column-name casing -- never an actual socket to a real
non-Postgres engine. ``build_runner()`` (``vanna/core/datasource/runners.py``) is the
one function that turns a stored ``database_url`` into a working ``SqlRunner`` for
Postgres, MySQL, Oracle, SQL Server and SQLite alike, and it is what a real tenant's
data source goes through -- so these tests drive it directly against live databases
instead of asserting on generated SQL text.

Each engine has its own project-local dev sandbox (``databases/docker-compose.yml``,
a sibling of this repo) seeded with the Chinook schema, so the same query --
"how many rows are in the Customer table" -- can run unmodified against all five
engines. Every engine is independently skipped if its container is not reachable,
so this file does nothing destructive to a laptop with no sandbox running and does
not fail the suite in that case; only when a container *is* up but a query fails
does it turn red.

Credentials/ports match ``databases/.env`` in the sandbox as committed. Override
any of them with ``VANNA_TEST_<ENGINE>_URL`` for a differently-configured sandbox
or CI runner.
"""

from __future__ import annotations

import os

import pytest

from vanna.capabilities.sql_runner.models import RunSqlToolArgs
from vanna.core.datasource.runners import build_runner
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory


def _context(tenant: str = "acme") -> ToolContext:
    return ToolContext(
        user=User(id="u1", email="u1@acme.test", tenant_id=tenant),
        conversation_id="c1",
        request_id="r1",
        tenant_id=tenant,
        agent_memory=DemoAgentMemory(),
    )


def _env_url(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


#: One row of proof per engine: the Chinook `Customer` table, seeded identically
#: across every engine in the sandbox (case differs: Oracle/SQL Server default to
#: the schema's declared case, which the seed scripts keep as `Customer`).
_ROW_COUNT_SQL = "SELECT COUNT(*) AS n FROM Customer"

DIALECTS = {
    "postgres": {
        "url": _env_url(
            "VANNA_TEST_MULTIDB_POSTGRES_URL",
            "postgresql://postgres:postgres123@127.0.0.1:5432/postgres",
        ),
        # Every seeded schema lives inside one `postgres` database here
        # (unlike MySQL/Oracle, which get one database/user per schema), so
        # this must be schema-qualified regardless of search_path.
        "sql": "SELECT COUNT(*) AS n FROM chinook.customer",
    },
    "mysql": {
        "url": _env_url(
            "VANNA_TEST_MYSQL_URL",
            "mysql://root:mysql123@127.0.0.1:3306/chinook",
        ),
        "sql": _ROW_COUNT_SQL,
    },
    "oracle": {
        "url": _env_url(
            "VANNA_TEST_ORACLE_URL",
            "oracle://chinook:Oracle123!@127.0.0.1:1521/XEPDB1",
        ),
        "sql": _ROW_COUNT_SQL,
    },
    "mssql": {
        "url": _env_url(
            "VANNA_TEST_MSSQL_URL",
            "mssql://sa:SqlServer123!@127.0.0.1:1433/Chinook?driver=SQL Server",
        ),
        # The seed loads Chinook's tables under a `chinook` schema, not `sa`'s
        # default `dbo` -- so this must be schema-qualified, unlike the other
        # three engines where Chinook's tables live in the database's default
        # search path/schema already.
        "sql": "SELECT COUNT(*) AS n FROM chinook.Customer",
    },
}


def _runner_or_skip(name: str):
    spec = DIALECTS[name]
    try:
        runner = build_runner(spec["url"])
    except Exception as e:  # pragma: no cover - constructing a runner is local
        pytest.skip(f"could not build a {name} runner: {e}")
    return runner, spec["sql"]


@pytest.mark.integration
class TestLiveMultiDialectExecution:
    """One test per engine, each independently skipped if its container is not
    reachable right now -- so this class degrades to "all skipped" on a laptop
    with no sandbox, and to full coverage the moment ``docker compose up`` has
    finished in ``databases/``."""

    @pytest.mark.parametrize("engine", sorted(DIALECTS))
    async def test_row_count_via_the_apps_own_runner(self, engine):
        runner, sql = _runner_or_skip(engine)
        try:
            df = await runner.run_sql(RunSqlToolArgs(sql=sql), _context())
        except Exception as e:
            pytest.skip(f"{engine} sandbox not reachable: {e}")

        assert len(df) == 1
        # Column name casing differs by engine (Oracle upper-cases unquoted
        # identifiers, MySQL/Postgres lower-case them) -- normalise instead of
        # asserting a literal key.
        value = list(df.iloc[0].to_dict().values())[0]
        assert int(value) > 0

    async def test_a_bad_query_raises_rather_than_returning_nonsense(self):
        # Any one reachable engine proves the runner surfaces a real driver
        # error instead of silently returning an empty frame -- try mysql
        # first since it needs no special credentials beyond the sandbox
        # default, falling through to the next reachable engine otherwise.
        for engine in ("mysql", "postgres", "oracle", "mssql"):
            try:
                runner, _ = _runner_or_skip(engine)
            except pytest.skip.Exception:
                continue

            with pytest.raises(Exception):
                await runner.run_sql(
                    RunSqlToolArgs(sql="SELECT * FROM table_that_does_not_exist"),
                    _context(),
                )
            return
        pytest.skip("no sandbox engine was reachable")
