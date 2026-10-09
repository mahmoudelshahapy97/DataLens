-- Agent memory, persisted.
--
-- `Platform` built its memory from a hardcoded `EphemeralMemory` stub: every
-- save was a no-op, every search returned an empty list, and nothing survived a
-- request -- let alone a restart. That is why the chat's own status line read
-- `Memory ✗` and why `/memories` answered "No recent memories found" however
-- much the agent had been used.
--
-- Two kinds of memory, two tables, because they are searched differently and
-- deleted separately:
--
-- `agent_tool_memory` is what the agent *did* -- a question, the tool it chose
-- and the arguments it passed. This is what makes "somebody already asked this"
-- work: a similar question retrieves the tool call that answered it.
--
-- `agent_text_memory` is what somebody *told* it to remember -- a preference, a
-- convention, a correction. Free text, no tool, no arguments.
--
-- Both are scoped `(tenant_id, user_id)`. Tenant because one workspace's saved
-- patterns must never surface in another's retrieval -- the same rule every
-- other table here follows. User because a memory is frequently personal ("I
-- mean net revenue"), and a preference leaking between colleagues in the same
-- workspace is its own kind of wrong answer. A caller that wants the whole
-- workspace's memories passes no user.

CREATE TABLE IF NOT EXISTS vanna_app.agent_tool_memory (
    memory_id       text        PRIMARY KEY,
    tenant_id       text        NOT NULL,
    user_id         text        NOT NULL DEFAULT '',
    question        text        NOT NULL DEFAULT '',
    tool_name       text        NOT NULL DEFAULT '',
    args            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- A failed call is worth keeping: "this approach did not work" is a useful
    -- thing to retrieve, and dropping failures makes the store look better than
    -- the system is.
    success         boolean     NOT NULL DEFAULT true,
    metadata        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vanna_app.agent_text_memory (
    memory_id       text        PRIMARY KEY,
    tenant_id       text        NOT NULL,
    user_id         text        NOT NULL DEFAULT '',
    content         text        NOT NULL,
    metadata        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Recent-first listing per scope is the read both tables get most: `/memories`
-- and the retrieval step that runs before every question.
CREATE INDEX IF NOT EXISTS agent_tool_memory_recent_idx
    ON vanna_app.agent_tool_memory (tenant_id, user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS agent_text_memory_recent_idx
    ON vanna_app.agent_text_memory (tenant_id, user_id, created_at DESC);

-- Search is a trigram similarity over the question text rather than embeddings.
--
-- Deliberate: an embedding store is a second piece of infrastructure to run and
-- a model to keep in sync, and the retrieval this serves is "has somebody asked
-- something like this" over one workspace's questions -- hundreds, not
-- millions. `pg_trgm` answers that in the database that is already here. If it
-- is unavailable the store falls back to ILIKE, which is worse but not absent.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS agent_tool_memory_question_trgm_idx
    ON vanna_app.agent_tool_memory USING gin (question gin_trgm_ops);

CREATE INDEX IF NOT EXISTS agent_text_memory_content_trgm_idx
    ON vanna_app.agent_text_memory USING gin (content gin_trgm_ops);
