-- Reports: a dashboard, on a schedule, delivered somewhere.
--
-- There is no `reports` table here, and that absence is the design. A report in
-- this system is already a `Dashboard` that declares `parameters` -- the model
-- says so (`backend/vanna/dashboards/models.py`, the comment on `Dashboard.
-- parameters`) and `tools/seed_reports.py` seeds fifty of them. What was missing
-- was never the document. It was the *schedule*, the *delivery* and the *record
-- of what was sent*, which is exactly what these three tables add.
--
-- The alternative -- a report entity owning its own SQL, the way most products do
-- it -- would mean a second thing to verify, a second thing to render, and a
-- second place for a tile's chart specification to live. It also cannot express a
-- report with more than one panel without reinventing tiles.
--
-- ----------------------------------------------------------------------
-- The property this whole file is built around
-- ----------------------------------------------------------------------
--
-- **Tiles execute as the caller.** Two people opening the same dashboard see
-- different numbers, because the row and column rules that apply to their
-- questions apply there too. It is the guarantee the product is sold on and the
-- reason an export can never become a way around a grant.
--
-- A scheduled run has no caller. So one is named: `report_schedules.run_as`. The
-- run executes with that member's identity, through the same
-- `render_dashboard(registry, user, ...)` path a browser request uses -- not a
-- service account, not a superuser, and not "whoever created the schedule" read
-- from a column that nobody rechecks. Membership is verified again at execution
-- time, so removing somebody from a workspace stops their reports with the next
-- tick rather than at the next code change.
--
-- Get this wrong and a schedule is a grant bypass with a mail server attached.

-- ----------------------------------------------------------------------
-- Schedules
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.report_schedules (
    id              text        PRIMARY KEY,
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,

    -- Deleting the dashboard deletes its schedules. A schedule pointing at a
    -- document that no longer exists is a run that fails every tick forever, and
    -- it fails in a background loop where nobody is watching.
    dashboard_id    text        NOT NULL REFERENCES vanna_app.dashboards(id) ON DELETE CASCADE,

    name            text        NOT NULL,

    -- Values bound to the dashboard's declared parameters. Validated against
    -- those declarations on write *and* again before a run: the dashboard is
    -- editable, so a parameter this schedule binds can be renamed out from under
    -- it. `params.resolve` refuses a name the document does not declare, which is
    -- what keeps this column off the SQL injection path -- it is never
    -- interpolated, only matched against a declaration and then rendered by type.
    parameters      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Standard five-field crontab. Parsed by `vanna_app.reports.next_occurrence`
    -- rather than a dependency: the five fields are a small, closed grammar, and a
    -- scheduler library would need a shared jobstore to be safe across the four
    -- uvicorn workers -- strictly more machinery than a `next_run_at` column.
    cron            text        NOT NULL,

    -- An IANA name. "09:00 daily" means nothing without one, and storing the
    -- offset instead would drift by an hour twice a year.
    timezone        text        NOT NULL DEFAULT 'UTC',

    -- The member the tiles execute as. See the note at the top of this file.
    -- An email rather than a user id: `tenant_users` is keyed that way and the
    -- resolver takes an address, so a foreign key here would name a row the
    -- execution path does not look at.
    run_as          text        NOT NULL,

    -- [{"kind": "email", "target": "..."}, {"kind": "webhook", "target": "https://..."},
    --  {"kind": "inapp", "target": "..."}]
    -- A list because one report legitimately goes to a mailing list *and* a Slack
    -- channel, and modelling that as three schedules means three executions of
    -- the same SQL.
    channels        jsonb       NOT NULL DEFAULT '[]'::jsonb,

    -- 'html' reuses `dashboards/export.py` -- the same self-contained, offline,
    -- correctly-permissioned artifact the Export button produces. 'csv' is a zip
    -- of one file per tile. Neither needs a new dependency, which is why there is
    -- no 'pdf': that means weasyprint or a headless browser in the API image, and
    -- the HTML export is the better artifact anyway.
    format          text        NOT NULL DEFAULT 'html'
                    CHECK (format IN ('html', 'csv')),

    is_active       boolean     NOT NULL DEFAULT true,

    -- When this schedule is next due. Advanced by the materialiser under an
    -- advisory lock, which is what makes "exactly one run per tick" true with
    -- four workers rather than approximately true.
    next_run_at     timestamptz,
    last_run_at     timestamptz,

    created_by      text        NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- The materialiser's query: due, active, in cron order.
CREATE INDEX IF NOT EXISTS report_schedules_due_idx
    ON vanna_app.report_schedules (next_run_at)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS report_schedules_tenant_idx
    ON vanna_app.report_schedules (tenant_id, name);

-- ----------------------------------------------------------------------
-- Runs
-- ----------------------------------------------------------------------
--
-- One row per execution, whether scheduled or asked for by hand. It is the queue,
-- the history and the artifact store at once, and that is deliberate: a separate
-- queue table would have to be kept consistent with a separate history table
-- across a crash, and the interesting question -- "did Tuesday's report go out,
-- and what did it say?" -- needs both halves anyway.

CREATE TABLE IF NOT EXISTS vanna_app.report_runs (
    id              text        PRIMARY KEY,
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,

    -- NULL means somebody pressed Run now. Deleting a schedule keeps its runs and
    -- orphans them rather than erasing the record that they were delivered --
    -- "we never sent that" is a claim the history has to be able to refute.
    schedule_id     text        REFERENCES vanna_app.report_schedules(id) ON DELETE SET NULL,

    -- Carried on the run rather than read through the schedule, for the same
    -- reason: a run must stay readable after its schedule is gone.
    dashboard_id    text        NOT NULL,
    run_as          text        NOT NULL,
    parameters      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    channels        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    format          text        NOT NULL DEFAULT 'html',

    status          text        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'claimed', 'running', 'succeeded', 'failed')),

    -- When it became due. The claim query orders by this, so a backlog drains
    -- oldest-first rather than in whatever order the rows happen to sit.
    run_at          timestamptz NOT NULL DEFAULT now(),

    -- Which worker took it. Not for correctness -- SKIP LOCKED handles that --
    -- but so a run stuck in 'running' can be traced to a process.
    claimed_by      text,
    claimed_at      timestamptz,
    started_at      timestamptz,
    finished_at     timestamptz,

    tile_count      integer     NOT NULL DEFAULT 0,
    row_count       integer     NOT NULL DEFAULT 0,
    error           text,

    -- The delivered bytes, kept so "send me that again" does not re-execute the
    -- warehouse and quietly return different numbers. bytea rather than a path:
    -- there is no object store in this deployment, the artifacts are small
    -- (~1.3MB for a full HTML export) and a file on a container filesystem is
    -- gone on the next deploy.
    artifact          bytea,
    artifact_filename text,
    artifact_bytes    integer   NOT NULL DEFAULT 0,

    -- Non-empty only for a run-now; a scheduled run is requested by the schedule.
    requested_by    text        NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The claim query. Partial, because the queue is the tiny live tail of a table
