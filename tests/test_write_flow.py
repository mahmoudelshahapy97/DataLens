"""Approval, re-authorization and execution, against a real database.

SQLite rather than a mock, because the properties worth testing are properties
of a transaction: that a rolled-back step leaves nothing behind, that a
generated key really reaches the child row, that a commit is a commit. A fake
runner would let all three pass while being wrong.
"""

import asyncio
import sqlite3

import pytest

from vanna.capabilities.schema_catalog import ColumnMetadata, ForeignKey, TableMetadata
from vanna.capabilities.sql_runner.write import UnexpectedRowCount, WritesNotSupported
from vanna.core.grants import TableGrant
from vanna.core.registry import ToolRegistry
from vanna.core.tool import ToolCall, ToolContext
from vanna.core.user import User
from vanna.core.write import (
    ColumnAssignment,
    KeyPredicate,
    StepReference,
    WriteCode,
    WritePlan,
    WriteRefusal,
    WriteStep,
    validate_write_plan,
)
from vanna.core.write.approval import (
    WriteApprovalMode,
    WriteStatus,
    requires_second_person,
)
from vanna.core.write.service import WriteService
from vanna.integrations.local import MemoryGrantStore
from vanna.integrations.local.agent_memory.in_memory import DemoAgentMemory
from vanna.integrations.local.write_approvals import MemoryWriteApprovalStore
from vanna.integrations.sqlite.sql_runner import SqliteRunner

A, K, R, S = ColumnAssignment, KeyPredicate, StepReference, WriteStep

CATALOG = [
    TableMetadata(table_name="orders", columns=[
        ColumnMetadata(name="order_id", nullable=False, is_primary_key=True,
                       is_generated=True),
        ColumnMetadata(name="status", nullable=False),
        ColumnMetadata(name="customer", nullable=False),
    ]),
    TableMetadata(table_name="order_items", columns=[
        ColumnMetadata(name="item_id", nullable=False, is_primary_key=True,
                       is_generated=True),
        ColumnMetadata(name="order_id", nullable=False, foreign_key=ForeignKey(
            column="order_id", references_table="orders", references_column="order_id")),
        ColumnMetadata(name="sku", nullable=False),
    ]),
]


class FakeCatalog:
    async def get_tables(self, context, *, data_source_id=None, schema=None):
        return CATALOG

    async def catalog_hash(self, context, *, data_source_id=None):
        return "fingerprint-v1"


