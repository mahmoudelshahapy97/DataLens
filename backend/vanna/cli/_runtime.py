"""Building a runner and a catalog from a profile and a project.

The missing piece between configuration and execution. Until now the CLI could
describe a project and validate it but not *use* it, so ``vanna query`` had
nothing to run against and the packaged skills referred to a command that did
not exist.

Kept deliberately small: this assembles the pieces needed to run SQL, not a
full agent. Agent assembly involves an LLM service, knowledge stores, lifecycle
hooks and middleware, and ``vanna_app/platform.py`` already does it -- a
second, thinner assembly here would drift from that one and quietly answer
questions differently.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Tuple

from ..capabilities.sql_runner import ExecutionPolicy
from ..config import Profile, ProfileStore, resolve_profile
from ..core.errors import ErrorCode, ErrorPhase, VannaError
from ..project import Project

logger = logging.getLogger(__name__)

#: Dialect -> the module and class that runs it. Imported lazily, so a missing
#: driver is an error about *that* dialect rather than an import failure at CLI
#: startup for everyone.
_RUNNERS = {
    "sqlite": ("vanna.integrations.sqlite.sql_runner", "SqliteRunner"),
    "postgres": ("vanna.integrations.postgres.sql_runner", "PostgresRunner"),
    "postgresql": ("vanna.integrations.postgres.sql_runner", "PostgresRunner"),
    "mysql": ("vanna.integrations.mysql.sql_runner", "MySQLRunner"),
    "duckdb": ("vanna.integrations.duckdb.sql_runner", "DuckDBRunner"),
    "bigquery": ("vanna.integrations.bigquery.sql_runner", "BigQueryRunner"),
    "snowflake": ("vanna.integrations.snowflake.sql_runner", "SnowflakeRunner"),
    "clickhouse": ("vanna.integrations.clickhouse.sql_runner", "ClickHouseRunner"),
    "mssql": ("vanna.integrations.mssql.sql_runner", "MSSQLRunner"),
    "oracle": ("vanna.integrations.oracle.sql_runner", "OracleRunner"),
    "presto": ("vanna.integrations.presto.sql_runner", "PrestoRunner"),
    "hive": ("vanna.integrations.hive.sql_runner", "HiveRunner"),
}

#: Dialect -> the distribution to install for it.
#:
#: This used to be derived: the hint was ``pip install 'vanna[{dialect}]'``, which
#: worked only because an extra happened to be named after every dialect. There are
#: no extras now -- nothing is installed, the code is imported from source -- so a
#: hint has to name the driver itself. Spelled out rather than guessed, because
#: ``pip install postgres`` installs an unrelated package and a wrong hint is worse
#: than none.
_DRIVERS = {
    "sqlite": None,  # standard library
    "postgres": "psycopg2-binary",
    "postgresql": "psycopg2-binary",
    "mysql": "PyMySQL",
    "duckdb": "duckdb",
    "bigquery": "google-cloud-bigquery db-dtypes",
    "snowflake": "snowflake-connector-python",
    "clickhouse": "clickhouse_connect",
    "mssql": "pyodbc",
    "oracle": "oracledb",
    "presto": "pyhive thrift",
    "hive": "pyhive thrift",
}

#: Profile keys that name the connection itself rather than a driver argument.
_CONNECTION_KEYS = ("dsn", "connection_string", "url", "database_path", "path")


def build_runner(
    profile: Profile, *, max_rows: int = 1000, timeout_seconds: int = 60
) -> Any:
    """Construct the SQL runner a profile describes.

    Placeholders are resolved here and nowhere earlier, so a credential exists
    only for the lifetime of the connection call.
    """
    entry = _RUNNERS.get(profile.dialect)
    if entry is None:
        raise VannaError(
            ErrorCode.NOT_IMPLEMENTED,
            f"No runner for dialect {profile.dialect!r}.",
            phase=ErrorPhase.CONFIGURATION,
            hint="Supported: " + ", ".join(sorted(set(_RUNNERS))),
        )

    module_name, class_name = entry
    try:
        module = __import__(module_name, fromlist=[class_name])
    except ImportError as exc:
        driver = _DRIVERS.get(profile.dialect)
        raise VannaError(
            ErrorCode.DEPENDENCY_MISSING,
            f"The {profile.dialect} driver is not installed.",
            phase=ErrorPhase.CONFIGURATION,
            hint=(
                f"pip install {driver}"
                if driver
                else "The import failed for a reason other than a missing driver; "
                "see the cause."
            ),
            cause=exc,
        )

    runner_class = getattr(module, class_name)
    settings = profile.resolve()
    policy = ExecutionPolicy(max_rows=max_rows, timeout_seconds=timeout_seconds)

    # SQLite and DuckDB take a path positionally; everything else takes keyword
    # arguments whose names differ per driver, so the profile's keys are passed
    # through and the driver decides what it recognises.
    connection = next(
        (settings.pop(key) for key in _CONNECTION_KEYS if key in settings), None
    )

    try:
        if profile.dialect in ("sqlite", "duckdb"):
            return runner_class(connection or ":memory:", policy=policy, **settings)
        if connection is not None:
            return runner_class(connection, policy=policy, **settings)
        return runner_class(policy=policy, **settings)
    except TypeError as exc:
        # Almost always a profile key the driver does not accept. Name it,
        # rather than surfacing a bare TypeError from deep inside a driver.
        raise VannaError(
            ErrorCode.MISCONFIGURED,
            f"The {profile.dialect} runner rejected these settings: {exc}",
            phase=ErrorPhase.PROFILE_RESOLUTION,
            hint="Check the profile's keys with `vanna profile show`.",
            cause=exc,
        )


def load_context(
    path: Optional[Path] = None,
    *,
    profile_name: Optional[str] = None,
) -> Tuple[Optional[Project], Profile]:
    """Resolve the project and the profile a command should use.

    The project's pinned profile beats the globally active one, so a project
    cannot start querying a different database because someone ran
    ``vanna profile switch`` in another terminal.
    """
    project = Project.find(path)
    profile = resolve_profile(
        profile_name,
        store=ProfileStore(),
        project_profile=project.config.profile if project else None,
    )
    return project, profile


def system_context(tenant_id: str = "default"):
    """A ToolContext for CLI execution.

    The CLI runs as whoever is at the keyboard; there is no request and no
    session. Access rules that need a session property will refuse, which is
    correct -- a command line should not be a way around them.
    """
    import uuid

    from ..core.tool import ToolContext
    from ..core.user import User
    from ..integrations.local.agent_memory.in_memory import DemoAgentMemory

    return ToolContext(
        user=User(id="cli", tenant_id=tenant_id, group_memberships=["user"]),
        conversation_id="cli",
        request_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        agent_memory=DemoAgentMemory(),
    )
