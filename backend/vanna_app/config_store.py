"""Configuration files in the control plane.

Every semantic project, instruction pack and domain definition used to be read
from disk on the path that serves a question. This is where they live instead:
one generic catalog (:data:`SCHEMA`.``config_files``) plus its history
(``config_versions``), behind :class:`PostgresConfigStore`.

Three ideas hold the module together.

**Generic on purpose.** A cube, a compiled manifest and an instruction pack have
nothing structural in common, and a table per kind means every new configuration
file is a migration. Files are identified by their path relative to ``backend/``
and classified by :func:`classify`; a path nobody anticipated lands in ``other``
with its content intact rather than being refused.

**Parsed once.** ``parsed`` is the runtime representation and ``raw_content`` is
the archive. The importer pays the YAML parse; four uvicorn workers rebuilding a
manifest read JSONB. Keeping the raw bytes too is what lets the exporter write a
file back out with its comments, which JSONB alone cannot do.

**Not a second credential store.** ``tenant_datasources`` already holds warehouse
credentials, encrypted. :func:`find_secrets` exists so that configuration
carrying a password is refused at the door rather than quietly becoming a second
place secrets live -- one nobody thinks of as a secret store, and so one nobody
protects like one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger("vanna.config_store")

SCHEMA = "vanna_app"

# ----------------------------------------------------------------------
# Kinds and classification
# ----------------------------------------------------------------------

KIND_DOMAIN = "domain"
KIND_BASELINE = "instruction_baseline"
KIND_PACK = "instruction_pack"
KIND_PROJECT_CONFIG = "project_config"
KIND_CUBE = "cube"
KIND_MODEL = "model"
KIND_MODEL_SQL = "model_sql"
KIND_RELATIONSHIPS = "relationships"
KIND_MANIFEST = "manifest"
KIND_CATALOG = "catalog"
KIND_KNOWLEDGE_RULE = "knowledge_rule"
KIND_KNOWLEDGE_SQL = "knowledge_sql"
KIND_VIEW = "view"
KIND_EVAL_DATASET = "eval_dataset"
KIND_OTHER = "other"

SCOPE_GLOBAL = "global"
SCOPE_TENANT = "tenant"
SCOPE_PROJECT = "project"

#: Extensions worth cataloguing. Everything else under ``backend/`` is code,
#: build output or a lockfile, none of which the runtime reads as configuration.
CONFIG_EXTENSIONS = (".yml", ".yaml", ".json", ".md", ".sql")

#: Directories the walk never descends into. ``vanna/`` is the library -- its
#: YAML is test fixtures and packaging metadata, not this deployment's settings.
SKIP_DIRECTORIES = frozenset(
    {"vanna", "__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}
)


@dataclass(frozen=True)
class Classification:
    """Where a file belongs and what it is."""

    scope: str
    tenant_id: str
    project: str
    kind: str


def normalise_path(relative_path: str) -> str:
    """Forward slashes, no leading ``./``.

    The path is the identity of a row, so it cannot depend on which operating
    system ran the importer. A Windows checkout would otherwise store
    ``projects\\chinook\\cubes\\sales.yml`` and never match the Linux container's
    lookup for the same file.
    """
    return str(relative_path).replace("\\", "/").lstrip("./").strip("/")


def classify(relative_path: str) -> Classification:
    """Where a path relative to ``backend/`` belongs.

    The project rules mirror :class:`vanna.project.layout.ProjectPaths` and
    ``Platform._project_dir_for``: a directory under ``projects/`` is named for
    the workspace it configures, which is what makes ``tenant_id`` a column here
    rather than a lookup somewhere else.
    """
    path = normalise_path(relative_path)
    parts = path.split("/")

    if path == "domains/domains.yml":
        return Classification(SCOPE_GLOBAL, "", "", KIND_DOMAIN)

    if parts[0] == "instructions":
        if path == "instructions/baseline.yml":
            return Classification(SCOPE_GLOBAL, "", "", KIND_BASELINE)
        if len(parts) == 3 and parts[1] == "packs":
            return Classification(SCOPE_GLOBAL, "", "", KIND_PACK)
        return Classification(SCOPE_GLOBAL, "", "", KIND_OTHER)

    if parts[0] == "evals":
        return Classification(SCOPE_GLOBAL, "", "", KIND_EVAL_DATASET)

    if parts[0] == "projects" and len(parts) >= 3:
        name = parts[1]
        rest = parts[2:]
        kind = _project_kind(rest)
        return Classification(SCOPE_PROJECT, name, name, kind)

    return Classification(SCOPE_GLOBAL, "", "", KIND_OTHER)


def _project_kind(rest: Sequence[str]) -> str:
    head = rest[0]
    if len(rest) == 1:
        if head == "vanna_project.yml":
            return KIND_PROJECT_CONFIG
        if head == "relationships.yml":
            return KIND_RELATIONSHIPS
        return KIND_OTHER
    if head == "cubes":
        return KIND_CUBE
    if head == "models":
        return KIND_MODEL_SQL if rest[-1].endswith(".sql") else KIND_MODEL
    if head == "views":
        return KIND_VIEW
    if head == "target":
        return KIND_MANIFEST if rest[-1] == "mdl.json" else KIND_CATALOG
    if head == "knowledge":
        return KIND_KNOWLEDGE_SQL if len(rest) > 1 and rest[1] == "sql" else KIND_KNOWLEDGE_RULE
    return KIND_OTHER


def checksum_of(raw_content: str) -> str:
    """SHA-256 of the content as UTF-8, which is how it is stored and compared."""
    return hashlib.sha256(raw_content.encode("utf-8")).hexdigest()


class ConfigParseError(ValueError):
    """A YAML or JSON configuration file could not be read."""


class ConfigurationUnavailable(RuntimeError):
    """The catalog does not hold configuration this deployment needs.

    Raised rather than falling back to the files on disk. With
    ``VANNA_CONFIG_SOURCE=database`` the catalog is what runs, and a deployment
    whose import failed should say so loudly at the first request rather than
    serve whatever YAML happens to be baked into the image -- which looks healthy
    and is not.
    """


def parse_content(relative_path: str, raw_content: str) -> Optional[Any]:
    """The structured form of a file, or None when it has none.

    Markdown and SQL have no structure to speak of, and returning ``{}`` for them
    would make "no parse" indistinguishable from "an empty mapping". A YAML or
    JSON file that will not parse raises: the importer records the failure and
    stores the file anyway, which is a decision for the caller rather than a
    silent ``None`` here.
    """
    path = normalise_path(relative_path)
    if path.endswith(".json"):
        try:
            return json.loads(raw_content)
        except json.JSONDecodeError as exc:
            raise ConfigParseError(f"{path}: {exc}") from exc
    if path.endswith((".yml", ".yaml")):
        import yaml

        try:
            return yaml.safe_load(raw_content)
        except yaml.YAMLError as exc:
            raise ConfigParseError(f"{path}: {exc}") from exc
    return None


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------

#: Key names that mean "this value is a credential". Matched on the key, not the
#: value, because an instruction that says "never expose the password column" is
#: configuration and a key called ``password`` is not.
SECRET_KEY_RE = re.compile(
    r"(?:^|[_.-])(?:password|passwd|pwd|secret|api_?key|apikey|access_?key|"
    r"auth_?token|token|credential|credentials|private_?key|dsn|"
    r"connection_?string|client_?secret)(?:$|[_.-])",
    re.IGNORECASE,
)

#: A connection URL carrying a username and password, wherever it appears --
#: including in a file with no structure at all, which the key scan cannot see.
SECRET_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@")

REDACTED = "«redacted»"


def find_secrets(parsed: Any, raw_content: str = "") -> List[str]:
    """Every place this file looks like it carries a credential.

    Returns pointers such as ``domains[0].database_url``, empty when the file is
    clean. The caller decides what to do about it; the importer refuses.
    """
    found: List[str] = []

    def walk(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{pointer}.{key}" if pointer else str(key)
                if SECRET_KEY_RE.search(str(key)) and _is_secretish(value):
                    found.append(child)
                    continue
                walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{pointer}[{index}]")
        elif isinstance(node, str) and SECRET_URL_RE.search(node):
            found.append(pointer or "<value>")

    walk(parsed, "")

    if raw_content and SECRET_URL_RE.search(raw_content):
        found.append("<raw content>")

    return found


def _is_secretish(value: Any) -> bool:
    """Whether a value under a secret-looking key is actually worth refusing.

    ``token_budget: 4000`` and ``api_key: ""`` are not credentials. Refusing them
    would make the check something people route around, and a check people route
    around protects nothing.
    """
    if isinstance(value, (bool, int, float)) or value is None:
        return False
    return bool(str(value).strip())


def redact(parsed: Any) -> Any:
    """A copy with every secret-looking value replaced by :data:`REDACTED`."""
    if isinstance(parsed, dict):
        return {
            key: (
                REDACTED
                if SECRET_KEY_RE.search(str(key)) and _is_secretish(value)
                else redact(value)
            )
            for key, value in parsed.items()
        }
    if isinstance(parsed, list):
        return [redact(item) for item in parsed]
    if isinstance(parsed, str) and SECRET_URL_RE.search(parsed):
        return SECRET_URL_RE.sub(lambda m: m.group(0).split("://")[0] + "://" + REDACTED + "@", parsed)
    return parsed


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


@dataclass
class ConfigRecord:
    """One catalogued file."""

    relative_path: str
    raw_content: str
    #: Empty means "work it out from the path", which is what almost every caller
    #: wants. Defaulting to ``global`` instead was a trap: a record built for
    #: ``projects/demo/cubes/sales.yml`` without naming its scope would be stored
    #: as platform-wide content, and the runtime -- which looks it up *by* the
    #: classification -- would never find it again.
    scope: str = ""
    tenant_id: str = ""
    project: str = ""
    kind: str = ""
    extension: str = ""
    parsed: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    checksum: str = ""
    version: int = 0
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        self.relative_path = normalise_path(self.relative_path)
        if not self.scope:
            where = classify(self.relative_path)
            self.scope = where.scope
            self.tenant_id = self.tenant_id or where.tenant_id
            self.project = self.project or where.project
            self.kind = self.kind or where.kind
        if not self.kind:
            self.kind = KIND_OTHER
        if not self.checksum:
            self.checksum = checksum_of(self.raw_content)
        if not self.extension:
            _, _, tail = self.relative_path.rpartition(".")
            self.extension = f".{tail}" if tail and tail != self.relative_path else ""

    @classmethod
    def from_file(
        cls,
        relative_path: str,
        raw_content: str,
        *,
        parsed: Any = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ConfigRecord":
        """A record for a file read off disk. Scope and kind come from the path."""
        return cls(
            relative_path=relative_path,
            raw_content=raw_content,
            parsed=parsed,
            metadata=dict(metadata or {}),
        )


class StoredProject:
    """A semantic project whose settings came from the catalog, not a directory.

    ``Project`` is a root path plus a config, and in the catalog there is no root
    path -- so this carries only the half the runtime actually reads.
    ``_build_registry`` wants ``config.fanout_guard`` and
    ``config.extra['session_properties']``; nothing downstream of it touches
    ``paths``, and a synthetic root that resolved against the current directory
    would be a lie waiting for somebody to follow it.
    """

    __slots__ = ("config", "tenant_id", "source")

    def __init__(self, config: Any, tenant_id: str) -> None:
        self.config = config
        self.tenant_id = tenant_id
        self.source = "database"

    @classmethod
    def from_record(cls, record: "ConfigRecord") -> "StoredProject":
        """Validate a stored ``vanna_project.yml`` into a project.

        Through ``ProjectConfig.from_dict``, which is what reading the file uses,
        so an unknown ``schema_version`` or a missing name is refused here exactly
        as it is there.
        """
        from pathlib import Path as _Path

        from vanna.project.loader import ProjectConfig

        config = ProjectConfig.from_dict(
            dict(record.parsed or {}), source=_Path(record.relative_path)
        )
        return cls(config, record.tenant_id)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StoredProject {self.config.name!r} for {self.tenant_id!r}>"


def _row_to_record(row: Dict[str, Any]) -> ConfigRecord:
    return ConfigRecord(
        id=row.get("id"),
        relative_path=row["relative_path"],
        raw_content=row.get("raw_content") or "",
        # From the row, not from the path: a file reclassified by a later version
        # of `classify` must still be readable at the scope it was written under.
        scope=row.get("scope") or SCOPE_GLOBAL,
        tenant_id=row.get("tenant_id") or "",
        project=row.get("project") or "",
        kind=row.get("kind") or KIND_OTHER,
        extension=row.get("extension") or "",
        parsed=row.get("parsed"),
        metadata=dict(row.get("metadata") or {}),
        checksum=row.get("checksum") or "",
        version=int(row.get("version") or 0),
        updated_by=row.get("updated_by"),
        updated_at=row.get("updated_at"),
    )


_COLUMNS = (
    "id, scope, tenant_id, project, relative_path, kind, extension, checksum, "
    "raw_content, parsed, metadata, version, updated_by, imported_at, updated_at"
)


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


class PostgresConfigStore:
    """Read and write the configuration catalog.

    Writes go through ``AppDatabase.transact`` so they take a pool slot like every
    other store, and so a file and its history row are one transaction -- a
    version row missing for a change that happened is worse than no history.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    # -- reads ---------------------------------------------------------

    async def get(
        self,
        relative_path: str,
        *,
        scope: Optional[str] = None,
        tenant_id: str = "",
        project: str = "",
    ) -> Optional[ConfigRecord]:
        path = normalise_path(relative_path)
        if scope is None:
            where = classify(path)
            scope, tenant_id, project = where.scope, where.tenant_id, where.project
        row = await self.db.fetch_one(
            f"SELECT {_COLUMNS} FROM {SCHEMA}.config_files "
            "WHERE scope = %s AND tenant_id = %s AND project = %s AND relative_path = %s",
            (scope, tenant_id, project, path),
        )
        return _row_to_record(row) if row else None

    async def list(
        self,
        *,
        kind: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
        scope: Optional[str] = None,
        tenant_id: Optional[str] = None,
        project: Optional[str] = None,
        with_content: bool = True,
    ) -> List[ConfigRecord]:
        """Rows matching every filter given, ordered by path.

        ``with_content=False`` leaves ``raw_content`` out of the result. A listing
        screen wants forty paths and checksums, not forty manifests, and one of
        those manifests is 300 KB.
        """
        columns = _COLUMNS if with_content else _COLUMNS.replace("raw_content, ", "")
        clauses: List[str] = []
        params: List[Any] = []
        wanted = [k for k in ([kind] if kind else list(kinds or [])) if k]
        if wanted:
            clauses.append("kind = ANY(%s)")
            params.append(wanted)
        if scope is not None:
            clauses.append("scope = %s")
            params.append(scope)
        if tenant_id is not None:
            clauses.append("tenant_id = %s")
            params.append(tenant_id)
        if project is not None:
            clauses.append("project = %s")
            params.append(project)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self.db.fetch_all(
            f"SELECT {columns} FROM {SCHEMA}.config_files {where} ORDER BY relative_path",
            tuple(params),
        )
        return [_row_to_record(row) for row in rows or []]

    async def history(self, record_id: int, *, limit: int = 20) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"""SELECT version, checksum, source, created_by, note, created_at
                  FROM {SCHEMA}.config_versions
                 WHERE config_file_id = %s
                 ORDER BY version DESC LIMIT %s""",
            (record_id, limit),
        )
        return list(rows or [])

    async def fingerprint(self) -> Tuple[int, str, int]:
        """A cheap value that changes whenever any configuration does.

        One aggregate over the whole table rather than one per workspace. This
        runs four times -- once per uvicorn worker -- every refresh window, so it
        has to be cheap; and configuration writes are rare enough that
        invalidating a little too much costs nothing.

        The sum of versions is in there because ``count`` and ``max(updated_at)``
        can both stay put across an edit-and-revert inside one clock tick.
        """
        row = await self.db.fetch_one(
            f"""SELECT count(*)::bigint AS n,
                       coalesce(max(updated_at), 'epoch'::timestamptz)::text AS at,
                       coalesce(sum(version), 0)::bigint AS v
                  FROM {SCHEMA}.config_files"""
        )
        if not row:
            return (0, "", 0)
        return (int(row["n"]), str(row["at"]), int(row["v"]))

    # -- writes --------------------------------------------------------

    async def put(
        self,
        record: ConfigRecord,
        *,
        source: str = "import",
        actor: Optional[str] = None,
        note: Optional[str] = None,
        project_into: Optional[Callable[[Any, ConfigRecord], None]] = None,
    ) -> str:
        """Store a file. Returns ``created``, ``updated`` or ``unchanged``.

        Idempotent by checksum: re-importing a file nobody touched writes nothing,
        moves no timestamp and adds no history. That is what makes running the
        importer on every boot a reasonable thing to do.

        ``project_into`` runs inside the same transaction, and is how derived
        tables stay consistent with the catalog -- see
        :mod:`vanna_app.config_projection`. A write that updated the catalog and
        left the projection behind would put the two out of step with nothing to
        say which one is right.
        """
        outcome: Dict[str, str] = {}

        def run(cursor: Any) -> None:
            outcome["result"] = apply_put(
                cursor, record, source=source, actor=actor, note=note,
                project_into=project_into,
            )

        await self.db.transact(run)
        return outcome["result"]

    # -- boot and CLI paths --------------------------------------------
    #
    # The importer runs before there is an event loop -- from `create_app`, and
    # from `backend/tools/` -- and it runs *once*, over a few dozen small files. These
    # take the synchronous route through `AppDatabase` for that reason.
    #
    # It matters more than it looks: an `asyncio.Semaphore` binds itself to the
    # first loop that awaits it, so a boot-time `asyncio.run()` around the async
    # methods would poison the gate for the loop uvicorn later starts. Sharing the
    # statements and not the plumbing is what keeps one description of the write.

    def put_sync(
        self,
        record: ConfigRecord,
        *,
        source: str = "import",
        actor: Optional[str] = None,
        note: Optional[str] = None,
        project_into: Optional[Callable[[Any, ConfigRecord], None]] = None,
    ) -> str:
        """:meth:`put`, without an event loop. Same statements, same transaction."""
        with self.db.transaction() as connection:
            with connection.cursor() as cursor:
                return apply_put(
                    cursor, record, source=source, actor=actor, note=note,
                    project_into=project_into,
                )

    def get_sync(
        self,
        relative_path: str,
        *,
        scope: Optional[str] = None,
        tenant_id: str = "",
        project: str = "",
    ) -> Optional[ConfigRecord]:
        path = normalise_path(relative_path)
        if scope is None:
            where = classify(path)
            scope, tenant_id, project = where.scope, where.tenant_id, where.project
        row = self.db.run_sync(
            f"SELECT {_COLUMNS} FROM {SCHEMA}.config_files "
            "WHERE scope = %s AND tenant_id = %s AND project = %s "
            "AND relative_path = %s",
            (scope, tenant_id, project, path),
            fetch="one",
        )
        return _row_to_record(row) if row else None

    def list_sync(
        self,
        *,
        kinds: Optional[Iterable[str]] = None,
        with_content: bool = True,
    ) -> List[ConfigRecord]:
        columns = _COLUMNS if with_content else _COLUMNS.replace("raw_content, ", "")
        wanted = [k for k in (kinds or []) if k]
        where = "WHERE kind = ANY(%s)" if wanted else ""
        rows = self.db.run_sync(
            f"SELECT {columns} FROM {SCHEMA}.config_files {where} "
            "ORDER BY relative_path",
            (wanted,) if wanted else (),
            fetch="all",
        )
        return [_row_to_record(row) for row in rows or []]

    async def delete(
        self,
        relative_path: str,
        *,
        scope: Optional[str] = None,
        tenant_id: str = "",
        project: str = "",
    ) -> bool:
        path = normalise_path(relative_path)
        if scope is None:
            where = classify(path)
            scope, tenant_id, project = where.scope, where.tenant_id, where.project
        removed = await self.db.execute(
            f"DELETE FROM {SCHEMA}.config_files "
            "WHERE scope = %s AND tenant_id = %s AND project = %s AND relative_path = %s",
            (scope, tenant_id, project, path),
        )
        return bool(removed)


