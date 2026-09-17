-- Core columns: which columns of a table an admin has curated as the ones
-- that matter most.
--
-- Same shape and reasoning as table_annotations / column_annotations in
-- 0010_schema_catalog.sql: this is meaning, not structure, so a re-scan must
-- never touch it, and it is not foreign-keyed to catalog_columns so a
-- selection survives structural churn and re-applies if a dropped column
-- comes back.

CREATE TABLE IF NOT EXISTS vanna_app.core_columns (
    tenant_id       text        NOT NULL REFERENCES vanna_app.tenants(id) ON DELETE CASCADE,
    data_source_id  text        NOT NULL,
    table_key       text        NOT NULL,
    column_key      text        NOT NULL,

    marked_by       text,
    marked_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, data_source_id, table_key, column_key)
);

CREATE INDEX IF NOT EXISTS core_columns_by_table_idx
    ON vanna_app.core_columns (tenant_id, data_source_id, table_key);
