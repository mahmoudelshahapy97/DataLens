-- Make history search and cost reporting survive a real amount of data.
--
-- The history view filters with `question ILIKE '%…%' OR sql ILIKE '%…%'` against
-- a table indexed only by (tenant_id, created_at). Neither predicate can use that
-- index, so every search was a sequential scan over the fastest-growing table in
-- the system -- fine on a demo, quadratically worse every month in production.
--
-- pg_trgm rather than a tsvector column. Full-text search stems and tokenises,
-- which is right for prose and wrong here: people search history for fragments of
-- SQL and half-remembered column names, and `WHERE amount_cents` is not a word.
-- Trigram indexes accelerate exactly the LIKE pattern already being used, so the
-- query does not change -- only its plan does.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS generations_question_trgm_idx
    ON vanna_app.generations USING gin (question gin_trgm_ops);

CREATE INDEX IF NOT EXISTS generations_sql_trgm_idx
    ON vanna_app.generations USING gin (sql gin_trgm_ops);

-- The "mine" filter on the history view, and per-user cost attribution.
CREATE INDEX IF NOT EXISTS generations_user_idx
    ON vanna_app.generations (tenant_id, user_id, created_at DESC);

-- Cost rollups scan a date range for one tenant and sum. The existing
-- (tenant_id, created_at DESC) index serves the range; including the payload
-- columns lets the rollup answer from the index alone.
CREATE INDEX IF NOT EXISTS generations_cost_idx
    ON vanna_app.generations (tenant_id, created_at)
    INCLUDE (cost_usd, prompt_tokens, completion_tokens, model);

-- Retention deletes by age across every tenant.
CREATE INDEX IF NOT EXISTS generations_created_idx
    ON vanna_app.generations (created_at);
