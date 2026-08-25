-- Remember whether a data source still answers.
--
-- `probe()` runs once, when a source is registered, and the result is thrown away
-- as soon as the request that asked for it ends. Nothing has recorded it since, so
-- a workspace whose warehouse password was rotated last week, or whose host moved,
-- looks identical to a healthy one until somebody asks a question and gets an
-- error they cannot interpret.
--
-- Three columns, deliberately not a separate table: this is the *current* state of
-- one row, not a history. A time series of connection checks is a monitoring
-- system's job, and inventing half of one here would mean growing a retention
-- policy for it.
--
-- Nullable, with no default: NULL means "never checked", which is a different
-- thing from "checked and healthy" and the console needs to say so differently.
-- A source registered before this migration is honestly unknown rather than
-- optimistically green.

ALTER TABLE vanna_app.tenant_datasources
    ADD COLUMN IF NOT EXISTS last_checked_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_ok         boolean,
    -- Sanitised on the way in. A driver's connection error routinely contains the
    -- host, the user and occasionally the password it tried; this is shown in a
    -- browser to anyone who administers the workspace.
    ADD COLUMN IF NOT EXISTS last_error      text;
