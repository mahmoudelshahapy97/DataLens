-- Inferred relationships: joins the scanner proposes for databases that declare
-- no foreign keys, and the admin review that confirms or rejects them.
--
-- Stored on catalog_relationships itself rather than in a side table, because
-- an inferred join is the same kind of fact as a declared one -- two columns
-- that join -- differing only in how sure anybody is. Keeping them in one
-- table means every reader already sees both, and filters on review_status
-- decide which are served.
--
-- The review survives a rescan: upsert_relationships keeps review_status once
-- reviewed_by is set, so rejecting a bad guess is a decision made once, not
-- after every scan. A foreign key later declared for the same columns wins
-- outright (origin becomes 'declared', review_status 'accepted').

ALTER TABLE vanna_app.catalog_relationships
    ADD COLUMN origin        text        NOT NULL DEFAULT 'declared',
    ADD COLUMN confidence    real,
    ADD COLUMN review_status text        NOT NULL DEFAULT 'accepted',
    ADD COLUMN reviewed_by   text,
    ADD COLUMN reviewed_at   timestamptz,
    ADD CONSTRAINT catalog_relationships_origin_check
        CHECK (origin IN ('declared', 'inferred')),
    ADD CONSTRAINT catalog_relationships_review_check
        CHECK (review_status IN ('accepted', 'proposed', 'rejected')),
    ADD CONSTRAINT catalog_relationships_confidence_check
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));

-- The review screen lists one data source's inferred joins.
CREATE INDEX IF NOT EXISTS catalog_relationships_inferred_idx
    ON vanna_app.catalog_relationships (tenant_id, data_source_id)
    WHERE origin = 'inferred';
