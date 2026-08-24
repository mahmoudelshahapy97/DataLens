-- The schema catalog, made durable and queryable.
--
-- Until now the catalog lived in one JSON file on disk ($VANNA_DATA_DIR/catalog.json),
-- keyed in memory by (tenant, data source). That is fine for a laptop and wrong for a
-- deployment: there is no row-level tenant isolation, two replicas cannot both own the
-- file, and an administrator cannot query it. This migration gives the catalog the same
-- treatment every other piece of shared state already has.
--
-- The organising idea is a split this codebase already discovered once, in
-- capabilities/schema_catalog/dbt.py: "dbt owns meaning (descriptions), the scan owns
-- structure (real types, nullability, row counts, observed values). Taking whole records
-- from either side would throw away half the picture."
--
-- So structure and meaning live in different tables:
--
--   catalog_tables / catalog_columns / catalog_relationships
--       Machine-discovered. Every scan rewrites them.
--   table_annotations / column_annotations / business_domains / domain_tables
--       Human-authored. No scan ever touches them.
--
-- That is not tidiness, it is the fix for a real defect. `upsert_tables` replaces a
-- whole TableMetadata record, so with meaning stored alongside structure a routine
-- re-scan silently destroys every description somebody wrote. Separate tables make that
-- impossible rather than merely discouraged.
--
-- Everything is keyed on the normalized name -- `table_key`, `column_key`, produced by
-- normalize_table/normalize_identifier in core/grants/models.py -- and not on a
-- surrogate row id. That is deliberate, and is where this diverges from the reference
-- implementation, which needs version-stable resource ids because its grants carry a
-- foreign key to them. Ours do not: table_grants and column_grants are already keyed
-- (tenant_id, data_source_id, role, table_key[, column_key]). Keying the catalog the
-- same way means a grant, an annotation and a scanned column all join on the same
-- columns, and a grant survives a re-scan for free rather than by careful bookkeeping.

-- ----------------------------------------------------------------------
-- Structure: what the database actually contains
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.catalog_tables (
    tenant_id           text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id      text        NOT NULL,
    -- Normalized (casefolded, unquoted), schema-qualified where the engine has schemas.
    -- The same key the grant tables use.
    table_key           text        NOT NULL,

    -- As the database spells it, so a screen can show `Sales.Orders`.
    schema_name         text        NOT NULL DEFAULT '',
    table_name          text        NOT NULL,
    table_type          text        NOT NULL DEFAULT 'table',
    row_count_estimate  bigint,

    -- Freshness, and why a table has no columns.
    --
    -- Not decorative: the scanner records a table it failed to profile with
    -- status='failed', an error message and an empty column list. Flattening that to
    -- 'scanned' would present a successfully-scanned table that happens to have no
    -- columns -- and grant resolution drops zero-column tables, so the failure would
    -- turn into a silent disappearance instead of something a screen can report.
    status              text        NOT NULL DEFAULT 'scanned',
    error_message       text,

    -- A table that vanished from a scan becomes 'removed' rather than being deleted.
    -- Deleting it would take its annotations and its domain membership with it, and a
    -- table can disappear for reasons that are not permanent -- a failed deploy, a
    -- rename in progress, a scan that ran against a replica mid-migration. Recording
    -- the absence keeps the curation and lets a screen say "this is gone" honestly.
    lifecycle_status    text        NOT NULL DEFAULT 'present',

    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, table_key),

    CONSTRAINT catalog_tables_lifecycle_check
        CHECK (lifecycle_status IN ('present', 'removed')),
    CONSTRAINT catalog_tables_type_check
        CHECK (table_type IN ('table', 'view', 'materialized_view')),
    -- Mirrors CatalogStatus in capabilities/schema_catalog/models.py.
    CONSTRAINT catalog_tables_status_check
        CHECK (status IN ('not_scanned', 'scanning', 'scanned', 'stale', 'failed'))
);

CREATE TABLE IF NOT EXISTS vanna_app.catalog_columns (
    tenant_id           text        NOT NULL,
    data_source_id      text        NOT NULL,
    table_key           text        NOT NULL,
    column_key          text        NOT NULL,

    column_name         text        NOT NULL,
    ordinal             integer     NOT NULL DEFAULT 0,
    data_type           text        NOT NULL DEFAULT 'unknown',
    nullable            boolean     NOT NULL DEFAULT true,
    is_primary_key      boolean     NOT NULL DEFAULT false,
    -- Identity/computed. The database refuses an INSERT that names one, which is why
    -- grant resolution never marks such a column writable.
    is_generated        boolean     NOT NULL DEFAULT false,
    has_default         boolean     NOT NULL DEFAULT false,

    -- Profiling output. Worth persisting: re-deriving it costs a SELECT DISTINCT per
    -- column against the customer's warehouse.
    low_cardinality     boolean     NOT NULL DEFAULT false,
    categories          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    sample_values       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    foreign_key         jsonb,

    lifecycle_status    text        NOT NULL DEFAULT 'present',
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, table_key, column_key),

    -- Cascades from its table, which is safe because a table is only ever marked
    -- 'removed', never deleted, by a scan.
    FOREIGN KEY (tenant_id, data_source_id, table_key)
        REFERENCES vanna_app.catalog_tables (tenant_id, data_source_id, table_key)
        ON DELETE CASCADE,

    CONSTRAINT catalog_columns_lifecycle_check
        CHECK (lifecycle_status IN ('present', 'removed'))
);

