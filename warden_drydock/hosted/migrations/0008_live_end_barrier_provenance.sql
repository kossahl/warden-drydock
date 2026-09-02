-- P6-LIVE-SYNC-BACKEND v2: persist the end barrier (Decision A) and optional
-- affected-record provenance (Decision 2 / Issue #67) without disturbing existing
-- live rows. Old rows remain readable: end_barrier stays NULL and record_id stays
-- NULL for captures created before this migration.
--
-- Non-destructive, additive-only. No BEGIN/COMMIT here: the migration runner owns
-- the transaction and advisory lock (hosted_schema_migration wrapper).
ALTER TABLE hosted_live_session
    ADD COLUMN IF NOT EXISTS end_barrier jsonb;

ALTER TABLE hosted_live_capture
    ADD COLUMN IF NOT EXISTS record_id text;

-- Affected-record provenance is optional and validated to the campaign-record
-- identifier grammar (domain id) when present. NULL remains the default for
-- pre-migration rows and captures without a known affected record. Length and
-- grammar mirror the HTTP layer domain-id adoption rules.
ALTER TABLE hosted_live_capture
    ADD CONSTRAINT hosted_live_capture_record_id_domain_check
    CHECK (record_id IS NULL OR (char_length(record_id) BETWEEN 3 AND 200 AND record_id ~ '^[a-z0-9][a-z0-9-]*$'));