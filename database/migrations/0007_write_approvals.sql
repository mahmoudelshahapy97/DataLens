-- Writes awaiting a decision, and who has to make it.
--
-- Approval spans requests. A change is proposed in one turn, read and decided in
-- another, and the two may be minutes and several HTTP requests apart. The previous
-- attempt kept the pending write in `ToolContext.metadata`, which lives for exactly
-- one request -- so the approval could never arrive, and on a write-enabled workspace
-- every statement previewed forever and none ever ran. This table is the fix.
--
-- Note what is stored in its own column rather than folded into `plan_hash`:
-- `grants_version`, `catalog_fingerprint` and `expires_at`. The hash proves the
-- statement is the one that was shown. These prove the *world* it was shown in still
-- holds. Keeping them apart is what lets a refusal say which one moved -- "permissions
-- changed" is something a person can act on, "something changed" is not.
--
-- `plan` is the typed WritePlan, not SQL. It is re-parsed through its Pydantic model
-- and re-validated against freshly built permissions before it runs, so a row edited
-- here cannot skip a validator. The stored statements are rebuilt and compared, never
-- executed as stored.

ALTER TABLE vanna_app.tenants
    ADD COLUMN IF NOT EXISTS write_approval_mode text NOT NULL DEFAULT 'self';

DO $$
BEGIN
    ALTER TABLE vanna_app.tenants
        ADD CONSTRAINT tenants_write_approval_mode_check
        CHECK (write_approval_mode IN
               ('self', 'second_person_destructive', 'second_person_always'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS vanna_app.pending_writes (
    id                  uuid        PRIMARY KEY,
    tenant_id           text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id      text        NOT NULL,

    conversation_id     text,
    request_id          text,
    requested_by        text        NOT NULL,
    requested_by_email  text,

    status              text        NOT NULL DEFAULT 'pending',

    -- The typed plan. Never trusted as stored: re-parsed and re-validated first.
    plan                jsonb       NOT NULL,
    -- The statements as they will run, still parameterized. Safe to keep and to
    -- show, because the markers stand where the tenant's data would be.
    statement_preview   text        NOT NULL,
    -- Shapes only: counts, tables, verbs. Never the bound values.
    parameter_summary   jsonb       NOT NULL DEFAULT '{}'::jsonb,

    operation           text        NOT NULL,
    tables              jsonb       NOT NULL DEFAULT '[]'::jsonb,
    expected_row_count  integer     NOT NULL DEFAULT 0,
    is_destructive      boolean     NOT NULL DEFAULT false,

    -- Each independently checkable, so a refusal can name what moved.
    plan_hash           text        NOT NULL,
    grants_version      bigint      NOT NULL DEFAULT 0,
    catalog_fingerprint text,

    expires_at          timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    decided_by          text,
    decided_at          timestamptz,
    error               text,

    CONSTRAINT pending_writes_status_check CHECK (status IN (
        'pending', 'awaiting_review', 'approved', 'rejected',
        'expired', 'refused', 'executed', 'failed')),

    CONSTRAINT pending_writes_plan_hash_check CHECK (plan_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT pending_writes_rows_check CHECK (expected_row_count >= 0),

    -- A decided row must record who decided it, and an undecided one must not
    -- claim it was. Expired is the exception: nobody decided it, time did.
    CONSTRAINT pending_writes_decision_consistency CHECK (
        (status IN ('pending', 'awaiting_review')
            AND decided_by IS NULL AND decided_at IS NULL)
        OR (status = 'expired' AND decided_by IS NULL)
        OR (status IN ('approved', 'rejected', 'refused', 'executed', 'failed')
            AND decided_by IS NOT NULL AND decided_at IS NOT NULL)
    )
);

-- The review queue, and the expiry sweep. Partial because the settled rows are
-- the overwhelming majority and neither query ever wants them.
CREATE INDEX IF NOT EXISTS pending_writes_open_idx
    ON vanna_app.pending_writes (tenant_id, status, expires_at)
    WHERE status IN ('pending', 'awaiting_review');

-- "What did this workspace change, and who approved it."
CREATE INDEX IF NOT EXISTS pending_writes_history_idx
    ON vanna_app.pending_writes (tenant_id, created_at DESC);
