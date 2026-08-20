CREATE TABLE hosted_http_operation_receipt (
 operation text NOT NULL CHECK (operation IN ('provider_consent','campaign_create','proposal_create','proposal_correct','proposal_reject','proposal_approve')),
 idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 3 AND 80 AND idempotency_key ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
 payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
 state text NOT NULL CHECK (state IN ('pending','completed')),
 http_status integer CHECK (http_status BETWEEN 200 AND 599),
 response_body jsonb,
 completed_at timestamptz,
 CHECK ((state='pending' AND http_status IS NULL AND response_body IS NULL AND completed_at IS NULL)
     OR (state='completed' AND http_status IS NOT NULL AND response_body IS NOT NULL AND completed_at IS NOT NULL)),
 PRIMARY KEY (operation, idempotency_key)
);

ALTER TABLE hosted_proposal_version
 ADD COLUMN generation_id text,
 ADD COLUMN source_revision text,
 ADD COLUMN source_set_digest char(64),
 ADD COLUMN terminal_draft_digest char(64),
 ADD CONSTRAINT hosted_proposal_provenance_complete CHECK (
   (generation_id IS NULL AND source_revision IS NULL AND source_set_digest IS NULL AND terminal_draft_digest IS NULL)
   OR
   (generation_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'
    AND source_revision = base_revision
    AND source_set_digest ~ '^[a-f0-9]{64}$'
    AND terminal_draft_digest ~ '^[a-f0-9]{64}$')
 );