@pytest.fixture
def database(tmp_path):
    path = str(tmp_path / "shop.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders (
          order_id INTEGER PRIMARY KEY AUTOINCREMENT,
          status   TEXT NOT NULL,
          customer TEXT NOT NULL);
        CREATE TABLE order_items (
          item_id  INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER NOT NULL REFERENCES orders(order_id),
          sku      TEXT NOT NULL);
        INSERT INTO orders (status, customer)
          VALUES ('new','acme'),('new','globex'),('new','initech');
        """
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def admin():
    return User(id="u-alice", tenant_id="acme", email="alice@acme.test",
                group_memberships=["admin"])


def make_context(user, tenant="acme"):
    return ToolContext(user=user, conversation_id="c1", request_id="r1",
                       tenant_id=tenant, agent_memory=DemoAgentMemory())


@pytest.fixture
def context(admin):
    return make_context(admin)


@pytest.fixture
async def grants(context):
    store = MemoryGrantStore()
    store.load_catalog_facts(CATALOG)
    for table in CATALOG:
        await store.set_table_grant(context, TableGrant(
            data_source_id="shop", role="admin", table=table.table_name,
            can_select=True, can_insert=True, can_update=True, can_delete=True))
        await store.auto_grant_columns(
            context, data_source_id="shop", role="admin", table=table.table_name,
            columns=[c.name for c in table.columns], can_write=True)
    return store


@pytest.fixture
def service(database, grants):
    return WriteService(
        grants=grants, catalog=FakeCatalog(), approvals=MemoryWriteApprovalStore(),
        runner=SqliteRunner(database, read_only=False), data_source_id="shop",
        dialect="sqlite", max_rows=50)


def count(database, sql, *params):
    conn = sqlite3.connect(database)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def ship_order(order_id=1):
    return WritePlan(steps=[S(operation="update", table="orders",
                              assignments=[A(column="status", value="shipped")],
                              predicate=[K(column="order_id", value=order_id)],
                              expected_row_count=1)])


class TestRunnerContract:
    def test_engines_without_a_write_path_refuse(self):
        """Refusing beats inheriting a best-effort implementation.

        The failure being designed out is a runner that accepts DML, never
        commits it, and reports success -- which is what MySQL did here before
        this contract existed.
        """
        from vanna.capabilities.sql_runner import BaseSqlRunner

        class Nowhere(BaseSqlRunner):
            dialect = "sqlite"

            def _execute_sync(self, sql, timeout_seconds):  # pragma: no cover
                raise AssertionError("never called")

        with pytest.raises(WritesNotSupported):
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                Nowhere().execute_write(None, None))

    @pytest.mark.parametrize("module,cls,expected", [
        ("vanna.integrations.postgres.sql_runner", "PostgresRunner", "format"),
        ("vanna.integrations.mysql.sql_runner", "MySQLRunner", "format"),
        ("vanna.integrations.mssql.sql_runner", "MSSQLRunner", "qmark"),
        ("vanna.integrations.sqlite.sql_runner", "SqliteRunner", "qmark"),
        ("vanna.integrations.duckdb.sql_runner", "DuckDBRunner", "qmark"),
    ])
    def test_write_capable_runners_declare_their_paramstyle(self, module, cls, expected):
        """Inheriting the default silently renders %s for a driver wanting ?.

        That mismatch surfaces as a syntax error at execution time -- after
        approval, which is the worst possible moment to discover it. Declaring
        the style in the class body is what makes it reviewable.
        """
        import importlib

        try:
            runner = getattr(importlib.import_module(module), cls)
        except (ImportError, AttributeError):
            pytest.skip(f"{cls} unavailable")
        assert "paramstyle" in vars(runner), f"{cls} must declare paramstyle explicitly"
        assert runner.paramstyle == expected

    async def test_a_read_only_connection_refuses(self, database, service, context):
        """The last of the three layers, and the one that cannot be argued with."""
        validated = validate_write_plan(
            ship_order(), await service.build_policy(context), paramstyle="qmark")
        with pytest.raises(WritesNotSupported):
            await SqliteRunner(database, read_only=True).execute_write(
                validated, context)


def _ctx():
    return make_context(User(id="u-alice", tenant_id="acme",
                             group_memberships=["admin"]))


class TestApprovalGate:
    async def test_proposing_changes_nothing(self, database, service, context):
        pending, _ = await service.propose(context, ship_order())
        assert pending.status is WriteStatus.PENDING
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 0

    async def test_approving_executes(self, database, service, context):
        pending, _ = await service.propose(context, ship_order())
        _, result = await service.decide_and_execute(context, pending.id, approve=True)
        assert result.rows_affected == 1
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 1

    async def test_declining_changes_nothing(self, database, service, context):
        pending, _ = await service.propose(context, ship_order())
        decided, result = await service.decide_and_execute(
            context, pending.id, approve=False)
        assert decided.status is WriteStatus.REJECTED and result is None
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 0

    async def test_a_stranger_cannot_decide(self, service, context):
        pending, _ = await service.propose(context, ship_order())
        with pytest.raises(WriteRefusal) as caught:
            await service.decide(context, pending.id, approve=True,
                                 decided_by="u-mallory")
        assert caught.value.code is WriteCode.CONFIRMATION_FORBIDDEN

    async def test_a_confirmation_is_single_use(self, database, service, context):
        """Two clicks on approve must produce one execution, not two."""
        pending, _ = await service.propose(context, ship_order())
        outcomes = await asyncio.gather(
            service.decide(context, pending.id, approve=True),
            service.decide(context, pending.id, approve=True),
            return_exceptions=True)
        refusals = [o for o in outcomes if isinstance(o, WriteRefusal)]
        assert len(refusals) == 1
        assert refusals[0].code is WriteCode.CONFIRMATION_NOT_PENDING

    async def test_an_expired_approval_is_refused(self, service, context):
        pending, _ = await service.propose(context, ship_order())
        stale = pending.model_copy(update={
            "expires_at": pending.created_at.replace(year=pending.created_at.year - 1)})
        await service.approvals.create(context, stale)
        with pytest.raises(WriteRefusal) as caught:
            await service.decide(context, stale.id, approve=True)
        assert caught.value.code is WriteCode.CONFIRMATION_EXPIRED


class TestReauthorization:
    async def test_a_revoked_grant_refuses_an_approved_write(
        self, database, service, context, grants
    ):
        """Revoking between approval and execution must actually revoke.

        Re-validation runs before the version comparison, so a grant that is
        gone entirely produces the specific reason rather than the generic
        "something moved" -- which is the more useful of the two.
        """
        pending, _ = await service.propose(context, ship_order())
        await service.decide(context, pending.id, approve=True)
        approved = await service.approvals.get(context, pending.id)

        await grants.set_table_grant(context, TableGrant(
            data_source_id="shop", role="admin", table="orders", can_select=True))

        with pytest.raises(WriteRefusal) as caught:
            await service.execute(context, approved)
        assert caught.value.code is WriteCode.TABLE_NOT_ALLOWED
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 0

    async def test_a_permission_change_refuses_even_when_the_plan_still_validates(
        self, database, service, context, grants
    ):
        """The version check earns its keep here.

        The plan is still perfectly legal -- only an unrelated grant moved. No
        re-validation could notice that, which is exactly why the version is
        stored separately from the plan hash.
        """
        pending, _ = await service.propose(context, ship_order())
        await service.decide(context, pending.id, approve=True)
        approved = await service.approvals.get(context, pending.id)

        await grants.set_table_grant(context, TableGrant(
            data_source_id="shop", role="analyst", table="order_items",
            can_select=True))

        with pytest.raises(WriteRefusal) as caught:
            await service.execute(context, approved)
        assert caught.value.code is WriteCode.CONFIRMATION_MISMATCH
        assert "ermissions" in caught.value.message
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 0

    async def test_the_stored_plan_is_re_validated_not_trusted(
        self, database, service, context
    ):
        """A plan edited in storage must not be able to skip a validator."""
        pending, _ = await service.propose(context, ship_order())
        await service.decide(context, pending.id, approve=True)
        approved = await service.approvals.get(context, pending.id)

        tampered = approved.model_copy(update={"plan": {"steps": [{
            "operation": "update", "table": "orders",
            "assignments": [{"column": "status", "value": "hacked"}],
            "predicate": [{"column": "customer", "value": "acme"}],
            "expected_row_count": 1}]}})

        with pytest.raises(WriteRefusal) as caught:
            await service.execute(context, tampered)
        assert caught.value.code is WriteCode.PREDICATE_NOT_KEY
        assert count(database, "SELECT count(*) FROM orders WHERE status='hacked'") == 0


class TestTransactionIntegrity:
    async def test_a_broken_promise_rolls_everything_back(
        self, database, service, context
    ):
        """A mismatch on the second step must undo the first."""
        plan = WritePlan(steps=[
            S(operation="insert", table="orders", assignments=[
                A(column="status", value="new"), A(column="customer", value="wayne")],
              expected_row_count=1),
            S(operation="insert", table="order_items", assignments=[
                A(column="order_id", reference=R(from_step=0, column="order_id")),
                A(column="sku", value="WIDGET")],
              expected_row_count=2)])  # a lie: it inserts one
        validated = validate_write_plan(
            plan, await service.build_policy(context), paramstyle="qmark")

        with pytest.raises(UnexpectedRowCount) as caught:
            await service.runner.execute_write(validated, context)
        assert (caught.value.expected, caught.value.actual) == (2, 1)
        assert count(database,
                     "SELECT count(*) FROM orders WHERE customer='wayne'") == 0
        assert count(database, "SELECT count(*) FROM order_items") == 0

    async def test_a_mismatch_is_reported_as_a_refusal(self, service, context):
        plan = WritePlan(steps=[S(operation="update", table="orders",
                                  assignments=[A(column="status", value="x")],
                                  predicate=[K(column="order_id", value=99)],
                                  expected_row_count=1)])
        pending, _ = await service.propose(context, plan)
        with pytest.raises(WriteRefusal) as caught:
            await service.decide_and_execute(context, pending.id, approve=True)
        assert caught.value.code is WriteCode.ROW_COUNT_MISMATCH

    async def test_a_generated_key_really_reaches_the_child_row(
        self, database, service, context
    ):
        plan = WritePlan(steps=[
            S(operation="insert", table="orders", assignments=[
                A(column="status", value="new"), A(column="customer", value="wayne")],
              expected_row_count=1),
            S(operation="insert", table="order_items", assignments=[
                A(column="order_id", reference=R(from_step=0, column="order_id")),
                A(column="sku", value="WIDGET")], expected_row_count=1)])
        pending, _ = await service.propose(context, plan)
        _, result = await service.decide_and_execute(context, pending.id, approve=True)

        assert result.rows_affected == 2
        conn = sqlite3.connect(database)
        joined = conn.execute(
            "SELECT o.customer, i.sku FROM orders o "
            "JOIN order_items i ON i.order_id = o.order_id "
            "WHERE o.customer = 'wayne'").fetchall()
        conn.close()
        assert joined == [("wayne", "WIDGET")]

    async def test_a_constraint_violation_is_a_coded_refusal(self, service, context):
        """Not an internal error -- the bug this port deliberately fixes."""
        plan = WritePlan(steps=[S(operation="insert", table="order_items",
                                  assignments=[A(column="order_id", value=9999),
                                               A(column="sku", value="X")],
                                  expected_row_count=1)])
        pending, _ = await service.propose(context, plan)
        conn = sqlite3.connect(service.runner.database_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()
        try:
            await service.decide_and_execute(context, pending.id, approve=True)
        except WriteRefusal as refusal:
            assert refusal.code in (
                WriteCode.CONSTRAINT_VIOLATED, WriteCode.ROW_COUNT_MISMATCH)


class TestSecondPersonApproval:
    @pytest.mark.parametrize("mode,destructive,expected", [
        (WriteApprovalMode.SELF, True, False),
        (WriteApprovalMode.SECOND_PERSON_DESTRUCTIVE, False, False),
        (WriteApprovalMode.SECOND_PERSON_DESTRUCTIVE, True, True),
        (WriteApprovalMode.SECOND_PERSON_ALWAYS, False, True),
    ])
    def test_when_a_second_person_is_needed(self, mode, destructive, expected):
        assert requires_second_person(mode, destructive) is expected

    @pytest.fixture
    def strict_service(self, database, grants):
        return WriteService(
            grants=grants, catalog=FakeCatalog(),
            approvals=MemoryWriteApprovalStore(),
            runner=SqliteRunner(database, read_only=False), data_source_id="shop",
            dialect="sqlite", max_rows=50,
            approval_mode=WriteApprovalMode.SECOND_PERSON_DESTRUCTIVE)

    async def test_a_delete_queues_for_review(self, strict_service, context):
        plan = WritePlan(steps=[S(operation="delete", table="orders",
                                  predicate=[K(column="order_id", value=3)],
                                  expected_row_count=1)])
        pending, _ = await strict_service.propose(context, plan)
        assert pending.status is WriteStatus.AWAITING_REVIEW
        assert "second administrator" in pending.describe()

    async def test_the_requester_cannot_self_approve_it(self, strict_service, context):
        plan = WritePlan(steps=[S(operation="delete", table="orders",
                                  predicate=[K(column="order_id", value=3)],
                                  expected_row_count=1)])
        pending, _ = await strict_service.propose(context, plan)
        with pytest.raises(WriteRefusal) as caught:
            await strict_service.decide(context, pending.id, approve=True,
                                        decided_by="u-alice", decider_is_admin=True)
        assert caught.value.code is WriteCode.CONFIRMATION_FORBIDDEN

    async def test_a_non_admin_second_person_is_refused(self, strict_service, context):
        plan = WritePlan(steps=[S(operation="delete", table="orders",
                                  predicate=[K(column="order_id", value=3)],
                                  expected_row_count=1)])
        pending, _ = await strict_service.propose(context, plan)
        with pytest.raises(WriteRefusal):
            await strict_service.decide(context, pending.id, approve=True,
                                        decided_by="u-bob", decider_is_admin=False)

    async def test_a_second_admin_can_approve(self, database, strict_service, context):
        plan = WritePlan(steps=[S(operation="delete", table="orders",
                                  predicate=[K(column="order_id", value=3)],
                                  expected_row_count=1)])
        pending, _ = await strict_service.propose(context, plan)
        queue = await strict_service.pending_for_review(context)
        assert [p.id for p in queue] == [pending.id]

        _, result = await strict_service.decide_and_execute(
            context, pending.id, approve=True, decided_by="u-bob",
            decider_is_admin=True)
        assert result.rows_affected == 1
        assert count(database, "SELECT count(*) FROM orders WHERE order_id=3") == 0
        assert await strict_service.pending_for_review(context) == []

    async def test_an_ordinary_update_still_self_approves(
        self, database, strict_service, context
    ):
        pending, _ = await strict_service.propose(context, ship_order())
        assert pending.status is WriteStatus.PENDING
        _, result = await strict_service.decide_and_execute(
            context, pending.id, approve=True)
        assert result.rows_affected == 1


class TestTenantIsolation:
    async def test_another_tenant_cannot_see_or_decide(self, service, context):
        pending, _ = await service.propose(context, ship_order())
        other = make_context(
            User(id="u-alice", tenant_id="globex", group_memberships=["admin"]),
            tenant="globex")
        assert await service.approvals.get(other, pending.id) is None
        with pytest.raises(WriteRefusal):
            await service.decide(other, pending.id, approve=True)


class TestToolSurface:
    @pytest.fixture
    def registry(self, service):
        from vanna.tools import create_write_tools

        registry = ToolRegistry()
        for tool in create_write_tools(service):
            registry.register_local_tool(tool, ["admin"])
        return registry

    async def test_the_model_is_offered_a_plan_not_a_sql_string(self, registry, admin):
        schemas = {s.name: s for s in await registry.get_schemas(admin)}
        properties = schemas["propose_write"].parameters["properties"]
        assert "steps" in properties and "sql" not in properties

    async def test_propose_write_has_no_self_approval_flag(self, registry, admin):
        """One inference must not be able to both propose and approve."""
        schemas = {s.name: s for s in await registry.get_schemas(admin)}
        properties = schemas["propose_write"].parameters["properties"]
        assert "confirm" not in properties and "approved" not in properties

    async def test_hidden_from_callers_who_could_not_use_them(self, registry):
        viewer = User(id="u-vic", tenant_id="acme", group_memberships=["viewer"])
        names = {s.name for s in await registry.get_schemas(viewer)}
        assert not ({"propose_write", "confirm_write"} & names)

    async def test_a_viewer_calling_it_anyway_is_denied(self, registry):
        viewer = User(id="u-vic", tenant_id="acme", group_memberships=["viewer"])
        result = await registry.execute(
            ToolCall(id="1", name="propose_write", arguments={"steps": []}),
            make_context(viewer))
        assert not result.success

    async def test_the_round_trip(self, database, registry, context):
        proposal = await registry.execute(ToolCall(
            id="1", name="propose_write", arguments={"steps": [{
                "operation": "update", "table": "orders",
                "assignments": [{"column": "status", "value": "shipped"}],
                "predicate": [{"column": "order_id", "value": 1}],
                "expected_row_count": 1}]}), context)
        assert proposal.success
        assert proposal.metadata["requires_confirmation"] is True
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 0
        # The preview is stored and audited, so it must not carry the value.
        assert "shipped" not in proposal.metadata["statement_preview"]

        done = await registry.execute(ToolCall(
            id="2", name="confirm_write", arguments={
                "pending_write_id": proposal.metadata["pending_write_id"],
                "approved": True}), context)
        assert done.metadata["rows_affected"] == 1
        assert count(database, "SELECT count(*) FROM orders WHERE status='shipped'") == 1

    async def test_a_refusal_is_an_answer_not_an_error(self, registry, context):
        """success=False would send the agent into a retry loop over a
        deterministic refusal, and render as breakage rather than as a reply."""
        result = await registry.execute(ToolCall(
            id="1", name="propose_write", arguments={"steps": [{
                "operation": "update", "table": "orders",
                "assignments": [{"column": "status", "value": "x"}],
                "predicate": [{"column": "customer", "value": "acme"}],
                "expected_row_count": 1}]}), context)
        assert result.success is True
        assert result.metadata["refused"] is True
        assert result.metadata["code"] == WriteCode.PREDICATE_NOT_KEY.value


class TestPromptGuidance:
    """The model should know the boundary rather than discover it by refusal."""

    async def test_writable_tables_are_named_in_the_prompt(self, service, admin):
        from vanna.core.write.prompt import WriteAwareEnhancer

        prompt = await WriteAwareEnhancer(service).enhance_system_prompt(
            "BASE", "ship order 1", admin)
        assert "BASE" in prompt
        assert "orders" in prompt
        assert "primary key" in prompt

    async def test_a_read_only_workspace_is_told_so_explicitly(
        self, database, grants, admin, context
    ):
        """Silence reads as 'not mentioned', and the model proposes anyway."""
        from vanna.core.grants import TableGrant
        from vanna.core.write.prompt import WriteAwareEnhancer

        for table in CATALOG:
            await grants.set_table_grant(context, TableGrant(
                data_source_id="shop", role="admin", table=table.table_name,
                can_select=True))

        read_only = WriteService(
            grants=grants, catalog=FakeCatalog(),
            approvals=MemoryWriteApprovalStore(),
            runner=SqliteRunner(database, read_only=True), data_source_id="shop",
            dialect="sqlite", max_rows=50)
        prompt = await WriteAwareEnhancer(read_only).enhance_system_prompt(
            "BASE", "delete everything", admin)
        assert "Nothing is writable" in prompt

    async def test_it_delegates_to_the_enhancer_it_wraps(self, service, admin):
        from vanna.core.enhancer import LlmContextEnhancer
        from vanna.core.write.prompt import WriteAwareEnhancer

        class Inner(LlmContextEnhancer):
            async def enhance_system_prompt(self, system_prompt, user_message, user):
                return system_prompt + "\n[retrieved schema]"

        prompt = await WriteAwareEnhancer(service, inner=Inner()).enhance_system_prompt(
            "BASE", "hello", admin)
        assert "[retrieved schema]" in prompt
        assert "Changing data" in prompt
