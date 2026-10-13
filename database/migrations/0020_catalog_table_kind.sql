-- Views are recorded as views.
--
-- The scanner has always listed views alongside base tables, and stored both
-- the same way, so a view reached the prompt indistinguishable from a table.
-- That hid exactly the facts a view's name can misstate: the employees sample's
-- `current_dept_emp` returns every employee's *latest* department, former staff
-- included, and the agent took its name at its word and counted 300,024
-- "current" employees instead of 240,124.
--
-- `view_definition` is what the scan read from information_schema.views (or
-- sqlite_master), whitespace-collapsed and capped. It is structure, rewritten by
-- every scan like the rest of this table; a human-written description in
-- table_annotations still wins in the prompt.

ALTER TABLE vanna_app.catalog_tables
    ADD COLUMN table_kind      text NOT NULL DEFAULT 'table',
    ADD COLUMN view_definition text,
    ADD CONSTRAINT catalog_tables_kind_check CHECK (table_kind IN ('table', 'view'));
