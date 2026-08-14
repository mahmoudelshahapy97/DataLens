"""Build the right SQL runner for a connection URL.

The companion to ``connections.py``: that module says what a connection *is*,
this one turns one into something that can execute SQL.

It lives here rather than in the deployment because three places need it -- the
per-tenant runtime, the "test this connection" button, and the CLI -- and when
each had its own version, two of them only knew about PostgreSQL.

Every driver is imported inside its own branch. Importing them at module scope
would make the whole package depend on ``snowflake-connector-python``,
``oracledb`` and nine others, when a given deployment uses one.
"""

from __future__ import annotations

from typing import Any, Optional

from .connections import engine_for_url, parse_url


class UnsupportedDataSource(ValueError):
    """Raised for a URL no runner can be built for.

    Its own type because the caller usually wants to answer "that engine is not
    supported" rather than treat it as a connection failure -- the two have very
    different fixes.
    """


def build_runner(
    database_url: str,
    *,
    policy: Any = None,
    read_only: bool = True,
) -> Any:
    """Return a SQL runner for *database_url*.

    Args:
        database_url: A connection URL as produced by ``Engine.url``.
        policy: An ``ExecutionPolicy`` (row cap, timeout). Passed through.
        read_only: Ask the driver for a read-only connection where it supports
            one. Not every engine can; those that cannot rely on the SQL policy
            and the statement allow-list instead.

    Raises:
        UnsupportedDataSource: if the URL names an engine with no runner. This
            deliberately does **not** fall back to a local demo database --
            answering questions from the wrong data looks like success and is
            far worse than an error.
    """
    engine = engine_for_url(database_url)
    if engine is None:
        scheme = (database_url or "").split("://", 1)[0] or database_url
        raise UnsupportedDataSource(
            f"Unsupported data source {scheme!r}. Supported engines: "
            "postgres, mysql, mssql, oracle, clickhouse, snowflake, bigquery, "
            "hive, presto, duckdb, sqlite."
        )

    fields = parse_url(database_url)

    def get(name: str, default: str = "") -> str:
        return str(fields.get(name) or default)

    name = engine.name

    if name == "postgres":
        from ...integrations.postgres import PostgresRunner

        return PostgresRunner(
            connection_string=database_url, policy=policy, read_only=read_only
        )

    if name == "mysql":
        from ...integrations.mysql import MySQLRunner

        return MySQLRunner(
            host=get("host"),
            database=get("database"),
            user=get("username"),
            password=get("password"),
            port=int(get("port", "3306")),
            policy=policy,
        )

    if name == "clickhouse":
        from ...integrations.clickhouse import ClickHouseRunner

        return ClickHouseRunner(
            host=get("host"),
            database=get("database"),
            user=get("username"),
            password=get("password"),
            port=int(get("port", "8123")),
            policy=policy,
        )

    if name == "oracle":
        from ...integrations.oracle import OracleRunner

        return OracleRunner(
            user=get("username"),
            password=get("password"),
            dsn=f"{get('host')}:{get('port', '1521')}/{get('database')}",
            policy=policy,
        )

    if name == "mssql":
        from ...integrations.mssql import MSSQLRunner

        driver = get("driver", "ODBC Driver 18 for SQL Server")
        return MSSQLRunner(
            odbc_conn_str=(
                f"DRIVER={{{driver}}};SERVER={get('host')},{get('port', '1433')};"
                f"DATABASE={get('database')};UID={get('username')};"
                f"PWD={get('password')}"
            ),
            policy=policy,
        )

    if name == "snowflake":
        from ...integrations.snowflake import SnowflakeRunner

        return SnowflakeRunner(
            account=get("account"),
            username=get("username"),
            password=get("password") or None,
            database=get("database"),
            role=get("role") or None,
            warehouse=get("warehouse") or None,
            policy=policy,
        )

    if name == "bigquery":
        from ...integrations.bigquery import BigQueryRunner

        return BigQueryRunner(
            project_id=get("project"),
            cred_file_path=get("credentials_path") or None,
            policy=policy,
        )

    if name == "hive":
        from ...integrations.hive import HiveRunner

        return HiveRunner(
            host=get("host"),
            database=get("database", "default"),
            user=get("username") or None,
            password=get("password") or None,
            port=int(get("port", "10000")),
            policy=policy,
        )

    if name == "presto":
        from ...integrations.presto import PrestoRunner

        return PrestoRunner(
            host=get("host"),
            catalog=get("catalog", "hive"),
            schema=get("schema", "default"),
            user=get("username") or None,
            password=get("password") or None,
            port=int(get("port", "443")),
            policy=policy,
        )

    if name == "duckdb":
        from ...integrations.duckdb import DuckDBRunner

        return DuckDBRunner(
            database_path=get("path", ":memory:"),
            policy=policy,
            read_only=read_only,
        )

    from ...integrations.sqlite import SqliteRunner

    return SqliteRunner(get("path", ":memory:"), policy=policy, read_only=read_only)


def probe(database_url: str, *, timeout_seconds: int = 8) -> Optional[Any]:
    """Build a throwaway runner for a connection test.

    One row, short timeout, read-only: enough to prove the credentials work and
    the host is reachable without waiting on a real query.
    """
    from ...capabilities.sql_runner import ExecutionPolicy

    return build_runner(
        database_url,
        policy=ExecutionPolicy(max_rows=1, timeout_seconds=timeout_seconds),
        read_only=True,
    )