def apply_put(
    cursor: Any,
    record: ConfigRecord,
    *,
    source: str = "import",
    actor: Optional[str] = None,
    note: Optional[str] = None,
    project_into: Optional[Callable[[Any, ConfigRecord], None]] = None,
) -> str:
    """Write one file and its version row on an open cursor.

    Returns ``created``, ``updated`` or ``unchanged``. Mutates ``record`` with the
    id and version it ended up with, so a caller can record history against it.

    ``SELECT ... FOR UPDATE`` rather than an upsert: the version number is
    ``version + 1`` of what is there, and two concurrent writers reading the same
    row would both compute the same next version and one would lose its history
    row to the unique constraint.
    """
    if source not in ("import", "api"):
        raise ValueError(f"unknown source {source!r}")

    cursor.execute(
        f"""SELECT id, checksum, version FROM {SCHEMA}.config_files
             WHERE scope = %s AND tenant_id = %s AND project = %s
               AND relative_path = %s
               FOR UPDATE""",
        (record.scope, record.tenant_id, record.project, record.relative_path),
    )
    existing = cursor.fetchone()

    if existing and existing[1] == record.checksum:
        record.id, record.version = int(existing[0]), int(existing[2])
        return "unchanged"

    if existing:
        cursor.execute(
            f"""UPDATE {SCHEMA}.config_files
                   SET kind = %s, extension = %s, checksum = %s,
                       raw_content = %s, parsed = %s::jsonb,
                       metadata = %s::jsonb, version = version + 1,
                       updated_by = %s, updated_at = now()
                 WHERE id = %s
             RETURNING id, version""",
            (
                record.kind,
                record.extension,
                record.checksum,
                record.raw_content,
                _jsonb(record.parsed),
                json.dumps(record.metadata or {}),
                actor,
                existing[0],
            ),
        )
        result = "updated"
    else:
        cursor.execute(
            f"""INSERT INTO {SCHEMA}.config_files
                    (scope, tenant_id, project, relative_path, kind, extension,
                     checksum, raw_content, parsed, metadata, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
             RETURNING id, version""",
            (
                record.scope,
                record.tenant_id,
                record.project,
                record.relative_path,
                record.kind,
                record.extension,
                record.checksum,
                record.raw_content,
                _jsonb(record.parsed),
                json.dumps(record.metadata or {}),
                actor,
            ),
        )
        result = "created"

    row = cursor.fetchone()
    record.id, record.version = int(row[0]), int(row[1])

    cursor.execute(
        f"""INSERT INTO {SCHEMA}.config_versions
                (config_file_id, version, checksum, raw_content, parsed,
                 source, created_by, note)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)""",
        (
            record.id,
            record.version,
            record.checksum,
            record.raw_content,
            _jsonb(record.parsed),
            source,
            actor,
            note,
        ),
    )

    if project_into is not None:
        project_into(cursor, record)

    return result


