-- Per-table and per-column grants: the authorization surface writes are checked against.
--
-- Until now a workspace's `allow_writes` flag was the whole permission model: on, and
-- an admin could change anything the connection string reached. That is a switch, not a
-- permission -- there was no way to say "this agent may update order status and nothing
-- else", which is the only shape of write access anyone actually wants to grant.
--
-- Two tables rather than one, because the questions differ. A table grant answers "may
-- this role touch this relation, and with which verb". A column grant answers "may this
-- role read, filter on, aggregate, or assign this field". Collapsing them would force
-- every column to inherit its table's verbs, and the common case -- readable table, two
-- writable columns -- would become inexpressible.
--
-- The CHECK constraints restate an invariant the Pydantic models also enforce: a write
-- flag requires its read flag. The duplication is deliberate. The models guard the API,
-- the constraints guard everything else -- a migration, a psql session, a future store
-- implementation -- and the invariant is one nobody should be able to break by taking a
-- different path to the same row.

CREATE TABLE IF NOT EXISTS vanna_app.table_grants (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,
    -- Matches an entry in the caller's group memberships. A caller holding several
    -- roles receives the union of their grants, so adding a role can only widen.
    role            text        NOT NULL,
    -- Normalized (casefolded, unquoted) name -- the key a write plan is matched on.
    table_key       text        NOT NULL,
    -- The same name as an administrator typed it, kept only so an admin screen can
    -- show `Sales.Orders` rather than `sales.orders`.
    table_name      text        NOT NULL,

    can_select      boolean     NOT NULL DEFAULT false,
    can_insert      boolean     NOT NULL DEFAULT false,
    can_update      boolean     NOT NULL DEFAULT false,
    can_delete      boolean     NOT NULL DEFAULT false,

    granted_by      text,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, role, table_key),

    CONSTRAINT table_grants_insert_requires_select CHECK (NOT can_insert OR can_select),
    CONSTRAINT table_grants_update_requires_select CHECK (NOT can_update OR can_select),
    CONSTRAINT table_grants_delete_requires_select CHECK (NOT can_delete OR can_select)
);

CREATE TABLE IF NOT EXISTS vanna_app.column_grants (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,
    role            text        NOT NULL,
    table_key       text        NOT NULL,
    column_key      text        NOT NULL,
    table_name      text        NOT NULL,
    column_name     text        NOT NULL,

    can_read        boolean     NOT NULL DEFAULT false,
    -- Separate from can_read because they leak differently: a column you may filter on
    -- but not read still answers questions about individual rows, one predicate at a time.
    can_filter      boolean     NOT NULL DEFAULT false,
    can_aggregate   boolean     NOT NULL DEFAULT false,
    can_write       boolean     NOT NULL DEFAULT false,

    granted_by      text,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, role, table_key, column_key),

    CONSTRAINT column_grants_filter_requires_read    CHECK (NOT can_filter    OR can_read),
    CONSTRAINT column_grants_aggregate_requires_read CHECK (NOT can_aggregate OR can_read),
    CONSTRAINT column_grants_write_requires_read     CHECK (NOT can_write     OR can_read)
);

-- Resolution reads every grant for one (tenant, data source) at once.
CREATE INDEX IF NOT EXISTS column_grants_lookup_idx
    ON vanna_app.column_grants (tenant_id, data_source_id, table_key);

-- The version a write is approved under.
--
-- A write is authorized once when it is proposed and again when it executes, and those
-- are separated by however long a human takes to read an approval card. Without a
-- number to compare, a grant revoked in between would be revoked in name only: the
-- approved plan would still carry permissions that no longer exist. Every mutation
-- above bumps this in the same transaction, so a reader can never see a changed grant
-- under an unchanged version.
CREATE TABLE IF NOT EXISTS vanna_app.grant_versions (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,
    version         bigint      NOT NULL DEFAULT 1,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id),

    -- 0 means "never granted anything", which resolution reports without a row here.
    -- A stored version is therefore always a real change.
    CONSTRAINT grant_versions_positive CHECK (version > 0)
);