-- that grows forever -- a full index would be almost entirely history.
CREATE INDEX IF NOT EXISTS report_runs_claimable_idx
    ON vanna_app.report_runs (run_at)
    WHERE status = 'pending';

-- The history screen: one schedule, newest first.
CREATE INDEX IF NOT EXISTS report_runs_schedule_idx
    ON vanna_app.report_runs (tenant_id, schedule_id, created_at DESC);

-- Retention sweeps, which delete by age across every tenant.
CREATE INDEX IF NOT EXISTS report_runs_created_idx
    ON vanna_app.report_runs (created_at);

-- ----------------------------------------------------------------------
-- In-app notifications
-- ----------------------------------------------------------------------
--
-- The delivery channel with no egress. Worth having as its own table rather than
-- as a flag on the run: a member wants one list of "things that finished", and
-- reports will not be the only thing that ever lands in it -- a write approval
-- waiting on them belongs there too.

CREATE TABLE IF NOT EXISTS vanna_app.notifications (
    id              text        PRIMARY KEY,
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,

    -- Address, matching `run_as` and `tenant_users`.
    user_email      text        NOT NULL,

    -- 'report_run' today. Not a CHECK: a new kind must not need a migration, and
    -- an unrecognised kind renders with its title and link intact.
    kind            text        NOT NULL DEFAULT 'report_run',

    title           text        NOT NULL,
    body            text        NOT NULL DEFAULT '',

    -- An in-app path, never an absolute URL. A stored absolute URL is a stored
    -- open redirect the moment the deployment's hostname changes.
    link            text        NOT NULL DEFAULT '',

    read_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The bell's query: my unread, newest first.
CREATE INDEX IF NOT EXISTS notifications_unread_idx
    ON vanna_app.notifications (tenant_id, user_email, created_at DESC)
    WHERE read_at IS NULL;

CREATE INDEX IF NOT EXISTS notifications_inbox_idx
    ON vanna_app.notifications (tenant_id, user_email, created_at DESC);

-- ----------------------------------------------------------------------
-- Column masking
-- ----------------------------------------------------------------------
--
-- Until now a column was readable or it was *dropped from the model* -- and
-- dropping is deliberate. Projecting NULL instead silently corrupts AVG and SUM
-- and never tells the reader the number is wrong (`vanna/core/grants/resolve.py`).
--
-- Masking is a third option, and it is weaker than both. It belongs here anyway
-- because the alternative people reach for is to grant the column and hope, but
-- the *implementation* has to match how row rules already work: the expression is
-- rewritten inside the model's CTE, so a user's outer subquery sits above it and
-- cannot undo it. Masking applied to a result set after execution -- which is how
-- most products do it -- is defeated by a cache that does not know the caller's
-- role, and by any path that reads the rows before the masker runs.
--
--   none     the column is projected as itself (the default; nothing changes)
--   hash     md5 of the text. Equality and distinct-counts still work, which is
--            the point and also the leak: it is a pseudonym, not a redaction.
--   partial  first two characters, then '***'. Leaks a prefix, by construction.
--   null     projected as NULL of the right type. Honest about hiding, and
--            corrupts aggregates exactly as described above -- offered because a
--            reader who sees NULL knows something is missing, which is sometimes
--            worth more than a column that is not there at all.
--
-- `can_read = false` -- dropping the column -- remains the default and the
-- recommendation. Nothing here changes that.

ALTER TABLE vanna_app.column_grants
    ADD COLUMN IF NOT EXISTS mask_strategy text NOT NULL DEFAULT 'none';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'column_grants_mask_strategy_known'
    ) THEN
        ALTER TABLE vanna_app.column_grants
            ADD CONSTRAINT column_grants_mask_strategy_known
            CHECK (mask_strategy IN ('none', 'hash', 'partial', 'null'));
    END IF;
END $$;

-- A mask on a column nobody may read is a rule that never fires, and it reads in
-- an admin screen as protection that is not there. Restating the invariant as a
-- constraint rather than trusting the API: the models guard the API, the
-- constraints guard a migration, a psql session and every future store.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'column_grants_mask_requires_read'
    ) THEN
        ALTER TABLE vanna_app.column_grants
            ADD CONSTRAINT column_grants_mask_requires_read
            CHECK (mask_strategy = 'none' OR can_read);
    END IF;
END $$;
