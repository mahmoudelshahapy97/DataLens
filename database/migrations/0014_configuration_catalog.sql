-- Configuration moves into the database.
--
-- Every semantic project, instruction pack and domain definition this deployment
-- runs on is a file on disk, read at runtime: `vanna_project.yml`, `cubes/*.yml`,
-- `models/*/metadata.yml`, `relationships.yml`, `target/mdl.json`,
-- `instructions/baseline.yml`, `domains/domains.yml`. That works for a checkout
-- and stops working the moment an administrator wants to change a cube from a
-- browser -- there is nowhere for the change to go except a file in an image.
--
-- These two tables are where it goes instead. PostgreSQL becomes the runtime
-- source of truth; the files become the bootstrap import and the export artifact.
--
-- Two tables, not ten. A cube, a manifest and a pack have nothing in common
-- structurally, and giving each its own table means every new kind of
-- configuration file is a migration. `config_files` is deliberately generic so
-- that adding `backend/projects/newco/cubes/orders.yml` tomorrow is an import,
-- not a schema change.

-- ----------------------------------------------------------------------
-- The catalog
-- ----------------------------------------------------------------------
--
-- Three representations of one file, because no two of them are the same thing:
--
--   raw_content  the exact bytes, comments and all. What the exporter writes back
--                out and what a diff view shows. JSONB cannot reproduce a file.
--   parsed       the runtime representation. The importer pays the YAML parse
--                once so that four workers rebuilding a manifest do not each pay
--                it again, and so this is queryable.
--   checksum     SHA-256 of the raw bytes. Makes the import idempotent and makes
--                "the database is behind the files" a detectable state rather
--                than a suspicion.

CREATE TABLE IF NOT EXISTS vanna_app.config_files (
    id              bigserial   PRIMARY KEY,

    -- Who owns this file. `global` is platform content (the baseline, the packs,
    -- the domain definitions); `tenant` and `project` are one workspace's own.
    -- Without the distinction, every global pack would be duplicated per tenant.
    scope           text        NOT NULL
                    CHECK (scope IN ('global', 'tenant', 'project')),

    -- Empty strings rather than NULL, and that is not cosmetic: in PostgreSQL a
    -- UNIQUE constraint treats every NULL as distinct, so a nullable tenant_id
    -- would let the same global file be inserted any number of times and the
    -- constraint below would not object once.
    tenant_id       text        NOT NULL DEFAULT '',
    project         text        NOT NULL DEFAULT '',

    -- Relative to `backend/`, forward slashes on every platform:
    -- 'projects/chinook/cubes/sales.yml'. The path is the identity -- it is what
    -- the importer matches on and what the runtime asks for.
    relative_path   text        NOT NULL,

    -- Classified from the path by the importer: domain, instruction_baseline,
    -- instruction_pack, project_config, cube, model, relationships, manifest,
    -- knowledge_rule, eval_dataset, other. Not a CHECK constraint: a new kind
    -- must not need a migration, and an unrecognised path lands in `other` with
    -- its content intact rather than being rejected.
    kind            text        NOT NULL DEFAULT 'other',
    extension       text        NOT NULL DEFAULT '',

    checksum        text        NOT NULL,
    raw_content     text        NOT NULL,

    -- NULL when the file could not be parsed -- a `.md` knowledge rule, or a YAML
    -- file with a syntax error. Stored anyway, and reported by the importer:
    -- a file silently dropped is a configuration difference nobody finds.
    parsed          jsonb,

    metadata        jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Bumped on every content change, and the join key for the history below.
    version         integer     NOT NULL DEFAULT 1,
    updated_by      text,

    imported_at     timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (scope, tenant_id, project, relative_path)
);

-- The lookup the runtime actually makes: "everything of kind X for workspace Y".
CREATE INDEX IF NOT EXISTS config_files_scope_idx
    ON vanna_app.config_files (scope, tenant_id, project, kind);

-- Ask questions of the content without unnesting it per query --
-- `parsed @> '{"name": "customers"}'` across every model in every project.
CREATE INDEX IF NOT EXISTS config_files_parsed_idx
    ON vanna_app.config_files USING gin (parsed);

-- ----------------------------------------------------------------------
-- The history
-- ----------------------------------------------------------------------
--
-- Once configuration is edited from a browser rather than through a pull request,
-- the questions a pull request used to answer have to be answered here: who
-- changed this model, when, what did it look like before, and can I put it back.
--
-- A full copy per version rather than a diff. Configuration files are kilobytes,
-- there are dozens of them, and a stored diff needs the whole chain replayed to
-- answer "what was running last Tuesday" -- which is the only question this table
-- exists to answer.

CREATE TABLE IF NOT EXISTS vanna_app.config_versions (
    id              bigserial   PRIMARY KEY,
    config_file_id  bigint      NOT NULL
                    REFERENCES vanna_app.config_files(id) ON DELETE CASCADE,
    version         integer     NOT NULL,

    checksum        text        NOT NULL,
    raw_content     text        NOT NULL,
    parsed          jsonb,

    -- How it arrived: 'import' from a file, 'api' from an administrator.
    source          text        NOT NULL DEFAULT 'import'
                    CHECK (source IN ('import', 'api')),
    created_by      text,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (config_file_id, version)
);

CREATE INDEX IF NOT EXISTS config_versions_file_idx
    ON vanna_app.config_versions (config_file_id, version DESC);
