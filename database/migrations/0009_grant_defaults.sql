-- Grant defaults: what a workspace grants before anybody edits the matrix.
--
-- Until now a new workspace granted nothing, with no screen on which to change
-- that, so the permission model was theoretically complete and practically
-- unreachable. Two things fix that: a preset that fills in a sensible starting
-- matrix, and a per-workspace record of which preset that was.

-- Where a grant row came from.
--
-- The reason a preset can be re-applied at all. Without it, "re-apply" either
-- discards an administrator's own edits or cannot discard anything, and neither
-- is usable. 'explicit' is the default precisely so that every row written before
-- this migration is protected: a later `replace` deletes only what a preset wrote.
ALTER TABLE vanna_app.table_grants
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'explicit';
ALTER TABLE vanna_app.column_grants
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'explicit';

DO $$ BEGIN
    ALTER TABLE vanna_app.table_grants
        ADD CONSTRAINT table_grants_source_check
        CHECK (source IN ('explicit', 'preset'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE vanna_app.column_grants
        ADD CONSTRAINT column_grants_source_check
        CHECK (source IN ('explicit', 'preset'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Preset rows are rewritten as a set, which is the only reason the filter above
-- has to be fast.
CREATE INDEX IF NOT EXISTS table_grants_source_idx
    ON vanna_app.table_grants (tenant_id, data_source_id, role)
    WHERE source = 'preset';
CREATE INDEX IF NOT EXISTS column_grants_source_idx
    ON vanna_app.column_grants (tenant_id, data_source_id, role)
    WHERE source = 'preset';

-- What a workspace grants by default, per role, per connection.
--
-- Intent, not permission. Nothing in this table is consulted when a caller's
-- access is resolved: applying a policy writes ordinary rows into `table_grants`
-- and `column_grants`, and those are what resolution reads.
--
-- That indirection is the design, not an implementation detail. It keeps the
-- fail-closed rule intact -- a column with no row is dropped, with no second code
-- path that could disagree -- it means every change still moves `grant_versions`,
-- so a write approved under the old policy is re-authorized before it runs, and
-- it means the matrix an administrator reads is the same set of rows the resolver
-- reads, with no "effective versus stored" distinction to explain.
--
-- Keyed on the data source as well as the tenant because a workspace can be
-- repointed at a different database, and a policy written for one schema must not
-- follow it to another.
CREATE TABLE IF NOT EXISTS vanna_app.grant_policies (
    tenant_id            text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id       text        NOT NULL,
    role                 text        NOT NULL,

    -- 'none' is the default, and means today's behaviour: nothing is granted
    -- until somebody grants it. Not constrained to a list here, because a
    -- deployment may register its own preset; the API validates the name against
    -- what is actually registered.
    preset               text        NOT NULL DEFAULT 'none',

    -- Whether a table a later scan discovers is granted automatically. Off by
    -- default: a table that has just appeared is one nobody has looked at yet.
    apply_to_new_tables  boolean     NOT NULL DEFAULT false,

    -- Whether these grants also decide what may be *read*.
    --
    -- Off by default, and it must stay that way for an existing deployment.
    -- Until now grants governed writes only, so switching this on for a
    -- workspace with an empty matrix would deny every table at once. The API
    -- refuses to enable it before the role has grants.
    enforce_reads        boolean     NOT NULL DEFAULT false,

    last_applied_at      timestamptz,
    last_applied_version bigint,
    updated_by           text,
    updated_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, role)
);