-- Building the prompt reads every column of a data source at once.
CREATE INDEX IF NOT EXISTS catalog_columns_lookup_idx
    ON vanna_app.catalog_columns (tenant_id, data_source_id, table_key);

CREATE TABLE IF NOT EXISTS vanna_app.catalog_relationships (
    tenant_id           text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id      text        NOT NULL,

    from_table_key      text        NOT NULL,
    from_column_key     text        NOT NULL,
    to_table_key        text        NOT NULL,
    to_column_key       text        NOT NULL,

    -- Mirrors RelationshipMetadata. `name` and `description` are what reach the
    -- prompt; `join_type` is what stops the model fanning out a one-to-many join
    -- and double-counting a sum.
    name                text        NOT NULL DEFAULT '',
    join_type           text        NOT NULL DEFAULT 'many_to_one',
    description         text,

    lifecycle_status    text        NOT NULL DEFAULT 'present',
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, from_table_key, from_column_key,
                 to_table_key, to_column_key),

    CONSTRAINT catalog_relationships_join_type_check
        CHECK (join_type IN ('one_to_one', 'one_to_many', 'many_to_one', 'many_to_many')),
    CONSTRAINT catalog_relationships_lifecycle_check
        CHECK (lifecycle_status IN ('present', 'removed'))
);

-- ----------------------------------------------------------------------
-- Meaning: what it is for
-- ----------------------------------------------------------------------
--
-- Deliberately not foreign-keyed to catalog_tables. An annotation must outlive
-- structural churn -- that is the whole reason it is a separate table -- so it is tied
-- to the tenant and nothing else. An annotation for a table that no longer exists is
-- harmless, and is exactly what you want back if the table returns.

CREATE TABLE IF NOT EXISTS vanna_app.table_annotations (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,
    table_key       text        NOT NULL,

    display_name    text        NOT NULL DEFAULT '',
    -- Reaches the model verbatim. Worth more to generation accuracy than the type is.
    description     text        NOT NULL DEFAULT '',
    domain_id       uuid,

    updated_by      text,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, table_key)
);

CREATE TABLE IF NOT EXISTS vanna_app.column_annotations (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,
    table_key       text        NOT NULL,
    column_key      text        NOT NULL,

    display_name    text        NOT NULL DEFAULT '',
    description     text        NOT NULL DEFAULT '',

    -- {"A": "Active", "C": "Cancelled"} -- what a coded value means. The single
    -- highest-value piece of column metadata for text-to-SQL: without it the model
    -- guesses the literal, and a wrong literal returns zero rows rather than an error.
    value_labels    jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Curated counterpart to the scanner's SENSITIVE_COLUMN_PATTERNS regex. The regex
    -- decides what to redact from a prompt automatically; this records a human's
    -- judgement, which a screen can surface when somebody is about to grant read.
    sensitivity     text,

    updated_by      text,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, table_key, column_key),

    CONSTRAINT column_annotations_sensitivity_check
        CHECK (sensitivity IS NULL
               OR sensitivity IN ('public', 'internal', 'confidential', 'restricted'))
);

-- ----------------------------------------------------------------------
-- Business domains
-- ----------------------------------------------------------------------
--
-- A named slice of one database, with the vocabulary people use to talk about it.
--
-- Note what this is not: it is not an access control. Domain membership is intersected
-- with what the caller may already read, so a domain can only ever narrow the tables in
-- play, never add one. A grouping that could widen access would be a second, weaker
-- permission system wearing a friendly name.
--
-- Per (tenant, data source) rather than per tenant, because a domain names tables and
-- tables belong to one database.

CREATE TABLE IF NOT EXISTS vanna_app.business_domains (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,

    name            text        NOT NULL,
    description     text        NOT NULL DEFAULT '',

    -- {"churn": "a customer with no order in 90 days"} -- the terms of art that the
    -- schema alone cannot tell you.
    terminology     jsonb       NOT NULL DEFAULT '{}'::jsonb,

    is_enabled      boolean     NOT NULL DEFAULT true,

    created_by      text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Case-insensitive, because "Sales" and "sales" are the same domain to everyone except
-- a database.
CREATE UNIQUE INDEX IF NOT EXISTS business_domains_name_idx
    ON vanna_app.business_domains (tenant_id, data_source_id, lower(name));

CREATE TABLE IF NOT EXISTS vanna_app.domain_tables (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    domain_id       uuid        NOT NULL REFERENCES vanna_app.business_domains(id) ON DELETE CASCADE,
    -- Same key as everywhere else, so membership joins to grants and to the catalog
    -- without a lookup table.
    table_key       text        NOT NULL,

    added_by        text,
    added_at        timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, domain_id, table_key)
);

CREATE INDEX IF NOT EXISTS domain_tables_by_table_idx
    ON vanna_app.domain_tables (tenant_id, table_key);

-- Added after business_domains exists, so the reference is real rather than advisory.
-- ON DELETE SET NULL: deleting a domain must not delete the table's description.
DO $$ BEGIN
    ALTER TABLE vanna_app.table_annotations
        ADD CONSTRAINT table_annotations_domain_fk
        FOREIGN KEY (domain_id) REFERENCES vanna_app.business_domains(id)
        ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
