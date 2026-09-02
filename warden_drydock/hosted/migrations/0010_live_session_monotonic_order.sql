-- P6-LIVE-SYNC-BACKEND v2 (P1, Option C): persist a monotonic creation marker
-- (session_seq) on hosted_live_session so readback selection across multiple ended
-- sessions is deterministic and identical across the InMemory and PostgreSQL
-- repositories.
--
-- PRE-RELEASE RULE: The live-session backend has not been deployed and has no
-- user-owned data. Pre-release development databases are disposable and must be
-- reset before migration 0010 if live-session rows exist. Compatibility for
-- pre-0010 live-session rows is not supported.
-- (After first real deployment this exception does not apply.)
--
-- The migration therefore FAILS CLOSED (pre_release_live_session_reset_required)
-- if any hosted_live_session row exists when it runs. Because the table must be
-- empty, session_seq is assigned at INSERT time for every session from the first
-- supported deployment; with the empty-table guarantee the ordering is always
-- unambiguous and no legacy-fallback exists.
--
-- Non-destructive, additive-only. No BEGIN/COMMIT here: the migration runner owns
-- the transaction and advisory lock (hosted_schema_migration wrapper).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM hosted_live_session) THEN
        RAISE EXCEPTION 'pre_release_live_session_reset_required';
    END IF;
END$$;

ALTER TABLE hosted_live_session
    ADD COLUMN IF NOT EXISTS session_seq bigint;

CREATE SEQUENCE IF NOT EXISTS hosted_live_session_seq_seq;

ALTER TABLE hosted_live_session
    ALTER COLUMN session_seq SET DEFAULT nextval('hosted_live_session_seq_seq');

ALTER TABLE hosted_live_session
    ALTER COLUMN session_seq SET NOT NULL;

CREATE INDEX IF NOT EXISTS hosted_live_session_campaign_seq_idx
    ON hosted_live_session(campaign_id, session_seq DESC);