def _jsonb(value: Any) -> Optional[str]:
    """JSON text for a jsonb parameter, or None for SQL NULL.

    ``None`` and ``'null'::jsonb`` are different things here: the first means "no
    structured form", the second means "the file said null".
    """
    if value is None:
        return None
    return json.dumps(value)


# ----------------------------------------------------------------------
# The cache
# ----------------------------------------------------------------------


class ConfigCache:
    """Built configuration objects, held until the database says otherwise.

    ``load_project`` runs on every cold runtime build, so reading and validating a
    300 KB manifest per build is not free. What makes this more than a dictionary
    is the invalidation: the API runs four uvicorn worker processes, so a write in
    one of them is invisible to the other three, and an in-process ``invalidate()``
    call would leave three workers serving the old cube indefinitely.

    So the cache revalidates against a database fingerprint instead -- one cheap
    aggregate, at most once per ``refresh_seconds``. Freshness is bounded by that
    window rather than by which worker took the write, which is the property a
    multi-process deployment actually needs. It is the same shape as the
    fingerprint cache in ``vanna.integrations.local.markdown_knowledge``.
    """

    def __init__(
        self,
        store: Optional[PostgresConfigStore],
        *,
        refresh_seconds: float = 5.0,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._store = store
        self._refresh = max(0.0, float(refresh_seconds))
        self._on_change = on_change
        self._entries: Dict[Any, Any] = {}
        self._fingerprint: Optional[Tuple[int, str, int]] = None
        self._checked_at = 0.0
        self._generation = 0
        self._lock = asyncio.Lock()

    async def get(self, key: Any, build: Callable[[], Awaitable[Any]]) -> Any:
        """The cached value for ``key``, building it if the cache is cold.

        Misses are cached too, as whatever ``build`` returned -- "this workspace
        has no semantic project" is the common answer and is worth not asking the
        database about on every single runtime build.
        """
        await self._revalidate()
        async with self._lock:
            if key in self._entries:
                return self._entries[key]
            generation = self._generation
        value = await build()
        async with self._lock:
            # A build that started before an invalidation is describing the old
            # content; storing it would reinstate exactly what was just dropped.
            if generation == self._generation:
                self._entries[key] = value
        return value

    async def _revalidate(self) -> None:
        if self._store is None:
            return
        now = time.monotonic()
        if self._fingerprint is not None and (now - self._checked_at) < self._refresh:
            return
        async with self._lock:
            # Re-check under the lock: a burst of concurrent builds should cost
            # one fingerprint query between them, not one each.
            now = time.monotonic()
            if self._fingerprint is not None and (now - self._checked_at) < self._refresh:
                return
            self._checked_at = now
            try:
                current = await self._store.fingerprint()
            except Exception as exc:  # noqa: BLE001
                # A failed probe must not drop the cache: the fallback would be
                # rebuilding every manifest on every request against a database
                # that is already unhappy.
                logger.warning("Could not check the configuration fingerprint: %s", exc)
                return
            if self._fingerprint is None:
                self._fingerprint = current
                return
            if current != self._fingerprint:
                logger.info(
                    "Configuration changed (%s -> %s); rebuilding cached projects",
                    self._fingerprint, current,
                )
                self._fingerprint = current
                self._entries.clear()
                self._generation += 1
                changed = self._on_change
            else:
                changed = None
        if changed is not None:
            changed()

    def clear(self) -> None:
        """Forget everything, without waiting for the next fingerprint check."""
        self._entries.clear()
        self._generation += 1
        self._fingerprint = None
        self._checked_at = 0.0
