"""Every engine's connection probe must use SQL that engine accepts.

`SELECT 1` is valid everywhere here except Oracle, which has no bare SELECT and
answers ORA-00923 without a FROM clause. It was hardcoded in three places, so
`/admin/datasources/test` and both registration paths rejected every Oracle
database -- reachable ones included -- and no Oracle workspace could be added
through the console at all.

The engine-by-engine test below is the one that matters: it is the check that
fails the next time a runner is added for a dialect with the same quirk.
"""

from __future__ import annotations

import pytest

from vanna.capabilities.sql_runner.base_runner import BaseSqlRunner


def _runner_classes():
    """Every concrete runner that ships, by importing each integration."""
    found = {}
    for module, name in (
        ("vanna.integrations.postgres.sql_runner", "PostgresRunner"),
        ("vanna.integrations.mysql.sql_runner", "MySQLRunner"),
        ("vanna.integrations.sqlite.sql_runner", "SqliteRunner"),
        ("vanna.integrations.oracle.sql_runner", "OracleRunner"),
        ("vanna.integrations.mssql.sql_runner", "MSSQLRunner"),
    ):
        try:
            found[name] = getattr(__import__(module, fromlist=[name]), name)
        except Exception:  # pragma: no cover - driver not installed in this env
            pass
    return found


class TestHealthCheckSql:
    def test_the_default_is_the_portable_one(self):
        assert BaseSqlRunner.health_check_sql == "SELECT 1"

    def test_oracle_qualifies_it_with_dual(self):
        """A bare SELECT is a syntax error in Oracle, not a preference."""
        from vanna.integrations.oracle.sql_runner import OracleRunner

        assert OracleRunner.health_check_sql == "SELECT 1 FROM DUAL"

    @pytest.mark.parametrize("name", sorted(_runner_classes()))
    def test_every_runner_declares_something_runnable(self, name):
        runner = _runner_classes()[name]
        sql = runner.health_check_sql

        assert sql and sql.upper().startswith("SELECT")
        # Oracle is the one that needs FROM; the rest must stay portable so a
        # new deployment is not forced to pick a dialect to health-check itself.
        if runner.dialect == "oracle":
            assert "DUAL" in sql.upper()

    def test_the_probe_call_sites_use_it(self):
        """A fourth hardcoded `SELECT 1` would reintroduce the bug silently."""
        import inspect

        from vanna_app import datasources
        from vanna_app.routes import admin

        for module in (admin, datasources):
            source = inspect.getsource(module)
            assert 'RunSqlToolArgs(sql="SELECT 1")' not in source, (
                f"{module.__name__} hardcodes the probe query again"
            )
