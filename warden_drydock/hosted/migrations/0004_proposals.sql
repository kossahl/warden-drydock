CREATE TABLE hosted_proposal_version (
 proposal_id text NOT NULL CHECK (length(proposal_id) BETWEEN 3 AND 80 AND proposal_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
 version integer NOT NULL CHECK (version > 0),
 campaign_id text NOT NULL CHECK (length(campaign_id) BETWEEN 3 AND 80 AND campaign_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
 base_revision text NOT NULL CHECK (length(base_revision) BETWEEN 3 AND 80 AND base_revision ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
 diff_digest char(64) NOT NULL CHECK (diff_digest ~ '^[a-f0-9]{64}$'),
 payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
 changes jsonb NOT NULL,
 status text NOT NULL CHECK (status IN ('draft','approving','rejected','approved','conflict','published','quarantined')),
 publication_intent_token text CHECK (publication_intent_token IS NULL OR (length(publication_intent_token) BETWEEN 3 AND 80 AND publication_intent_token ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$')),
 published_revision_id text CHECK (published_revision_id IS NULL OR (length(published_revision_id) BETWEEN 3 AND 80 AND published_revision_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$')),
 result_digest char(64) CHECK (result_digest IS NULL OR result_digest ~ '^[a-f0-9]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (proposal_id, version)
);
CREATE TABLE hosted_proposal_audit (
 audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 proposal_id text NOT NULL, version integer NOT NULL,
 status text NOT NULL CHECK (status IN ('draft','approving','rejected','approved','conflict','published','quarantined')),
 diff_digest char(64) NOT NULL CHECK (diff_digest ~ '^[a-f0-9]{64}$'),
 payload_digest char(64) NOT NULL CHECK (payload_digest ~ '^[a-f0-9]{64}$'),
 event text NOT NULL,
 publication_intent_token text CHECK (publication_intent_token IS NULL OR (length(publication_intent_token) BETWEEN 3 AND 80 AND publication_intent_token ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$')),
 published_revision_id text CHECK (published_revision_id IS NULL OR (length(published_revision_id) BETWEEN 3 AND 80 AND published_revision_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$')),
 result_digest char(64) CHECK (result_digest IS NULL OR result_digest ~ '^[a-f0-9]{64}$'),
 occurred_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY (proposal_id, version) REFERENCES hosted_proposal_version(proposal_id, version)
);
