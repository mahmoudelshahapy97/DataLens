-- Right to be forgotten, as a workflow with a paper trail.
--
-- Two decisions here differ from how this is usually built, and both are
-- deliberate.
--
-- ----------------------------------------------------------------------
-- 1. Two people, not one
-- ----------------------------------------------------------------------
--
-- A request is *created* by one platform admin and *executed* by another. The
-- columns below are what makes that checkable rather than a convention.
--
-- The alternative -- one admin doing both -- makes irreversible destruction of
-- another person's data a single click by a single account. QueryLite gates the
-- equivalent endpoints on "admin in ANY workspace", which means anybody who
-- creates their own workspace becomes an admin of it and thereby acquires
-- cross-tenant deletion rights. That is not a threat model, it is an accident
-- waiting for the first person to notice.
--
-- ----------------------------------------------------------------------
-- 2. The audit trail survives the deletion
-- ----------------------------------------------------------------------
--
-- `generations` is deliberately not foreign-keyed to `tenants`, and the reason is
-- written at `sql_schema.sql`: "deleting a tenant must not silently erase the
-- record of what was asked under it". The same applies to a person.
--
-- So execution **redacts** where it cannot delete. Question text and SQL are
-- blanked, the user id is anonymised, and the row survives with its timing, cost
-- and token columns intact. Conversations, saved queries, dashboards they own,
-- sessions and API tokens are deleted outright.
--
-- This is not a loophole. The record that a deletion *happened*, and of what it
-- covered, is the compliance artifact -- it is what is produced when somebody
-- asks whether the request was honoured. Erasing it would defeat the feature it
-- belongs to, and a regulator asking "prove you deleted it" cannot be answered
-- by a system that deleted the proof.

CREATE TABLE IF NOT EXISTS vanna_app.deletion_requests (
    id              text        PRIMARY KEY,

    -- The subject. An address rather than a user id: a request can legitimately
    -- name somebody who has already been removed from every workspace, and a
    -- foreign key would make that unrepresentable.
    subject_email   text        NOT NULL,

    -- Empty means every workspace. A request scoped to one workspace is the
    -- common case -- a contractor leaving one client -- and erasing their account
    -- everywhere because of it would be its own incident.
    tenant_id       text        NOT NULL DEFAULT '',

    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'executing', 'completed', 'failed', 'cancelled')),

    -- The two-person rule, as data.
    requested_by    text        NOT NULL,
    executed_by     text,

    -- Why. Not optional in practice -- a deletion with no stated reason is one
    -- nobody can review later -- but not NOT NULL either, because refusing to
    -- record a request over a blank field helps nobody.
    notes           text        NOT NULL DEFAULT '',

    -- What execution actually did: counts per table, and what was redacted rather
    -- than deleted. This is the artifact somebody is shown when they ask whether
    -- the request was honoured, so it is stored rather than recomputed -- the
    -- rows it describes are gone and cannot be counted again.
    outcome         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error           text        NOT NULL DEFAULT '',

    created_at      timestamptz NOT NULL DEFAULT now(),
    executed_at     timestamptz,

    -- A person cannot approve their own request. Stated here as well as in the
    -- route, for the same reason every other invariant in this schema is: the
    -- models guard the API, the constraint guards a migration, a psql session and
    -- every future store.
    CONSTRAINT deletion_requests_two_person
        CHECK (executed_by IS NULL OR executed_by <> requested_by)
);

-- The queue an admin works from.
CREATE INDEX IF NOT EXISTS deletion_requests_pending_idx
    ON vanna_app.deletion_requests (created_at DESC)
    WHERE status = 'pending';

-- "Has this person been through this before?"
CREATE INDEX IF NOT EXISTS deletion_requests_subject_idx
    ON vanna_app.deletion_requests (subject_email, created_at DESC);
