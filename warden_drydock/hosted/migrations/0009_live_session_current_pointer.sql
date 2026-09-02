-- P6-LIVE-SYNC-BACKEND v2: add a persisted, unambiguous creation marker to
-- hosted_live_session so readback selection across multiple ended sessions is
-- deterministic and identical across the InMemory and PostgreSQL repositories
-- (P1-B). Active sessions keep the one-per-campaign partial index; ended sessions
-- remain readable and are selected newest-by-created_at.
--
-- Non-destructive, additive-only. No BEGIN/COMMIT here: the migration runner owns
-- the transaction and advisory lock (hosted_schema_migration wrapper). Existing rows
-- get now() as their created_at via the column default, so old rows remain readable.
ALTER TABLE hosted_live_session
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS hosted_live_session_campaign_created_idx
    ON hosted_live_session(campaign_id, created_at DESC, session_id DESC);