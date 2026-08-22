-- Audit trails: agent events, and administrative actions.
--
-- The library already defines an AuditLogger interface and the Agent constructor
-- already accepts one. Nothing ever passed an implementation, so every tool
-- invocation and access decision was unrecorded.
--
-- Administrative actions were worse: repointing a workspace at a different
-- database, granting someone admin, changing a plan, resetting a password -- all
-- existed only as lines on stdout. "Who changed this, and when" is the first
-- question asked after an incident and the logs are the wrong place to answer it,
-- because they are not queryable, not retained, and not scoped to a tenant.
--
-- Two tables rather than one. Agent events are high-volume, machine-shaped, and
-- interesting in aggregate. Admin events are rare, human-shaped, and interesting
-- individually -- and they must be readable by a tenant admin looking at their own
-- workspace, which agent-level tool traces should not be.

-- ----------------------------------------------------------------------
-- Agent events
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.audit_events (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        text        NOT NULL,
    event_type      text        NOT NULL,
    tenant_id       text        NOT NULL DEFAULT '',
    user_id         text        NOT NULL DEFAULT '',
    user_email      text        NOT NULL DEFAULT '',
    conversation_id text        NOT NULL DEFAULT '',
    request_id      text        NOT NULL DEFAULT '',
    tool_name       text,
    access_granted  boolean,
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_events_tenant_idx
    ON vanna_app.audit_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_request_idx
    ON vanna_app.audit_events (request_id);
-- Denials are what anyone actually goes looking for. A partial index keeps that
-- lookup cheap without paying for an index over the (much larger) success case.
CREATE INDEX IF NOT EXISTS audit_events_denied_idx
    ON vanna_app.audit_events (tenant_id, created_at DESC)
    WHERE access_granted = false;

-- ----------------------------------------------------------------------
-- Administrative actions
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.admin_audit (
    id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Who did it. Email rather than a foreign key: the record must survive the
    -- deletion of the account that made the change, which is exactly the case
    -- somebody will be investigating.
    actor_email  text        NOT NULL,
    actor_ip     text        NOT NULL DEFAULT '',
    -- 'tenant.update' | 'member.role_change' | 'billing.plan' | 'account.reset' ...
    action       text        NOT NULL,
    -- What it was done to. Tenant id where there is one, so a workspace admin can
    -- be shown their own workspace's history and nothing else.
    tenant_id    text        NOT NULL DEFAULT '',
    target       text        NOT NULL DEFAULT '',
    -- Before/after for the fields that changed. Credentials are redacted by the
    -- writer -- see record_admin_action -- so this column never holds a password.
    details      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    request_id   text        NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS admin_audit_tenant_idx
    ON vanna_app.admin_audit (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_actor_idx
    ON vanna_app.admin_audit (actor_email, created_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_action_idx
    ON vanna_app.admin_audit (action, created_at DESC);
