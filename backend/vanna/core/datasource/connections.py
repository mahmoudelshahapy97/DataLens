"""What each database needs in order to be connected to.

One registry, three consumers: the admin console's connection form, the
``vanna docs connection-info`` reference, and the URL/runner construction in the
deployment. Those three had drifted -- the form only ever produced
``postgresql://`` URLs and the deployment only ever built a Postgres runner,
while the library shipped eleven -- and the cause was that the same facts were
written down in three places and only one of them was ever updated.

So the facts live here once:

* which fields an engine needs, and which are optional
* what a sensible default port is
* how those fields become a connection URL
* whether the engine is addressed by host/port, by file path, or by account

**URLs are built here, never in the browser.** A password containing ``@`` or
``/`` produces a URL pointing at the wrong host unless it is percent-encoded
exactly once, and doing it in one function is how that stays true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote


@dataclass(frozen=True)
class Field:
    """One thing a person has to type to connect."""

    name: str
    label: str
    required: bool = False
    default: str = ""
    #: Rendered as ``<input type=...>``. "password" keeps it out of the DOM in
    #: clear text and out of autofill history.
    kind: str = "text"
    help: str = ""


@dataclass(frozen=True)
class Engine:
    """A database we can connect to, and what connecting to it takes."""

    name: str
    label: str
    #: URL scheme, and the prefix the deployment dispatches a runner on.
    scheme: str
    fields: Tuple[Field, ...]
    default_port: Optional[int] = None
    #: How the engine is addressed. The form uses this to decide which shape to
    #: render: a host/port pair, a file path, or something engine-specific.
    shape: str = "host"          # "host" | "file" | "account" | "project"
    notes: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------

    def required_fields(self) -> List[Field]:
        return [f for f in self.fields if f.required]

    def optional_fields(self) -> List[Field]:
        return [f for f in self.fields if not f.required]

    def missing(self, values: Mapping[str, str]) -> List[str]:
        """Required field labels that have not been supplied."""
        return [
            f.label
            for f in self.fields
            if f.required and not str(values.get(f.name) or "").strip()
        ]

    def url(self, values: Mapping[str, str]) -> str:
        """Compose a connection URL from form values.

        Returns "" when a required field is missing rather than emitting a
        half-formed URL -- a connection string with an empty host fails much
        later and much less clearly than "Host is required".
        """
        if self.missing(values):
            return ""

        def get(key: str, fallback: str = "") -> str:
            return str(values.get(key) or fallback).strip()

        if self.shape == "file":
            # No credentials, no host. The path is the whole address.
            return f"{self.scheme}:///{get('path').lstrip('/')}"

        if self.name == "bigquery":
            # Not a network address: a project, and optionally a credentials
            # file the driver reads itself.
            project = quote(get("project"), safe="")
            dataset = quote(get("dataset"), safe="")
            url = f"bigquery://{project}"
            return f"{url}/{dataset}" if dataset else url

        if self.name == "snowflake":
            user = quote(get("username"), safe="")
            password = quote(get("password"), safe="")
            account = quote(get("account"), safe="")
            database = quote(get("database"), safe="")
            url = f"snowflake://{user}:{password}@{account}/{database}"
            extras = {
                k: get(k)
                for k in ("warehouse", "role", "schema")
                if get(k)
            }
            if extras:
                url += "?" + "&".join(
                    f"{k}={quote(v, safe='')}" for k, v in extras.items()
                )
            return url

        if self.name == "presto":
            # Addressed by catalog and schema rather than a single database.
            user = quote(get("username"), safe="")
            password = quote(get("password"), safe="")
            credentials = f"{user}:{password}@" if user and password else (
                f"{user}@" if user else ""
            )
            path = quote(get("catalog", "hive"), safe="")
            schema = get("schema")
            if schema:
                path += f"/{quote(schema, safe='')}"
            return f"presto://{credentials}{get('host')}:{get('port', '443')}/{path}"

        # Everything else is host/port/database with optional credentials.
        user = quote(get("username"), safe="")
        password = quote(get("password"), safe="")
        host = get("host")
        port = get("port", str(self.default_port or ""))
        database = get("database")

        credentials = f"{user}:{password}@" if user else ""
        url = f"{self.scheme}://{credentials}{host}"
        if port:
            url += f":{port}"
        if database:
            url += f"/{quote(database, safe='')}"

        # Only Postgres takes sslmode in the URL; the others reject or ignore it.
        sslmode = get("sslmode")
        if sslmode and self.name == "postgres":
            url += f"?sslmode={quote(sslmode, safe='')}"
        return url


# ----------------------------------------------------------------------
# Common field shapes
# ----------------------------------------------------------------------


def _host_fields(
    *, port: int, user_required: bool = True, database_label: str = "Database"
) -> Tuple[Field, ...]:
    return (
        Field("host", "Host", required=True, help="Hostname or IP."),
        Field("port", "Port", default=str(port)),
        Field("database", database_label, required=True),
        Field("username", "User", required=user_required),
        Field("password", "Password", kind="password"),
    )


#: Every engine with a runner in this package. Keep this in step with
#: ``build_sql_runner`` -- an engine listed here that the deployment cannot build
#: is a form that collects details and then fails.
ENGINES: Dict[str, Engine] = {
    "postgres": Engine(
        name="postgres",
        label="PostgreSQL",
        scheme="postgresql",
        default_port=5432,
        aliases=("postgresql", "pg"),
        fields=_host_fields(port=5432)
        + (
            Field(
                "sslmode",
                "SSL mode",
                help="disable, require, verify-ca, verify-full.",
            ),
        ),
    ),
    "mysql": Engine(
        name="mysql",
        label="MySQL / MariaDB",
        scheme="mysql",
        default_port=3306,
        aliases=("mariadb",),
        fields=_host_fields(port=3306),
    ),
    "mssql": Engine(
        name="mssql",
        label="SQL Server",
        scheme="mssql",
        default_port=1433,
        aliases=("sqlserver",),
        fields=_host_fields(port=1433)
        + (
            Field(
                "driver",
                "ODBC driver",
                default="ODBC Driver 18 for SQL Server",
                help="Must be installed on the server running DataLens.",
            ),
        ),
        notes="Connects through ODBC; the driver must be present in the container.",
    ),
    "oracle": Engine(
        name="oracle",
        label="Oracle",
        scheme="oracle",
        default_port=1521,
        fields=(
            Field("host", "Host", required=True),
            Field("port", "Port", default="1521"),
            Field(
                "database",
                "Service name or SID",
                required=True,
                help="The service name, not a schema.",
            ),
            Field("username", "User", required=True),
            Field("password", "Password", kind="password"),
        ),
    ),
    "clickhouse": Engine(
        name="clickhouse",
        label="ClickHouse",
        scheme="clickhouse",
        default_port=8123,
        fields=_host_fields(port=8123),
    ),
    "snowflake": Engine(
        name="snowflake",
        label="Snowflake",
        scheme="snowflake",
        shape="account",
        fields=(
            Field(
                "account",
                "Account",
                required=True,
                help="e.g. xy12345.eu-west-1 -- not a URL.",
            ),
            Field("username", "User", required=True),
            Field("password", "Password", kind="password"),
            Field("database", "Database", required=True),
            Field("warehouse", "Warehouse", help="Compute warehouse to run on."),
            Field("role", "Role"),
            Field("schema", "Schema"),
        ),
        notes="Key-pair authentication is preferred over a password where available.",
    ),
    "bigquery": Engine(
        name="bigquery",
        label="BigQuery",
        scheme="bigquery",
        shape="project",
        fields=(
            Field("project", "Project ID", required=True),
            Field("dataset", "Dataset"),
            Field(
                "credentials_path",
                "Credentials file",
                help="Path to a service-account JSON, readable by the server. "
                "Leave blank to use the ambient credentials.",
            ),
        ),
        notes="The credentials file is read by the server, so it must exist there.",
    ),
    "hive": Engine(
        name="hive",
        label="Hive",
        scheme="hive",
        default_port=10000,
        fields=_host_fields(port=10000, user_required=False),
    ),
    "presto": Engine(
        name="presto",
        label="Presto / Trino",
        scheme="presto",
        default_port=443,
        aliases=("trino",),
        fields=(
            Field("host", "Host", required=True),
            Field("port", "Port", default="443"),
            Field("catalog", "Catalog", required=True, default="hive"),
            Field("schema", "Schema", default="default"),
            Field("username", "User"),
            Field("password", "Password", kind="password"),
        ),
    ),
    "duckdb": Engine(
        name="duckdb",
        label="DuckDB",
        scheme="duckdb",
        shape="file",
        fields=(
            Field(
                "path",
                "Database file",
                required=True,
                help="Path on the server. Use :memory: for a scratch database.",
            ),
        ),
    ),
    "sqlite": Engine(
        name="sqlite",
        label="SQLite",
        scheme="sqlite",
        shape="file",
        fields=(
            Field(
                "path",
                "Database file",
                required=True,
                help="Path on the server, not on your machine.",
            ),
        ),
    ),
}


def get_engine(name: str) -> Optional[Engine]:
    """Look an engine up by name, alias, or URL scheme."""
    key = (name or "").strip().lower()
    if key in ENGINES:
        return ENGINES[key]
    for engine in ENGINES.values():
        if key == engine.scheme or key in engine.aliases:
            return engine
    return None


def engine_for_url(url: str) -> Optional[Engine]:
    """The engine a connection URL belongs to.

    Used to pick a runner. Matched on the scheme prefix rather than an exact
    equality because drivers append themselves to it -- ``postgresql+psycopg2``,
    ``mysql+pymysql`` -- and the engine is the same either way.
    """
    head = (url or "").split("://", 1)[0].strip().lower()
    if not head:
        return None
    base = head.split("+", 1)[0]
    for engine in ENGINES.values():
        if base == engine.scheme or base in engine.aliases or base == engine.name:
            return engine
    # `postgres://` is a legacy spelling of `postgresql://` and still common.
    if base.startswith("postgres"):
        return ENGINES["postgres"]
    return None


def parse_url(url: str) -> Dict[str, str]:
    """Pull a URL apart into the fields that built it.

    The inverse of :meth:`Engine.url`, and the reason a stored ``database_url``
    can be turned back into constructor arguments for runners that take
    structured parameters rather than a DSN -- MySQL, Snowflake, BigQuery and
    the rest all want host/user/password separately.

    Values are unquoted, so a password that was percent-encoded on the way in
    comes back as the password.
    """
    from urllib.parse import parse_qsl, unquote, urlsplit

    engine = engine_for_url(url)
    if engine is None:
        return {}

    parts = urlsplit(url)
    values: Dict[str, str] = {"engine": engine.name}

    if engine.shape == "file":
        # sqlite:///data/demo.db -> /data/demo.db ; duckdb:///:memory: -> :memory:
        path = (parts.netloc + parts.path) or ""
        values["path"] = unquote(path.lstrip("/")) or ":memory:"
        if values["path"] != ":memory:" and not values["path"].startswith("/"):
            values["path"] = "/" + values["path"]
        return values

    if parts.username:
        values["username"] = unquote(parts.username)
    if parts.password:
        values["password"] = unquote(parts.password)
    if parts.hostname:
        values["host"] = parts.hostname
    if parts.port:
        values["port"] = str(parts.port)

    segments = [unquote(s) for s in parts.path.lstrip("/").split("/") if s]

    if engine.name == "bigquery":
        # bigquery://project/dataset -- the project is in the netloc.
        values["project"] = unquote(parts.hostname or parts.netloc or "")
        values.pop("host", None)
        if segments:
            values["dataset"] = segments[0]
    elif engine.name == "snowflake":
        values["account"] = parts.hostname or ""
        values.pop("host", None)
        if segments:
            values["database"] = segments[0]
    elif engine.name == "presto":
        if segments:
            values["catalog"] = segments[0]
        if len(segments) > 1:
            values["schema"] = segments[1]
    elif segments:
        values["database"] = segments[0]

    for key, value in parse_qsl(parts.query):
        values.setdefault(key, value)

    return values


def describe(engine: Engine) -> Dict[str, object]:
    """A JSON-friendly description, for the form and the CLI reference."""
    return {
        "name": engine.name,
        "label": engine.label,
        "scheme": engine.scheme,
        "shape": engine.shape,
        "default_port": engine.default_port,
        "notes": engine.notes,
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "required": f.required,
                "default": f.default,
                "kind": f.kind,
                "help": f.help,
            }
            for f in engine.fields
        ],
    }


def all_engines() -> List[Dict[str, object]]:
    """Every engine, described, ordered for display."""
    return [describe(e) for e in ENGINES.values()]
