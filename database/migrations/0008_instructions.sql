-- Business rules move from the filesystem into the control plane.
--
-- They lived as `knowledge/<tenant>/rules/*.md`, one file per rule named after a
-- slug of its own text. That naming carried two bugs a year of use would find:
-- two rules sharing their first sixty characters resolved to the same filename
-- and the second silently destroyed the first, and editing a rule's wording
-- moved it to a new file and orphaned the old one, so the rule appeared twice.
-- Identity here is a uuid, so neither is expressible.
--
-- It also puts rules where the rest of a workspace's state already is: covered by
-- the same backup, deleted by the same purge, and readable by every replica
-- rather than by whichever ones share a volume.
--
-- The platform baseline is deliberately NOT rows in this table. It lives in
-- instructions/baseline.yml and is merged at read time, so editing it reaches
-- every workspace on deploy instead of needing a backfill -- and so that no
-- DELETE, however privileged, can remove a rule the platform owns.

CREATE TABLE IF NOT EXISTS vanna_app.instructions (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    text        text        NOT NULL,
    scope       text        NOT NULL DEFAULT 'global',
    scope_ref   text,
    priority    integer     NOT NULL DEFAULT 0,
    enabled     boolean     NOT NULL DEFAULT true,

    -- 'tenant' was written here; 'library' was copied from a starter pack and is
    -- owned by the workspace from that moment. 'platform' is absent on purpose:
    -- a platform rule is never a row.
    origin      text        NOT NULL DEFAULT 'tenant',
    source_pack text,

    metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,

    CONSTRAINT instructions_scope_check
        CHECK (scope IN ('global', 'data_source', 'table', 'group')),
    CONSTRAINT instructions_origin_check
        CHECK (origin IN ('tenant', 'library')),
    CONSTRAINT instructions_text_check
        CHECK (length(btrim(text)) > 0),
    -- The invariant the stores also enforce in Python, restated where no code
    -- path can go around it. A non-global scope with no reference applies to
    -- nothing, and is indistinguishable from a rule that was silently dropped.
    CONSTRAINT instructions_scope_ref_check CHECK (
        scope = 'global' OR (scope_ref IS NOT NULL AND length(btrim(scope_ref)) > 0))
);

-- Resolution reads one tenant's rules in priority order, every request.
CREATE INDEX IF NOT EXISTS instructions_tenant_idx
    ON vanna_app.instructions (tenant_id, priority DESC, created_at);

-- Which platform rules a workspace has switched off.
--
-- Only the exceptions are stored. Absence means "on", which is what lets a newly
-- shipped baseline rule apply everywhere the moment it deploys rather than
-- needing a row written per workspace first.
CREATE TABLE IF NOT EXISTS vanna_app.instruction_overrides (
    tenant_id   text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    baseline_id text        NOT NULL,
    disabled    boolean     NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text,
    PRIMARY KEY (tenant_id, baseline_id)
);

-- Which starter packs a workspace has taken, so enabling one twice is a no-op
-- rather than a second copy of every rule in it.
CREATE TABLE IF NOT EXISTS vanna_app.instruction_packs_enabled (
    tenant_id  text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    pack_id    text        NOT NULL,
    enabled_at timestamptz NOT NULL DEFAULT now(),
    enabled_by text,
    PRIMARY KEY (tenant_id, pack_id)
);

-- Set once, by the one-shot markdown import.
--
-- Load-bearing, not bookkeeping. The obvious import rule -- "bring the files in
-- when the table is empty" -- resurrects every rule an administrator
-- deliberately deleted, on the next restart. `_seed_knowledge` already carries a
-- comment about exactly that failure for examples; this is how instructions
-- avoid it.
ALTER TABLE vanna_app.tenants
    ADD COLUMN IF NOT EXISTS instructions_imported_at timestamptz;
