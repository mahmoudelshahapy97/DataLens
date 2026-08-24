-- More than one database per workspace.
--
-- Until now `tenants.database_url` was the whole story: one workspace, one
-- connection string, and switching database meant switching workspace. That is a
-- reasonable model right up to the point where the same team owns two databases
-- and the same permission matrix should govern both.
--
-- Almost nothing has to change to support it, because the permission model was
-- already shaped this way. `table_grants`, `column_grants` and `grant_policies`
-- are all keyed `(tenant_id, data_source_id, ...)`, and migration 0010 keyed the
-- catalog the same way. The data source has been a first-class dimension
-- everywhere except in the one table that decides which databases exist.
--
-- Two things here.

-- ----------------------------------------------------------------------
-- The registry
-- ----------------------------------------------------------------------
--
-- `data_source_id` is the credential-free label `describe_data_source()` derives
-- from the URL -- `postgresql://db_postgres/chinook`. It is what every grant row
-- already carries, so a workspace's second database inherits the matrix machinery
-- with no translation layer.
--
-- The URL itself is encrypted exactly as `tenants.database_url` is, by the same
-- `Cipher`, and is never returned by an API that does not explicitly ask.

CREATE TABLE IF NOT EXISTS vanna_app.tenant_datasources (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,

    -- Encrypted at rest. Same treatment as tenants.database_url.
    database_url    text        NOT NULL,

    -- What a human calls it. `data_source_id` is derived from the URL and changes
    -- if the workspace is repointed; this does not, so a screen has something
    -- stable to show.
    label           text        NOT NULL DEFAULT '',

    -- Which one a new conversation gets when nobody chooses. Exactly one per
    -- workspace, enforced by the partial unique index below -- "no default" and
    -- "two defaults" are both states where the answer to "which database?"
    -- depends on row order.
    is_default      boolean     NOT NULL DEFAULT false,

    is_active       boolean     NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_datasources_default_idx
    ON vanna_app.tenant_datasources (tenant_id)
    WHERE is_default;

-- Backfill, so no existing workspace changes behaviour.
--
-- Every tenant with a URL gets exactly one registered data source, marked
-- default, and the label is left blank so the UI falls back to the derived id.
-- A tenant with a NULL URL uses the server default and has nothing to register --
-- it keeps working through the same fallback it always did.
--
-- The id is computed by the application rather than here: `describe_data_source`
-- has dialect-specific rules (file-backed engines have no host, so the path is
-- the whole address) that are not worth reimplementing in SQL and would drift the
-- moment either copy changed. The application backfills on first boot; this
-- migration only makes the table exist.

-- ----------------------------------------------------------------------
-- Conversations remember which database they are about
-- ----------------------------------------------------------------------
--
-- The binding is per conversation, not per message. A thread's history is a
-- record of questions asked against one schema, and letting the target change
-- mid-thread means the model reads earlier turns describing tables that are no
-- longer in scope -- and that a client could switch database by editing one
-- header on one request.
--
-- NULL means "whatever the workspace default is", which is every conversation
-- that existed before this column did.
ALTER TABLE vanna_app.conversations
    ADD COLUMN IF NOT EXISTS data_source_id text;
