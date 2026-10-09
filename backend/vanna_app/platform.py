"""The per-tenant agent cache, and everything bound to one workspace's data source.

One ``Agent`` is built **per tenant**, lazily, and cached -- because the SQL runner,
and therefore every tool in the registry, is bound to a connection at construction
time. Sharing one agent across tenants would mean sharing one database connection
across tenants, which is not a thing you can patch up later at the prompt layer.

Two changes make that cache safe to run at scale.

**Per-tenant locks.** Construction was serialised behind a single ``asyncio.Lock``,
and the work inside it opens a connection and probes ``information_schema`` -- so one
workspace pointed at an unreachable warehouse blocked the *first request of every
other workspace* for the length of a connect timeout. Each tenant now waits only for
itself.

**Bounded, with eviction.** The cache never evicted, and every entry holds a
connection pool. Five hundred workspaces meant five hundred pools in one process,
which is a slow leak that presents as a mystery. It is now an LRU with an idle TTL,
and eviction closes the runner rather than dropping it for the garbage collector to
find.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config_store import ConfigCache
from .observability import get_metrics
from .secrets import Secret
from .tenancy import describe_data_source

logger = logging.getLogger("vanna.platform")

#: Carries the question from the lifecycle hook that sees it to the tool that
#: records it, several LLM turns later. A ContextVar is the right primitive: the
#: agent runs one request per asyncio task, so the value a hook sets is visible to
#: the tools of that request and to no other.
_CURRENT_QUESTION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "vanna_current_question", default=""
)


def current_question() -> str:
    return _CURRENT_QUESTION.get("")


def _question_for(context: Any) -> str:
    """What to record as the question behind this SQL.

    A chat turn has one, captured by the lifecycle hook. Everything else that
    runs SQL through this tool does not -- a dashboard tile, a scheduled report,
    a cube drill -- and those were stored with an empty question. They are the
    *majority*: of 6,340 rows in one workspace, 6,075 had none, so the History
    screen ("every question asked in this workspace") was mostly page after page
    of "(no question recorded)" and the real questions were buried in it.

    So label them by where they came from. The conversation id already says:
    ``dashboard:<id>`` for a tile, ``export`` for a download. This follows the
    convention the export path set with ``[export] <title>`` rather than
    inventing a second one, and it keeps them in the record -- they cost tokens
    and touch data, so dropping them would be worse than labelling them.
    """
    question = current_question()
    if question:
        return question

    conversation = str(getattr(context, "conversation_id", "") or "")
    if conversation.startswith("dashboard:"):
        return f"[dashboard] {conversation.split(':', 1)[1]}"
    if conversation.startswith("report:"):
        return f"[report] {conversation.split(':', 1)[1]}"
    if conversation in ("export", "domains", "portal"):
        return f"[{conversation}]"
    return ""


def _usage_fields() -> Dict[str, Any]:
    """Model, tokens and cost for the request being recorded, or nothing.

    Empty when the provider reported no usage (the mock service, or a model
    whose response carries none) -- the columns then stay NULL, which is
    honest, rather than becoming a confident zero.
    """
    from .llm import consume_usage

    usage = consume_usage()
    if not usage:
        return {}
    return {
        "model": usage["model"] or None,
        "prompt_tokens": usage["prompt_tokens"] or None,
        "completion_tokens": usage["completion_tokens"] or None,
        "cost_usd": usage["cost_usd"],
    }


def question_capture_hook() -> Any:
    """Lifecycle hook that remembers the question being answered."""
    from vanna.core.lifecycle import LifecycleHook

    class QuestionCaptureHook(LifecycleHook):
        async def before_message(self, user: Any, message: str) -> Optional[str]:
            _CURRENT_QUESTION.set(message or "")
            # Zero the usage total too: without this, a second question in the
            # same task would be charged the first one's tokens as well.
            from .llm import reset_usage

            reset_usage()
            return None  # never modifies the message

    return QuestionCaptureHook()


# ----------------------------------------------------------------------
# The recording run_sql tool
# ----------------------------------------------------------------------


def recording_run_sql_tool(
    sql_runner: Any,
    generation_store: Any,
    data_source: str,
    *,
    admin_audit: Any = None,
) -> Any:
    """``RunSqlTool`` that records every execution.

    Wrapping the tool rather than post-processing the stream is what makes the
    record trustworthy: this sees the exact SQL that reached the database and the
    exact outcome, including executions that failed and ones the repair strategy
    retried -- none of which necessarily appear in the answer the user reads.

    This used to also gate writes, by parsing each statement, previewing DML, and
    waiting for the model to call again with ``confirm=true``. That gate never
    worked: ``RunSqlToolArgs`` declares only ``sql``, so Pydantic dropped the
    ``confirm`` argument during validation and the flag never arrived -- meaning
    a write-enabled workspace previewed every statement forever and executed
    none. It has been replaced rather than repaired, because the shape was wrong
    as well as broken: a single tool that can both propose and confirm lets one
    inference do both. Writes now go through ``propose_write``/``confirm_write``,
    which take a typed plan, build the statement themselves, and check it against
    per-table grants. This tool is read-only again, and the policy above it says so.
    """
    from vanna.core.generation import GenerationStatus, SqlGeneration
    from vanna.core.tool import ToolContext, ToolResult
    from vanna.tools import RunSqlTool

    class RecordingRunSqlTool(RunSqlTool):
        async def execute(self, context: ToolContext, args: Any) -> ToolResult:
            result = await super().execute(context, args)
            try:
                await self._record(context, args, result)
            except Exception as exc:
                # Bookkeeping must never break the answer.
                logger.debug("Generation not recorded: %s", exc)
            return result

        # -- lineage ---------------------------------------------------

        async def _record(self, context: ToolContext, args: Any, result: Any) -> None:
            meta = result.metadata or {}
            row_count = meta.get("row_count")

            if not result.success:
                status = GenerationStatus.INVALID
            elif row_count == 0:
                status = GenerationStatus.EMPTY
            else:
                status = GenerationStatus.VALID

            get_metrics().questions.labels(
                getattr(context, "tenant_id", "default"),
                str(getattr(status, "value", status)),
            ).inc()

            if meta.get("execution_ms") is not None:
                get_metrics().sql_seconds.labels(
                    getattr(self.sql_runner, "dialect", "unknown")
                ).observe(float(meta["execution_ms"]) / 1000.0)

            await generation_store.record(
                context,
                SqlGeneration(
                    tenant_id=context.tenant_id,
                    data_source_id=data_source,
                    user_id=getattr(context.user, "id", ""),
                    conversation_id=context.conversation_id,
                    request_id=context.request_id,
                    question=_question_for(context),
                    sql=getattr(args, "sql", "") or "",
                    status=status,
                    error=(result.error or None),
                    row_count=row_count,
                    truncated=bool(meta.get("truncated")),
                    execution_ms=meta.get("execution_ms"),
                    # What answering this cost. The tool knows the SQL and its
                    # outcome but not the model's price, so the metering
                    # middleware leaves its running total on a ContextVar and
                    # this reads it -- exactly how `current_question()` gets
                    # here from the lifecycle hook.
                    #
                    # It has to be pulled in at *write* time rather than pushed
                    # from the middleware: the LLM answers before the agent
                    # calls this tool, so an UPDATE from there would run against
                    # a row that does not exist yet.
                    **_usage_fields(),
                ),
            )

    return RecordingRunSqlTool(sql_runner=sql_runner)


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------


def _install_turn_nodes(agent: Any, llm: Any, settings: Any) -> None:
    """Extend the agent's turn graph with the optional reasoning steps.

    Done after construction rather than by passing ``turn_graph=``, because the
    three built-in nodes are bound methods on the agent -- there is no graph to
    extend until it exists.

    Both nodes cost a model call when they fire, and those calls are metered
    and billed like any other. Neither is wired unless its setting says so.
    """
    from vanna.core.agent.graph import TurnGraph

    from .agent_nodes import CriticNode, PlannerNode

    critic = (
        CriticNode(llm, max_retries=settings.max_critic_retries)
        if settings.enable_critic and settings.max_critic_retries > 0
        else None
    )
    planner = PlannerNode(llm) if settings.enable_planner else None
    if critic is None and planner is None:
        return

    base = agent._default_turn_graph()
    nodes = list(base.nodes)
    edges = base.edges

    if critic is not None:
        nodes.append(critic)
        # `llm_turn` sends a finished answer to `agent.answer_node`, so pointing
        # that at the critic is what puts it on the edge. Its own static edge is
        # where a critic out of retries falls through to.
        agent.answer_node = critic.name
        edges[critic.name] = "answer"

    entry = base.entry
    if planner is not None:
        nodes.append(planner)
        entry = planner.name
        edges[planner.name] = base.entry

    agent.turn_graph = TurnGraph(nodes, entry=entry, edges=edges)


def policy_for_user(read_only: Any, settings: Any, tenant: Dict[str, Any]) -> Optional[Any]:
    """Resolve the SQL policy for one caller.

    Always read-only now, for every caller including an admin of a
    write-enabled workspace.

    This used to hand admins a ``read_write`` policy so ``run_sql`` could carry
    DML. That is no longer how a change is made: writes go through
    ``propose_write``, which takes a typed plan and builds the statement itself,
    and never through a model-authored SQL string. Leaving the loophole open
    would mean two paths to the same effect with only one of them checked
    against table grants -- so the loophole is closed and this returns None,
    meaning "use the registry's read-only policy", for everyone.
    """
    return None


def _with_value_resolution(
    enhancer: Any, *, catalog: Any, store: Any, data_source: str
) -> Any:
    """Tell the model how the values in a question are actually spelled.

    The commonest cause of a wrong-but-valid query is an invented literal --
    `WHERE status = 'active'` against a column storing `'ACTIVE'`. It parses,
    validates, passes every permission check and returns nothing, which reads to
    a user as "it doesn't work" rather than as a near miss.
    """
    from vanna.capabilities.values.enhancer import ValueResolvingEnhancer

    return ValueResolvingEnhancer(
        store=store,
        catalog=catalog,
        data_source_id=data_source,
        inner=enhancer,
    )


def _with_write_context(enhancer: Any, write_service: Any) -> Any:
    """Tell the model which tables it may change, when any of them are.

    Without this the boundary is discoverable only by proposing something and
    being refused -- which works, but spends a turn and shows the user a
    refusal they did not need to see.
    """
    if write_service is None:
        return enhancer
    from vanna.core.write.prompt import WriteAwareEnhancer

    return WriteAwareEnhancer(write_service, inner=enhancer)


def _with_domain_context(
    enhancer: Any, store: Any, *, tenant_id: str, data_source_id: str
) -> Any:
    """Put the workspace's business vocabulary in the prompt.

    Outermost of the three, so the glossary reads after the schema and the write
    rules rather than before them.
    """
    if store is None:
        return enhancer
    from .domain_prompt import DomainContextEnhancer

    return DomainContextEnhancer(
        store, tenant_id=tenant_id, data_source_id=data_source_id, inner=enhancer
    )


# ----------------------------------------------------------------------
# Runtimes
# ----------------------------------------------------------------------


class TenantRuntime:
    """One workspace's agent and everything bound to its data source."""

    __slots__ = (
        "tenant_id", "data_source", "dialect", "runner", "catalog", "policy",
        "agent", "handler", "built_at", "last_used", "write_service", "retired",
    )

    def __init__(
        self,
        *,
        tenant_id: str,
        dialect: str,
        runner: Any,
        catalog: Any,
        policy: Any,
        agent: Any,
        handler: Any,
        data_source: str,
        write_service: Any = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.dialect = dialect
        self.runner = runner
        self.catalog = catalog
        self.policy = policy
        self.agent = agent
        self.handler = handler
        self.data_source = data_source
        self.write_service = write_service
        self.built_at = time.monotonic()
        self.last_used = self.built_at
        #: True once evicted. The object stays usable -- whoever still holds it is
        #: mid-request -- it simply is not in the cache any more.
        self.retired = False

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def retire(self) -> None:
        """Stop being cached, without closing anything.

        The eviction path calls this instead of :meth:`close`. Closing on eviction
        was a use-after-close: a runtime leaves the LRU the moment a newer one
        arrives, which can be while one of its own requests is still running, and
        that request then fails with "connection pool is closed" -- observed as a
        400 on `/run-sql` under load, sharing its request id with the warning.

        Dropping the reference is enough. The runner closes its pool in `__del__`,
        so the connections go back exactly when the last user lets go, which is
        the guarantee a grace period can only approximate.
        """
        self.retired = True

    def close(self) -> None:
        """Release the warehouse connection this runtime holds, now.

        For shutdown, where there are no in-flight requests to strand. Anywhere
        else, prefer :meth:`retire`.
        """
        for attribute in ("close", "dispose", "shutdown"):
            method = getattr(self.runner, attribute, None)
            if callable(method):
                try:
                    method()
                    return
                except Exception as exc:  # pragma: no cover - teardown only
                    logger.debug("Closing runner for %s: %s", self.tenant_id, exc)
                    return


def build_sql_runner(
    settings: Any, database_url: str, *, max_rows: int, read_only: bool = True
) -> Any:
    """Build the SQL runner, with guardrails, for one connection string.

    ``read_only`` opens the connection in a read-only transaction, which is the last
    of three independent layers between a question and a write: the per-user SQL
    policy, the statement allow-list, and this. It is the only one the application
    cannot talk its way past, so it is left on unless the workspace has explicitly
    been granted writes.
    """
    from vanna.capabilities.sql_runner import ExecutionPolicy
    from vanna.core.datasource.runners import UnsupportedDataSource, build_runner

    policy = ExecutionPolicy(max_rows=max_rows, timeout_seconds=settings.query_timeout)

    if not (database_url or "").strip():
        # The built-in demo database: the zero-configuration path that makes
        # `docker compose up` work with an empty .env.
        from vanna.integrations.sqlite import SqliteRunner

        from .demo import seed_demo_database

        seed_demo_database(settings.sqlite_path)
        return SqliteRunner(settings.sqlite_path, policy=policy, read_only=True)

    try:
        return build_runner(
            database_url,
            policy=policy,
            read_only=read_only,
            # Sized from the connection budget rather than the driver's default:
            # this number is multiplied by the number of cached runtimes and again
            # by the worker count, which is how five became several hundred.
            pool_max=settings.warehouse_pool_max,
            pool_min=settings.warehouse_pool_min,
        )
    except UnsupportedDataSource:
        # Deliberately not a fall back to SQLite. That is what this did before, and
        # it meant a workspace pointed at `mysql://...` quietly answered from the
        # demo database -- plausible numbers from the wrong data.
        raise


class Platform:
    """Owns the shared services and the per-tenant runtime cache.

    Shared across tenants: the LLM client, the knowledge stores, the catalog and the
    generation store -- all tenant-scoped internally, by contract, on
    ``ToolContext.tenant_id``.

    Not shared: the SQL runner and the tool registry built around it. Those hold a
    connection to one workspace's database.
    """

    def __init__(
        self,
        settings: Any,
        *,
        directory: Any,
        generation_store: Any,
        conversation_store: Any = None,
        dashboard_store: Any = None,
        counters: Any = None,
        audit_logger: Any = None,
        admin_audit: Any = None,
        billing: Any = None,
        grant_store: Any = None,
        write_approval_store: Any = None,
        value_store: Any = None,
        instruction_store: Any = None,
        instruction_library: Any = None,
        grant_policy_store: Any = None,
        catalog_factory: Any = None,
        domain_store: Any = None,
        datasource_registry: Any = None,
        config_store: Any = None,
        #: The control plane itself, for the stores this builds rather than
        #: receives. Agent memory is the only one so far.
        app_db: Any = None,
    ) -> None:
        from vanna.core.generation import LocalGenerationStore
        from vanna.core.llm import DelegatingLlmService
        from vanna.integrations.local import (
            LocalSchemaCatalog,
            MarkdownExampleStore,
            MarkdownInstructionStore,
        )

        from .llm import build_llm_service

        self.settings = settings
        self.directory = directory
        self.billing = billing
        self.counters = counters
        self.audit_logger = audit_logger
        self.admin_audit = admin_audit
        # Optional: without a grant store nothing is writable, which is the
        # correct default rather than a degraded one.
        self.grants = grant_store
        # Business domains. None without a control plane, which the routes
        # report as 503 rather than pretending to store anything.
        self.domains = domain_store
        # Which databases each workspace may query. None without a control
        # plane, in which case `resolve_source` falls back to the single
        # `tenants.database_url` -- the pre-registry behaviour.
        self.datasources = datasource_registry
        # Optional. Without it there is nowhere to record a default, so presets
        # and read enforcement are simply unavailable rather than half-wired.
        self.grant_policies = grant_policy_store
        self.write_approvals = write_approval_store
        # Optional. Without it, value hints fall back to whatever
        # low-cardinality values the scanner already recorded.
        self.values = value_store
        self.user_resolver: Any = None

        settings.data_dir.mkdir(parents=True, exist_ok=True)

        # Wrapped so a request can redirect LLM calls to the caller's own key. The
        # agent binds its service once at construction and agents are cached per
        # tenant, so this indirection is the only place the swap can happen.
        self.llm = DelegatingLlmService(build_llm_service(settings))

        # Ranked retrieval. `lexical` is dependency-free BM25 and the default; naming
        # a vector integration fuses it with lexical via RRF. A backend that cannot
        # be built downgrades *loudly* -- a silent fall back to keyword search is an
        # unclosable "accuracy dropped after the deploy" ticket.
        from vanna.capabilities.index import resolve_index

        self.index = resolve_index(settings.index_backend)
        logger.info("Retrieval index: %s", self.index.name)

        # A factory rather than a built store, because the index is resolved here
        # and the catalog needs it at construction. The control-plane catalog is
        # preferred wherever there is a control plane: the JSON file has no
        # row-level tenant isolation and cannot be shared by two replicas.
        self.catalog = (
            catalog_factory(self.index)
            if catalog_factory is not None
            else LocalSchemaCatalog(
                str(settings.data_dir / "catalog.json"), index=self.index
            )
        )
        # No `dialect=`: one store now serves tenants on different databases, so
        # there is no single dialect to validate against. The per-tenant dialect
        # still reaches the agent through the system prompt and the tool registry.
        self.examples = MarkdownExampleStore(str(settings.knowledge_dir), index=self.index)

        # Instructions: the workspace's own rules, with the platform baseline
        # layered on top at read time. The markdown store stays as the fallback
        # for a deployment with no control plane, and is what the one-shot
        # importer reads from for one that has just gained one.
        from vanna.capabilities.knowledge import LayeredInstructionStore

        from .instruction_library import InstructionLibrary

        self.instruction_library = instruction_library or InstructionLibrary()
        self.markdown_instructions = MarkdownInstructionStore(str(settings.knowledge_dir))
        self.tenant_instructions = instruction_store or self.markdown_instructions
        self.instructions = LayeredInstructionStore(
            self.tenant_instructions,
            baseline=self.instruction_library.baseline_instructions,
            disableable_ids=self.instruction_library.disableable_ids,
            # Only the Postgres store can record an opt-out durably. Without one
            # nothing is disableable, which `LayeredInstructionStore` enforces --
            # a choice that would not survive a restart must not be offered.
            overrides=instruction_store,
        )
        self.generations = generation_store or LocalGenerationStore(
            str(settings.data_dir / "generations.jsonl")
        )
        self.conversations = conversation_store
        self.dashboards = dashboard_store
        # Persistent when there is a control plane to persist into, which is
        # every real deployment. It used to be an in-process stub unconditionally
        # -- `Memory ✗` in the chat's own status line, and `/memories` empty
        # however much the agent had been used.
        self.memory = _build_memory(app_db)
        self.session_properties: Dict[str, str] = {}

        # Records model, tokens and cost for every LLM call. Set by `wiring` after
        # construction because it needs the generation store, which the platform is
        # handed rather than building. Without it the four cost columns on
        # `generations` stay NULL, which is how they got there in the first place.
        self.usage_middleware: Any = None

        # Keyed (tenant_id, data_source_id): a workspace can have several
        # databases registered, and a runtime is bound to exactly one connection.
        self._runtimes: "OrderedDict[Tuple[str, str], TenantRuntime]" = OrderedDict()
        # One lock per (workspace, database). A single shared lock serialised
        # construction across every workspace, and construction opens a connection
        # and probes the database -- so one unreachable warehouse stalled everybody.
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

        # Semantic projects, from the catalog rather than from disk when this
        # deployment has switched over. None in `disk` mode and without a control
        # plane, in which case `load_project` reads the YAML tree as it always did.
        self.config_store = config_store
        # Built manifests, held between runtime builds and dropped when the
        # catalog's fingerprint moves. `on_change` retires the cached runtimes
        # rather than only the manifests: a runtime holds the tool registry that
        # was built around the old manifest, so forgetting one without the other
        # would leave every existing runtime enforcing the previous cubes.
        self.config_cache = ConfigCache(
            config_store,
            refresh_seconds=settings.config_refresh_seconds,
            on_change=self._forget_runtimes_after_config_change,
        )

    # -- lock management -----------------------------------------------

    async def _lock_for(self, key: Tuple[str, str]) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    # -- semantic layer ------------------------------------------------

    def _project_dir_for(self, tenant_id: str) -> Optional[str]:
        """Which project directory, if any, describes this workspace's database.

        A manifest describes *one* database. Applying a single global manifest to
        every tenant shows a workspace another workspace's models and makes every
        query fail against a schema that does not contain them.

        Only consulted in ``disk`` mode. With ``VANNA_CONFIG_SOURCE=database`` the
        same rule -- a directory per workspace, named for it -- survives as the
        ``tenant_id`` column on ``config_files``, so which project belongs to
        which workspace does not depend on which mode is running.
        """
        if self.settings.projects_dir:
            candidate = Path(self.settings.projects_dir) / tenant_id
            if (candidate / "vanna_project.yml").is_file():
                return str(candidate)

        if self.settings.project_dir and tenant_id == self.settings.default_tenant:
            return self.settings.project_dir

        return None

    async def load_project(self, tenant_id: str) -> Tuple[Any, Any]:
        """Load one workspace's semantic project. Returns ``(project, manifest)``.

        Reads the *built* manifest rather than the YAML tree, so what runs is what
        somebody deliberately compiled with ``vanna project build``.

        Cached in ``database`` mode, because this runs on every cold runtime build
        and a manifest is a few hundred kilobytes of JSON to parse and validate.
        The cache re-checks the catalog's fingerprint rather than trusting an
        in-process invalidation, which is what makes an edit visible to all four
        workers -- see :class:`~vanna_app.config_store.ConfigCache`.

        Not cached in ``disk`` mode, and that is not an oversight. The fingerprint
        the cache revalidates against is a query, so with no catalog behind it an
        entry would live for the life of the process -- and editing a YAML file
        under a bind mount would stop taking effect at all, which is the whole
        local development loop.
        """
        if self.settings.config_source != "database":
            return await self._load_project(tenant_id)
        return await self.config_cache.get(
            ("project", tenant_id), lambda: self._load_project(tenant_id)
        )

    async def _load_project(self, tenant_id: str) -> Tuple[Any, Any]:
        if self.settings.config_source == "database":
            return await self._load_project_from_catalog(tenant_id)
        # Off the event loop: reading and validating a manifest is tens of
        # milliseconds of blocking file IO and pydantic, and it used to run inline
        # on the loop that was streaming somebody else's answer.
        return await asyncio.to_thread(self._load_project_from_disk, tenant_id)

    async def _load_project_from_catalog(self, tenant_id: str) -> Tuple[Any, Any]:
        """The same project, read from ``config_files``.

        Deliberately without a fallback to disk. A deployment that has switched to
        the catalog and finds it empty has a failed import, and quietly running
        the YAML that happens to be baked into the image would hide that behind a
        healthy-looking service serving whatever was shipped -- which is the
        failure mode moving configuration into PostgreSQL exists to remove.
        """
        from .config_store import ConfigurationUnavailable, StoredProject

        if self.config_store is None:
            raise ConfigurationUnavailable(
                "VANNA_CONFIG_SOURCE=database, but there is no control plane to "
                "read the configuration catalog from."
            )

        config_row = await self.config_store.get(
            f"projects/{tenant_id}/vanna_project.yml"
        )
        if config_row is None:
            # Not an error: a workspace with no semantic project queries its
            # physical catalog, exactly as one with no project directory does.
            return None, None

        project = StoredProject.from_record(config_row)

        manifest_row = await self.config_store.get(
            f"projects/{tenant_id}/target/mdl.json"
        )
        if manifest_row is None or manifest_row.parsed is None:
            # An incomplete configuration, not an absent one. Falling through to
            # the physical catalog here would silently widen what the workspace
            # can reach: grants for a semantic workspace name models, so a
            # workspace that loses its manifest loses the enforcement built on it.
            raise ConfigurationUnavailable(
                f"Workspace {tenant_id} has a semantic project in the catalog but "
                f"no built manifest (projects/{tenant_id}/target/mdl.json). Run "
                "`vanna project build` and re-import, or remove the project."
            )

        from vanna.semantic.models import Manifest

        # The same validation the file path uses, so a bad stored manifest fails
        # in the same place and with the same message as a bad file one.
        manifest = Manifest.from_json_dict(manifest_row.parsed)
        self._log_semantic_layer(tenant_id, project, manifest, source="the catalog")
        return project, manifest

    def _load_project_from_disk(self, tenant_id: str) -> Tuple[Any, Any]:
        project_dir = self._project_dir_for(tenant_id)
        if not project_dir:
            return None, None

        from vanna.core.errors import VannaError
        from vanna.project import Project
        from vanna.semantic import load_built_manifest

        try:
            project = Project.load(Path(project_dir))
            manifest = load_built_manifest(project.paths)
        except VannaError as exc:
            logger.error("Could not load the project at %s: %s", project_dir, exc)
            return None, None

        if manifest is None:
            logger.warning(
                "Project %s has no built manifest. Run `vanna project build`; until "
                "then %s uses the physical catalog.",
                project_dir, tenant_id,
            )
            return project, None

        self._log_semantic_layer(tenant_id, project, manifest, source=project_dir)
        return project, manifest

    @staticmethod
    def _log_semantic_layer(
        tenant_id: str, project: Any, manifest: Any, *, source: str
    ) -> None:
        logger.info(
            "Semantic layer for %s from %s: %d models, %d relationships, %d cubes "
            "(fanout_guard=%s)",
            tenant_id,
            source,
            len(manifest.models),
            len(manifest.relationships),
            len(manifest.cubes),
            project.config.fanout_guard,
        )

    def forget_configuration(self) -> None:
        """Drop cached configuration and the runtimes built on it, now.

        For the worker that took a configuration write: it should not have to wait
        for its own fingerprint check to see its own edit. The other workers find
        out on their next check, which is what bounds how stale they can be.
        """
        self.config_cache.clear()
        self._forget_runtimes_after_config_change()

    def _forget_runtimes_after_config_change(self) -> None:
        """Retire every cached runtime, because the configuration behind it moved.

        Retired rather than closed, for the reason `retire` documents: a
        configuration change lands while requests are in flight, and closing a
        runner underneath one of them fails that request with "connection pool is
        closed". The next request builds a fresh runtime from the new manifest.

        Synchronous on purpose -- it is called from the cache's revalidation,
        which cannot await a per-key lock without deadlocking against a build it
        may be racing. Emptying the dict is enough: a builder holding the lock
        installs its runtime afterwards, and the next fingerprint check retires
        that one too.
        """
        stale, self._runtimes = list(self._runtimes.values()), OrderedDict()
        for runtime in stale:
            runtime.retire()
        if stale:
            logger.info(
                "Retired %d cached runtime(s) after a configuration change",
                len(stale),
            )
            try:
                get_metrics().tenant_runtimes.set(0)
            except Exception:  # pragma: no cover - metrics are never load-bearing
                pass

    # -- runtimes ------------------------------------------------------

    async def resolve_source(
        self, tenant_id: str, data_source_id: Optional[str] = None
    ) -> Tuple[str, str]:
        """``(data_source_id, database_url)`` for a workspace's chosen database.

        Raises :class:`~vanna_app.datasources.UnknownDataSource` when an id is
        named that the workspace has not registered -- deliberately, rather than
        quietly substituting the default. A caller who asked for one database and
        silently received another would read the answer as being about the one
        they asked for.

        Falls back to ``tenants.database_url`` when the workspace has registered
        nothing. That is the pre-registry world and still the common one.
        """
        if self.datasources is not None:
            resolved = await self.datasources.resolve(tenant_id, data_source_id)
            if resolved is not None:
                url = resolved["database_url"]
                return resolved["data_source_id"], (
                    url.reveal() if isinstance(url, Secret) else str(url or "")
                )

        tenant: Dict[str, Any] = {}
        if self.directory is not None:
            tenant = await self.directory.get_tenant(tenant_id) or {}
        url_secret = tenant.get("database_url")
        workspace_url = (
            url_secret.reveal() if isinstance(url_secret, Secret) else (url_secret or "")
        )
        database_url = workspace_url or self.settings.database_url

        # Register what the workspace already had, once, so it appears in the
        # picker alongside anything added later. Idempotent and self-healing: it
        # no-ops the moment anything is registered, so this runs at most once per
        # workspace and then this branch stops being reached at all.
        #
        # Done here rather than in migration 0011 because the id comes from
        # `describe_data_source`, whose rules are dialect-specific -- a
        # file-backed engine has no host, so the path is the whole address -- and
        # a second implementation in SQL would drift from this one.
        if self.datasources is not None and workspace_url:
            try:
                await self.datasources.backfill(tenant_id, workspace_url)
            except Exception as exc:
                # A workspace that cannot be registered still works through this
                # fallback; failing the request would be a worse trade.
                logger.warning("Could not register %s's database: %s", tenant_id, exc)

        return describe_data_source(database_url), database_url

    async def runtime_for(
        self,
        tenant_id: str,
        *,
        data_source_id: Optional[str] = None,
        invalidate: bool = False,
    ) -> TenantRuntime:
        """Get (or build) the runtime for one workspace *and one of its databases*.

        Keyed on the pair. A workspace can now have several databases registered,
        and each needs its own runner, catalog and tool registry -- those are
        bound to a connection at construction, which is the reason this cache
        exists at all.

        ``data_source_id`` is None for every caller that does not care, which is
        most of them: they get the workspace default. It must never be passed
        straight from a request header -- ``resolve_source`` validates it against
        the registry, and the chat path takes it from the conversation record
        rather than from the client.
        """
        tenant_id = tenant_id or self.settings.default_tenant

        if invalidate:
            # Every database of this workspace, not just one: `invalidate` means
            # the workspace's configuration changed, and the caller does not know
            # which runtimes that touched.
            await self._drop(tenant_id)

        source_id, database_url = await self.resolve_source(tenant_id, data_source_id)
        key = (tenant_id, source_id)

        existing = self._runtimes.get(key)
        if existing is not None and not self._stale(existing):
            existing.touch()
            self._runtimes.move_to_end(key)
            return existing

        lock = await self._lock_for(key)
        async with lock:
            # Re-check: another request may have built it while we waited.
            current = self._runtimes.get(key)
            if current is not None and not self._stale(current):
                current.touch()
                return current
            if current is not None:
                # Same reasoning as eviction: somebody may still be using it.
                current.retire()
                self._runtimes.pop(key, None)

            runtime = await self._build_runtime(tenant_id, source_id, database_url)
            self._runtimes[key] = runtime
            self._runtimes.move_to_end(key)
            self._evict_if_needed()
            get_metrics().tenant_runtimes.set(len(self._runtimes))

        # Outside the lock: a first scan can take seconds and must not block this
        # workspace's other requests behind it.
        await self._prepare_tenant(runtime)
        return runtime

    def _stale(self, runtime: TenantRuntime) -> bool:
        ttl = self.settings.tenant_runtime_ttl_seconds
        return ttl > 0 and (time.monotonic() - runtime.last_used) > ttl

    def _evict_if_needed(self) -> None:
        """Keep the cache within its bound, retiring what leaves."""
        limit = self.settings.max_tenant_runtimes
        while len(self._runtimes) > limit:
            (tenant_id, source_id), runtime = self._runtimes.popitem(last=False)
            # Retire, do not close: a request may still be running on this
            # runtime, and closing its pool underneath it fails that request.
            runtime.retire()
            logger.info(
                "Evicted cached runtime for %s/%s (cache limit %d reached). The limit now counts workspace-database pairs, so a workspace with several databases uses several slots.",
                tenant_id, source_id, limit,
            )

    async def _drop(self, tenant_id: str) -> None:
        """Forget every runtime of a workspace, and the catalogs describing them.

        All of them, not one: the caller invalidates because the workspace's
        configuration changed -- it was repointed, a plan changed, a data source
        was added -- and does not know which of its databases that touched.
        """
        for key in [k for k in self._runtimes if k[0] == tenant_id]:
            lock = await self._lock_for(key)
            async with lock:
                stale = self._runtimes.pop(key, None)
            if stale is not None:
                # Retired, not closed. Invalidation happens while the workspace is
                # in use -- a data source is added, a plan changes -- so the same
                # use-after-close applies here as on eviction. The next request
                # builds a fresh runtime either way; this one's connections go back
                # when its last in-flight request lets go.
                stale.retire()
                logger.info("Dropped cached runtime for %s/%s", key[0], key[1])

        # The catalog describes the *old* database. Left in place, the agent is
        # shown tables that no longer exist on the connection it is now using.
        try:
            removed = await self.catalog.clear(self.system_context(tenant_id))
            if removed:
                logger.info(
                    "Cleared %d stale catalog entries for %s; it will rescan.",
                    removed, tenant_id,
                )
        except Exception as exc:
            logger.warning("Could not clear the catalog for %s: %s", tenant_id, exc)

    async def _build_runtime(
        self, tenant_id: str, data_source: str, database_url: str
    ) -> TenantRuntime:
        from vanna.core.agent import Agent, AgentConfig
        from vanna.core.billing import resolve_limits
        from vanna.core.enhancer import BudgetPolicy, RetrievalContextEnhancer
        from vanna.core.middleware import PromptCacheMiddleware
        from vanna.core.recovery import SqlRepairStrategy
        from vanna.core.sql_policy import SqlPolicy
        from vanna.core.system_prompt import AnalystSystemPromptBuilder
        from vanna.integrations.local import MemoryConversationStore
        from vanna.servers.base import ChatHandler
        from .chat_commands import DataLensWorkflow
        from vanna.tools import (
            AnalyzeTimeseriesTool,
            CalculatorTool,
            ComparePeriodsTool,
            CheckColumnValuesTool,
            CheckCoreColumnsTool,
            ListKnownValuesTool,
            ProfileColumnTool,
            RequestClarificationTool,
            SearchKnowledgeTool,
            SearchQueryHistoryTool,
            SuggestJoinsTool,
            SystemTimeTool,
            TIME_FUNCTION_NAMES,
            ValidateSqlTool,
            VisualizeDataTool,
            create_schema_tools,
        )
        from vanna.tools.agent_memory import (
            SaveQuestionToolArgsTool,
            SaveTextMemoryTool,
            SearchSavedCorrectToolUsesTool,
        )

        from .knowledge_links import WorkspaceKnowledge, manifest_of
        from .limits import build_limit_hooks

        settings = self.settings
        tenant: Dict[str, Any] = {}
        if self.directory is not None:
            tenant = await self.directory.get_tenant(tenant_id) or {}

        # The connection is chosen by `resolve_source` and passed in, because the
        # caller had to resolve it anyway to know which cache slot this is. Deriving
        # it a second time here would be a second answer to the same question, and
        # the two would disagree the first time a workspace registered a database
        # that is not the one on its `tenants` row.

        # Limits come from the subscription unless the workspace has an explicit
        # override. Resolved once: the row cap is baked into the SQL runner and the
        # policy's default LIMIT when they are built, so a plan change takes effect
        # when the runtime is next built -- which is what `invalidate` is for.
        subscription = None
        if self.billing is not None:
            subscription = await self.billing.get_subscription(tenant_id)

        limits = resolve_limits(
            tenant,
            subscription,
            default_quota=settings.daily_quota,
            default_max_rows=settings.max_rows,
        )
        logger.info(
            "Workspace %s limits: plan=%s quota=%s (%s) max_rows=%s (%s)",
            tenant_id,
            limits.plan.name,
            limits.daily_quota,
            limits.quota_source,
            limits.max_rows,
            limits.rows_source,
        )

        writes_enabled = settings.allow_writes and bool(tenant.get("allow_writes"))
        # The reading runner stays read-only whatever the workspace allows. A
        # write travels on its own runner, below, so the connection that serves
        # questions cannot commit anything even if a statement reaches it.
        runner = build_sql_runner(
            settings, database_url, max_rows=limits.max_rows, read_only=True
        )
        write_runner = (
            build_sql_runner(
                settings, database_url, max_rows=limits.max_rows, read_only=False
            )
            if writes_enabled
            else None
        )
        if writes_enabled:
            logger.warning(
                "Workspace %s has WRITES ENABLED; approved changes run on a "
                "separate writable connection.",
                tenant_id,
            )
        dialect = getattr(runner, "dialect", "sqlite")

        # Read-only, and non-deterministic time functions denied because
        # SystemTimeTool supplies real date literals instead.
        #
        # Built from `read_only()` rather than from `SqlPolicy(...)`, which is what
        # this used to do. The difference is not cosmetic: the bare constructor
        # takes the *field* defaults, so the two catalog checks stayed off no
        # matter what `read_only()` said, and the classmethod documented as "the
        # recommended default for natural-language query interfaces" was not the
        # policy any query was actually checked against. Revoking read on a column
        # filtered it out of the prompt and changed nothing about what `/run-sql`
        # would execute.
        policy = SqlPolicy.read_only().model_copy(
            update={
                "denied_functions": frozenset(TIME_FUNCTION_NAMES),
                "default_limit": limits.max_rows,
            }
        )

        catalog, registry = await self._build_registry(
            tenant_id, tenant, policy, runner, dialect, data_source
        )

        registry.register_local_tool(
            recording_run_sql_tool(
                runner,
                self.generations,
                data_source,
                admin_audit=self.admin_audit,
            ),
            [],
        )
        registry.register_local_tool(ValidateSqlTool(runner, policy=policy, catalog=catalog), [])
        registry.register_local_tool(CheckColumnValuesTool(runner), [])
        registry.register_local_tool(SystemTimeTool(), [])
        registry.register_local_tool(VisualizeDataTool(), [])

        # Memory. Registered only now that there is somewhere for it to go:
        # these were absent, so the agent could neither look up how a similar
        # question was answered before nor record how this one was -- and the
        # chat's own status line said `Memory ✗`, because `has_memory` is
        # computed from whether these two tools exist rather than from the
        # store.
        #
        # They read `ToolContext.agent_memory`, which is the per-workspace store
        # `_build_memory` provides, so there is nothing to pass here.
        registry.register_local_tool(SearchSavedCorrectToolUsesTool(), [])
        registry.register_local_tool(SaveQuestionToolArgsTool(), [])
        registry.register_local_tool(SaveTextMemoryTool(), [])

        # Four services already on Platform with no tool exposing them. Each
        # registers with [] (no access-group gate): search_knowledge and
        # list_known_values return exactly what the enhancers already inject
        # unasked, and search_query_history filters to the caller's own rows
        # itself rather than via a scope argument (`access_groups` attaches to
        # a tool name, not its arguments).
        registry.register_local_tool(CalculatorTool(), [])
        registry.register_local_tool(
            SearchKnowledgeTool(self.examples, self.instructions), []
        )
        registry.register_local_tool(
            SearchQueryHistoryTool(self.generations, catalog), []
        )
        if self.values is not None:
            # Platform.values is optional; an unconditional registration would
            # AttributeError inside execute(), which the registry swallows
            # into an undiagnosable "Execution failed".
            registry.register_local_tool(
                ListKnownValuesTool(self.values, catalog), []
            )

        write_service = None
        if write_runner is not None and self.grants is not None:
            from vanna.core.write.approval import WriteApprovalMode
            from vanna.core.write.service import WriteService
            from vanna.tools import create_write_tools

            write_service = WriteService(
                grants=self.grants,
                catalog=catalog,
                approvals=self.write_approvals,
                runner=write_runner,
                data_source_id=data_source,
                dialect=dialect,
                max_rows=settings.max_write_rows,
                approval_mode=WriteApprovalMode(
                    tenant.get("write_approval_mode") or "self"
                ),
                audit_logger=self.audit_logger,
            )
            for tool in create_write_tools(write_service):
                # Gated twice over: an analyst never sees these in the schema,
                # and the grant model decides which tables an admin may touch.
                registry.register_local_tool(tool, ["admin"])

        if self.dashboards is not None:
            from vanna.tools import create_dashboard_tools

            for tool in create_dashboard_tools(self.dashboards):
                # Viewers may read a dashboard but not author one.
                registry.register_local_tool(tool, ["admin", "analyst"])

        for tool in create_schema_tools(catalog):
            registry.register_local_tool(tool, [])

        # `catalog`, not `self.catalog`: join paths must be narrowed by the same
        # grant filtering and semantic wrapping every other schema read goes
        # through, or the tool would suggest a join through a table the caller
        # is not allowed to see.
        registry.register_local_tool(
            SuggestJoinsTool(catalog, data_source_id=data_source), []
        )

        # `runner` for the live aggregate, `catalog` for the facts the scanner
        # already captured -- a column profiled at scan time costs no round trip.
        registry.register_local_tool(
            ProfileColumnTool(runner, catalog=catalog, data_source_id=data_source), []
        )

        # Both run model-written SQL and declare `sql_argument_fields`, so the
        # same policy that guards run_sql guards them -- registering them here,
        # beside it, rather than with the catalog tools they resemble.
        registry.register_local_tool(AnalyzeTimeseriesTool(runner), [])
        registry.register_local_tool(ComparePeriodsTool(runner), [])

        # No dependencies at all: it puts a question to the user and ends the
        # turn. Available to every role -- a viewer's question is as likely to
        # be ambiguous as an admin's.
        registry.register_local_tool(RequestClarificationTool(), [])

        # self.catalog rather than the (possibly semantic-wrapped) `catalog`
        # above: core columns are a control-plane curation concept, same as
        # the annotations `routes/catalog.py` reaches for `deps.platform.catalog`
        # to reach, not part of the general SchemaCatalog interface.
        registry.register_local_tool(
            CheckCoreColumnsTool(self.catalog, data_source_id=data_source), []
        )

        hooks = build_limit_hooks(
            self.counters,
            daily_quota=limits.daily_quota,
            rate_limit_per_min=settings.rate_limit_per_min,
        )
        hooks.append(question_capture_hook())

        agent = Agent(
            llm_service=self.llm,
            tool_registry=registry,
            user_resolver=self.user_resolver,
            agent_memory=self.memory,
            conversation_store=self.conversations or MemoryConversationStore(),
            config=AgentConfig(max_tool_iterations=12, temperature=0.0),
            system_prompt_builder=AnalystSystemPromptBuilder(dialect=dialect),
            llm_context_enhancer=_with_domain_context(
                _with_write_context(
                    _with_value_resolution(
                        RetrievalContextEnhancer(
                            catalog=catalog,
                            example_store=self.examples,
                            instruction_store=self.instructions,
                            # Without this the enhancer keeps its default of
                            # "default", so a DATA_SOURCE-scoped rule only ever
                            # matched a workspace whose data source was literally
                            # named that -- i.e. never. The console has offered that
                            # scope the whole time.
                            data_source_id=data_source,
                            budget=BudgetPolicy(total_tokens=120_000),
                            schema_threshold=self.settings.schema_full_text_threshold,
                            # Glossary terms and cube metrics seed the search
                            # path's table selection; core columns are shown
                            # beside the schema instead of behind a tool call.
                            # `self.catalog` for core columns for the same
                            # reason CheckCoreColumnsTool takes it.
                            knowledge=WorkspaceKnowledge(
                                tenant_id=tenant_id,
                                data_source_id=data_source,
                                domains=self.domains,
                                catalog_store=self.catalog,
                                manifest=manifest_of(catalog),
                            ),
                        ),
                        catalog=catalog,
                        store=self.values,
                        data_source=data_source,
                    ),
                    write_service,
                ),
                self.domains,
                tenant_id=tenant_id,
                data_source_id=data_source,
            ),
            llm_middlewares=[
                middleware
                for middleware in (PromptCacheMiddleware(), self.usage_middleware)
                if middleware is not None
            ],
            lifecycle_hooks=hooks,
            error_recovery_strategy=SqlRepairStrategy(catalog=catalog),
            audit_logger=self.audit_logger,
            workflow_handler=DataLensWorkflow(),
        )

        _install_turn_nodes(agent, self.llm, settings)

        logger.info("Built runtime for %s -> %s (%s)", tenant_id, data_source, dialect)

        return TenantRuntime(
            tenant_id=tenant_id,
            dialect=dialect,
            runner=runner,
            catalog=catalog,
            policy=policy,
            agent=agent,
            handler=ChatHandler(agent),
            data_source=data_source,
            write_service=write_service,
        )

    async def _build_registry(
        self,
        tenant_id: str,
        tenant: Dict[str, Any],
        policy: Any,
        runner: Any,
        dialect: str,
        data_source: str,
    ) -> Tuple[Any, Any]:
        """Pick the tool registry: semantic layer, or the physical catalog.

        Two registries, one interface. With a manifest the agent writes SQL against
        model names and every tool call is compiled and access-checked in
        ``transform_args``; without one this is exactly the stack that ran before
        the semantic layer existed. The swap is here and nowhere else.
        """
        from vanna.core.sql_policy import SqlPolicyToolRegistry

        project, manifest = await self.load_project(tenant_id)
        fanout_guard = project.config.fanout_guard if project else "warn"
        session_properties = dict(self.session_properties)
        if project is not None:
            raw = project.config.extra.get("session_properties") or {}
            session_properties = {str(k): str(v) for k, v in raw.items()}

        # A manifest describes one database. Repointing a workspace at another one
        # leaves the models describing tables the new database does not have, and
        # every semantic query then fails with "relation does not exist".
        if manifest is not None:
            missing, unmodelled = await self._compare_manifest_to(manifest, runner)
            if missing:
                logger.error(
                    "Workspace %s is bound to %s, but its semantic project expects "
                    "tables that database does not have (%s). Falling back to the "
                    "scanned catalog.",
                    tenant_id, data_source, ", ".join(sorted(missing)[:5]),
                )
                manifest = None
            elif unmodelled:
                self._warn_about_coverage(tenant_id, data_source, manifest, unmodelled)

        resolver = policy_for_user(policy, self.settings, tenant)

        if manifest is None:
            catalog = self._read_guarded(tenant_id, data_source, self.catalog)
            return catalog, SqlPolicyToolRegistry(
                policy=policy,
                dialect=dialect,
                catalog=catalog,
                # Scoped, so the table and column checks see this database's
                # tables and not every table the workspace has anywhere.
                data_source_id=data_source,
                policy_for_user=resolver,
            )

        from vanna.capabilities.schema_catalog.semantic import SemanticSchemaCatalog
        from vanna.core.access import AccessControlToolRegistry, SessionPropertyResolver

        # The guard goes on the *outside*, around whichever catalog the agent
        # actually reads. Wrapping the physical one underneath instead left
        # enforcement silently inert for every semantic workspace: the semantic
        # catalog answers `get_tables` from the manifest and never consults the
        # physical one, so the filter was never reached. Grants for such a
        # workspace name models, which is what the preset expands over and what
        # the admin screen lists.
        catalog = self._read_guarded(
            tenant_id, data_source, SemanticSchemaCatalog(manifest, physical=self.catalog)
        )
        logger.info(
            "Workspace %s uses its semantic layer (%d models) over %s",
            tenant_id, len(manifest.models), data_source,
        )
        return catalog, AccessControlToolRegistry(
            policy=policy,
            dialect=dialect,
            catalog=catalog,
            data_source_id=data_source,
            manifest=manifest,
            fanout_guard=fanout_guard,
            session_resolver=SessionPropertyResolver(session_properties),
            policy_for_user=resolver,
        )

    def _read_guarded(self, tenant_id: str, data_source: str, inner: Any) -> Any:
        """The schema catalog, narrowed to what each caller may read.

        Returns the plain catalog unless this deployment has a grant store *and*
        a policy store to ask -- and even then the wrapper is inert until a role
        has ``enforce_reads`` switched on. Grants governed writes only until now,
        so anything that narrowed reads by default would take access away from
        every existing user at once.
        """
        if self.grants is None or self.grant_policies is None:
            return inner

        from .read_guard import GrantFilteredCatalog

        async def enforced_roles() -> Any:
            return await self.grant_policies.enforced_read_roles(
                tenant_id, data_source
            )

        return GrantFilteredCatalog(
            inner,
            grants=self.grants,
            data_source_id=data_source,
            enforced_roles=enforced_roles,
        )

    #: Below this share of a database's tables, a manifest is more likely to be an
    #: unfinished copy than a deliberate subset, and the log says so.
    COVERAGE_FLOOR = 0.5

    def _warn_about_coverage(
        self, tenant_id: str, data_source: str, manifest: Any, unmodelled: set
    ) -> None:
        """Say out loud which tables the semantic layer is hiding.

        The semantic catalog *replaces* the physical one, so a table absent from the
        manifest does not exist as far as the agent is concerned. That is the point
        of a semantic layer -- and it is also how a half-finished one fails.

        It failed here exactly that way. A two-model demo project was copied onto a
        workspace bound to the eleven-table chinook database, so nine tables became
        invisible and the agent answered its own starter question with "I can't
        answer that with the tables currently available". Nothing was broken, no
        error was raised, and the only visible symptom was an agent that appeared to
        have forgotten its schema.

        A subset can be deliberate, so this is a warning rather than a refusal. But
        it names the tables, because the alternative is discovering them one
        unanswerable question at a time.
        """
        modelled = len(manifest.models)
        total = modelled + len(unmodelled)
        if not total:
            return

        share = modelled / total
        level = logging.WARNING if share < self.COVERAGE_FLOOR else logging.INFO
        logger.log(
            level,
            "Workspace %s models %d of %d tables in %s; the agent cannot see the "
            "other %d (%s). A table missing from the manifest is invisible, not "
            "merely undocumented.",
            tenant_id, modelled, total, data_source, len(unmodelled),
            ", ".join(sorted(unmodelled)[:8]),
        )

    async def _compare_manifest_to(self, manifest: Any, runner: Any) -> Tuple[set, set]:
        """``(model tables the database lacks, database tables the manifest lacks)``.

        One cheap probe rather than a per-model check. Returns empty sets when the
        check itself cannot run -- an unreachable database is a different problem,
        and refusing the semantic layer over it would swap a clear error for a
        confusing one.
        """
        from vanna.capabilities.sql_runner import RunSqlToolArgs

        expected = {
            (model.table_reference or "").split(".")[-1].strip('"').lower()
            for model in manifest.models
            if model.table_reference
        }
        if not expected:
            return set(), set()

        try:
            frame = await runner.run_sql(
                RunSqlToolArgs(
                    sql=(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                    )
                ),
                self.system_context("__probe__"),
            )
            present = {str(v).lower() for v in frame.iloc[:, 0].tolist()}
        except Exception as exc:
            logger.debug("Could not verify the manifest against the database: %s", exc)
            return set(), set()

        return expected - present, present - expected

    # -- first use -----------------------------------------------------

    async def _prepare_tenant(self, runtime: TenantRuntime) -> None:
        """Scan and seed a workspace's knowledge, once, on first use.

        Lazy rather than at boot: with several workspaces pointed at several
        databases, scanning all of them at startup turns one slow warehouse into a
        container that never reports healthy.

        Note what is inside the "already scanned" shortcut and what is not. The
        scan and the example seeding are skipped for a workspace that has a
        catalog, because repeating them would be wasted work at best and would
        resurrect deleted seeds at worst. The markdown import and the grant
        defaults are **not** skipped: both have their own idempotence, and both
        exist precisely for a workspace that already has a catalog because it has
        been running since before they were added. Behind the shortcut they would
        never run on the only deployments that need them.
        """
        context = self.system_context(runtime.tenant_id)

        scanned = False
        try:
            # Scoped to this data source, not to the whole workspace. Asking the
            # broader question would report "already scanned" for a workspace whose
            # catalog was written under some other key -- including the "default"
            # one written before the scan started passing a real id -- and the
            # re-scan that would repair it would never run.
            scanned = bool(
                await runtime.catalog.get_tables(
                    context, data_source_id=runtime.data_source
                )
            )
        except Exception:
            pass

        if not scanned and self.settings.scan_on_start:
            from vanna.capabilities.schema_catalog import SchemaScanner

            try:
                # Keyed by the workspace's data source, not by the scanner's
                # "default" placeholder.
                #
                # Everything else that describes a database is already keyed this
                # way -- table_grants, column_grants, grant_policies -- and the
                # catalog was the one thing that was not. The consequences were
                # quiet and awkward: `routes/grants.py` asks the catalog for
                # `runtime.data_source`, got nothing back for any workspace whose
                # label was not literally "default", and rendered a permission
                # matrix with no tables in it. Applying a preset then granted
                # nothing, and enabling read enforcement was refused on the
                # grounds that the role would have no readable table -- which was
                # true, and nothing to do with what the administrator had asked
                # for.
                report = await SchemaScanner(runtime.runner, dialect=runtime.dialect).scan(
                    context, runtime.catalog, data_source_id=runtime.data_source
                )
                logger.info("Schema scan for %s: %s", runtime.tenant_id, report.summary())
            except Exception as exc:
                logger.error("Schema scan failed for %s: %s", runtime.tenant_id, exc)

        await self._apply_grant_defaults(runtime, context)
        await self._import_markdown_rules(runtime, context)

        if not scanned:
            await self._seed_knowledge(runtime, context)

    async def _apply_grant_defaults(self, runtime: TenantRuntime, context: Any) -> None:
        """Extend this workspace's grant defaults over whatever the scan found.

        `create_tenant` cannot do this: the catalog is scanned lazily on first
        use, so at creation there are no tables to grant. It writes the intent
        and this turns it into rows.

        Only for roles whose policy says ``apply_to_new_tables``, which is off by
        default -- a table that has just appeared is one nobody has looked at yet.
        """
        if self.grants is None or self.grant_policies is None:
            return

        from .grant_defaults import apply_to_new_tables
        from .read_guard import unfiltered

        await apply_to_new_tables(
            store=self.grants,
            policy_store=self.grant_policies,
            context=context,
            tenant_id=runtime.tenant_id,
            data_source_id=runtime.data_source,
            catalog=unfiltered(runtime.catalog),
        )

    async def _seed_knowledge(self, runtime: TenantRuntime, context: Any) -> None:
        """Seed starter examples, and bring across any markdown rules, once.

        Only when the store is empty -- a rescan must not resurrect seeds a reviewer
        deleted, or bury the curated examples accumulated since.

        There used to be a hardcoded instruction here, about ``_cents`` columns
        holding integer cents. It was seeded into every new workspace regardless
        of whether its schema had such a column, which made a business assumption
        look like a platform rule. It now lives in the ``finance-conventions``
        starter pack, where a workspace takes it deliberately or not at all.
        """
        try:
            from vanna.capabilities.knowledge import seed_example_store

            added = await seed_example_store(
                context,
                self.examples,
                await runtime.catalog.get_tables(
                    context, data_source_id=runtime.data_source
                ),
                await runtime.catalog.get_relationships(
                    context, data_source_id=runtime.data_source
                ),
                dialect=runtime.dialect,
            )
            if added:
                logger.info("Seeded %d starter examples for %s", added, runtime.tenant_id)
        except Exception as exc:
            logger.warning("Could not seed starter examples: %s", exc)

    async def ensure_instructions_imported(self, tenant_id: str) -> None:
        """Bring a workspace's markdown rules across, without building a runtime.

        The import normally rides along with the first use of a workspace, which
        needs a warehouse connection. That is too late for the console: an
        administrator opening the Instructions screen the morning after an
        upgrade would see the platform baseline and none of their own rules,
        which reads as data loss.

        Nothing here touches the warehouse -- it copies rows between two stores
        -- so the admin screens can call it directly and cheaply.
        """
        from .instruction_store import (
            PostgresInstructionStore,
            claim_import,
            needs_import,
            release_import,
        )

        store = self.tenant_instructions
        if not isinstance(store, PostgresInstructionStore):
            return

        try:
            # Cheap check first, so the common case is one SELECT rather than an
            # UPDATE on every list.
            if not await needs_import(store.db, tenant_id):
                return
            if not await claim_import(store.db, tenant_id):
                return  # another worker is doing it
        except Exception as exc:
            logger.warning("Could not claim the rule import for %s: %s", tenant_id, exc)
            return

        try:
            context = self.system_context(tenant_id)
            added = await store.import_from(context, self.markdown_instructions)
            if added:
                logger.info("Imported %d markdown rule(s) for %s", added, tenant_id)
        except Exception as exc:
            await release_import(store.db, tenant_id)
            logger.warning(
                "Could not import markdown rules for %s: %s", tenant_id, exc
            )

    async def _import_markdown_rules(self, runtime: TenantRuntime, context: Any) -> None:
        """Bring a workspace's `knowledge/<tenant>/rules/*.md` into the database.

        Exactly once per workspace, recorded on ``tenants.instructions_imported_at``.
        The marker is the whole point: the obvious condition -- import when the
        table is empty -- resurrects every rule an administrator deliberately
        deleted, on the next restart.

        The files are left in place. They are somebody's only copy until they are
        satisfied this worked.
        """
        await self.ensure_instructions_imported(runtime.tenant_id)

    # -- misc ----------------------------------------------------------

    def system_context(self, tenant_id: str) -> Any:
        """A privileged context for background work on one workspace."""
        import uuid

        from vanna.core.tool import ToolContext
        from vanna.core.user import User

        return ToolContext(
            user=User(id="system", tenant_id=tenant_id, group_memberships=["admin"]),
            conversation_id="system",
            request_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            agent_memory=self.memory,
        )

    def cached_tenants(self) -> int:
        return len(self._runtimes)

    def shutdown(self) -> None:
        for tenant_id, runtime in list(self._runtimes.items()):
            runtime.close()
            logger.debug("Closed runtime for %s", tenant_id)
        self._runtimes.clear()


def _build_memory(app_db: Any = None) -> Any:
    """Agent memory, partitioned per tenant.

    Backed by the control plane when there is one, so what the agent learns
    outlives the request that taught it. Without a database -- the demo and the
    tests -- it falls back to the in-process stub below, which is honest about
    being empty rather than pretending to remember.

    The partition wrapper is what keeps one workspace's saved patterns out of
    another's retrieval, independent of whether the backing store filters.
    """
    from vanna.capabilities.agent_memory import AgentMemory, TenantPartitionedAgentMemory

    if app_db is not None:
        from .memory_store import PostgresAgentMemory

        # A factory, not an instance: the wrapper calls it once per workspace and
        # requires each result to be independent. Pinning the tenant at
        # construction is what makes that true here -- the rows live in shared
        # tables, but a store built for one workspace cannot query another's.
        return TenantPartitionedAgentMemory(
            lambda tenant: PostgresAgentMemory(app_db, tenant_id=tenant)
        )

    class EphemeralMemory(AgentMemory):
        """Non-persistent memory, so a restart starts clean."""

        def __init__(self) -> None:
            self._text: list = []

        async def save_tool_usage(self, *a: Any, **k: Any) -> None:
            return None

        async def save_text_memory(self, content: Any, context: Any) -> Any:
            import uuid
            from datetime import datetime, timezone

            from vanna.capabilities.agent_memory import TextMemory

            memory = TextMemory(
                memory_id=str(uuid.uuid4()),
                content=content,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self._text.append(memory)
            return memory

        async def search_similar_usage(self, *a: Any, **k: Any) -> list:
            return []

        async def search_text_memories(self, query: Any, context: Any, **k: Any) -> list:
            return []

        async def get_recent_memories(self, context: Any, limit: int = 10) -> list:
            return []

        async def get_recent_text_memories(self, context: Any, limit: int = 10) -> list:
            return self._text[-limit:]

        async def delete_by_id(self, context: Any, memory_id: str) -> bool:
            return False

        async def delete_text_memory(self, context: Any, memory_id: str) -> bool:
            return False

        async def clear_memories(
            self, context: Any, tool_name: Any = None, before_date: Any = None
        ) -> int:
            count = len(self._text)
            self._text.clear()
            return count

    return TenantPartitionedAgentMemory(lambda _tenant: EphemeralMemory())
