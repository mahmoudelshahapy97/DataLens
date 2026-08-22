-- Shared counters for quota, rate limiting and login throttling.
--
-- All three used to be Python dictionaries. That has two consequences and both are
-- worse than they sound.
--
-- The limits multiply by the worker count. Two uvicorn workers enforcing "200 per
-- day" independently permit 400, and nobody can tell from the outside which number
-- is real. In practice this pinned the deployment to a single worker -- the entire
-- horizontal-scaling story, given up to avoid one table.
--
-- The login throttle was also unbounded: `defaultdict(deque)` keyed by address and
-- IP, with entries created on every attempt and removed only on success. Spraying
-- addresses grew it forever.
--
-- A fixed window is deliberate rather than a sliding one. A sliding window needs
-- the individual event timestamps, which is a row per request; a fixed window is
-- one row per (key, window) and a single atomic UPSERT. The cost is a boundary
-- effect -- up to 2x the limit across a window edge -- which is acceptable for a
-- daily quota and a per-minute burst guard, and is not acceptable for the login
-- throttle, so that one uses a short window where the effect is small.

CREATE TABLE IF NOT EXISTS vanna_app.counters (
    -- 'quota:tenant:acme' | 'rate:acme:ada@example.com' | 'login:ip:203.0.113.4'
    bucket_key   text        NOT NULL,
    -- Start of the fixed window this row counts, truncated by the caller so every
    -- process agrees on the boundary without coordinating.
    window_start timestamptz NOT NULL,
    count        integer     NOT NULL DEFAULT 0,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (bucket_key, window_start)
);

-- Garbage collection reads by age across all keys.
CREATE INDEX IF NOT EXISTS counters_window_idx
    ON vanna_app.counters (window_start);
