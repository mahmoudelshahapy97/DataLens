-- Baseline: the control plane as it stood before migrations existed.
--
-- Carried over verbatim from the idempotent DDL blob in the original tenancy.py so
-- that an existing deployment can adopt migrations without a rebuild: every object
-- here is IF NOT EXISTS, so applying 0001 to a database that already has these
-- tables records the version and changes nothing.
--
-- Later migrations are NOT written this way. From 0002 on, each migration assumes
-- the state the previous one left.

CREATE SCHEMA IF NOT EXISTS vanna_app;

-- Needed for gen_random_uuid(). In PostgreSQL 13+ it is built in, but the
-- extension is still the portable way to be sure.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ----------------------------------------------------------------------
-- Tenants and membership
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.tenants (
    id            text PRIMARY KEY,
    name          text        NOT NULL,
    description   text        NOT NULL DEFAULT '',
    -- NULL means "use the server default connection". Set per tenant to point a
    -- customer at their own database. Encrypted at rest from migration 0002.
    database_url  text,
    is_active     boolean     NOT NULL DEFAULT true,
    daily_quota   integer,
    max_rows      integer,
    -- Write statements for this workspace's admins. Off by default, and only half
    -- the decision: VANNA_ALLOW_WRITES must be set too.
    allow_writes  boolean     NOT NULL DEFAULT false,
    -- May members answer questions with their own LLM API key? Defaults to true
    -- because the alternative -- hitting a quota wall with no way past it -- is the
    -- situation the feature exists for.
    allow_byo_key boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Deployments that predate these columns.
ALTER TABLE vanna_app.tenants
    ADD COLUMN IF NOT EXISTS allow_writes boolean NOT NULL DEFAULT false;
ALTER TABLE vanna_app.tenants
    ADD COLUMN IF NOT EXISTS allow_byo_key boolean NOT NULL DEFAULT true;

CREATE TABLE IF NOT EXISTS vanna_app.tenant_users (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    -- Always stored lowercased by the application, so the unique constraint below
    -- actually prevents Ada@x.com and ada@x.com being two members.
    email        text        NOT NULL,
    full_name    text        NOT NULL DEFAULT '',
    role         text        NOT NULL DEFAULT 'analyst',
    is_active    boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz,
    UNIQUE (tenant_id, email),
    CONSTRAINT tenant_users_role_check CHECK (role IN ('admin', 'analyst', 'viewer'))
);

CREATE INDEX IF NOT EXISTS tenant_users_email_idx ON vanna_app.tenant_users (email);

CREATE TABLE IF NOT EXISTS vanna_app.starter_questions (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    question   text        NOT NULL,
    sort_order integer     NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------
-- Credentials
-- ----------------------------------------------------------------------

-- Identity is global; tenant_users remains the membership-and-role table, so one
-- account can belong to several workspaces with a different role in each.
CREATE TABLE IF NOT EXISTS vanna_app.users (
    email         text        PRIMARY KEY,
    password_hash text        NOT NULL,
    full_name     text        NOT NULL DEFAULT '',
    is_active     boolean     NOT NULL DEFAULT true,
    -- Forces a change on next login. Set when an admin issues a temporary password.
    must_change   boolean     NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Sessions are keyed by the SHA-256 of the token, never the token. A dump of this
-- table therefore yields nothing replayable.
CREATE TABLE IF NOT EXISTS vanna_app.sessions (
    token_hash text        PRIMARY KEY,
    email      text        NOT NULL REFERENCES vanna_app.users(email) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    user_agent text        NOT NULL DEFAULT '',
    ip         text        NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS sessions_email_idx ON vanna_app.sessions (email);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON vanna_app.sessions (expires_at);

-- Machine credentials: the CLI and `vanna mcp`, which cannot hold a cookie.
-- Separate from sessions so revoking a laptop does not sign out a scheduled job.
CREATE TABLE IF NOT EXISTS vanna_app.api_tokens (
    token_hash   text        PRIMARY KEY,
    email        text        NOT NULL REFERENCES vanna_app.users(email) ON DELETE CASCADE,
    name         text        NOT NULL DEFAULT '',
    expires_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);

CREATE INDEX IF NOT EXISTS api_tokens_email_idx ON vanna_app.api_tokens (email);

-- ----------------------------------------------------------------------
-- Billing
-- ----------------------------------------------------------------------

-- Attached to the tenant rather than the user because the quota is enforced per
-- workspace. Rows accumulate: a renewal or plan change inserts, it does not update,
-- so the history reads correctly. The current subscription is the newest row.
CREATE TABLE IF NOT EXISTS vanna_app.subscriptions (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    -- Names a plan in vanna.core.billing.PLANS. Deliberately not a foreign key:
    -- plans are code constants that ship with a release.
    plan         text        NOT NULL DEFAULT 'free',
    status       text        NOT NULL DEFAULT 'active',
    starts_at    timestamptz NOT NULL DEFAULT now(),
    -- NULL means open-ended, which is how an enterprise agreement with no end date
    -- is expressed. An expired subscription falls back to free, never to zero.
    expires_at   timestamptz,
    cancelled_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS subscriptions_tenant_idx
    ON vanna_app.subscriptions (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS vanna_app.payments (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    provider     text        NOT NULL DEFAULT 'manual',
    -- The provider's own identifier, and the reason this table is safe to write
    -- from a webhook: UNIQUE means a replayed delivery cannot bill twice.
    provider_ref text        NOT NULL UNIQUE,
    -- Integer cents. Money in a float is a rounding bug waiting for a large invoice.
    amount_cents integer     NOT NULL DEFAULT 0,
    currency     text        NOT NULL DEFAULT 'usd',
    status       text        NOT NULL DEFAULT 'succeeded',
    description  text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payments_tenant_idx
    ON vanna_app.payments (tenant_id, created_at DESC);

-- ----------------------------------------------------------------------
-- Content
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.conversations (
    id         text        PRIMARY KEY,
    tenant_id  text        NOT NULL,
    -- Owner. A conversation is one person's working notes, so it is scoped by user
    -- as well as tenant -- unlike saved queries and dashboards, which are
    -- deliberately shared across the workspace.
    user_id    text        NOT NULL,
    title      text        NOT NULL DEFAULT '',
    -- The whole Conversation model, messages included. Read and written as one unit
    -- by the agent on every turn, so a table per message would buy a join and cost
    -- write amplification.
    document   jsonb       NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversations_owner_idx
    ON vanna_app.conversations (tenant_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS vanna_app.dashboards (
    id         text        PRIMARY KEY,
    tenant_id  text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    title      text        NOT NULL,
    document   jsonb       NOT NULL,
    created_by text        NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dashboards_tenant_idx
    ON vanna_app.dashboards (tenant_id, title);

CREATE TABLE IF NOT EXISTS vanna_app.saved_queries (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    created_by text        NOT NULL DEFAULT '',
    title      text        NOT NULL,
    question   text        NOT NULL DEFAULT '',
    sql        text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS saved_queries_tenant_idx
    ON vanna_app.saved_queries (tenant_id, created_at DESC);

-- ----------------------------------------------------------------------
-- Generation lineage
-- ----------------------------------------------------------------------

-- Not a foreign key to tenants. Generations are an audit trail: deleting a tenant
-- must not silently erase the record of what was asked under it. Deliberate
-- erasure is a separate, explicit operation.
CREATE TABLE IF NOT EXISTS vanna_app.generations (
    id                    text        PRIMARY KEY,
    tenant_id             text        NOT NULL,
    data_source_id        text        NOT NULL DEFAULT 'default',
    user_id               text        NOT NULL DEFAULT '',
    conversation_id       text        NOT NULL DEFAULT '',
    request_id            text        NOT NULL DEFAULT '',
    question              text        NOT NULL DEFAULT '',
    sql                   text        NOT NULL DEFAULT '',
    status                text        NOT NULL DEFAULT 'valid',
    error                 text,
    error_kind            text,
    row_count             integer,
    truncated             boolean     NOT NULL DEFAULT false,
    execution_ms          double precision,
    repair_attempts       integer     NOT NULL DEFAULT 0,
    model                 text,
    prompt_tokens         integer,
    completion_tokens     integer,
    cost_usd              double precision,
    retrieved_example_ids jsonb       NOT NULL DEFAULT '[]'::jsonb,
    retrieved_table_names jsonb       NOT NULL DEFAULT '[]'::jsonb,
    retrieval_strategy    text,
    feedback              text,
    feedback_comment      text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    metadata              jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS generations_tenant_created_idx
    ON vanna_app.generations (tenant_id, created_at DESC);
-- Feedback arrives keyed by request_id, well after the row was written.
CREATE INDEX IF NOT EXISTS generations_request_idx
    ON vanna_app.generations (tenant_id, request_id);
