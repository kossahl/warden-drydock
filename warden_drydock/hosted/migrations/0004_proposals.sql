CREATE TABLE hosted_proposal_version (
 proposal_id text NOT NULL, version integer NOT NULL, campaign_id text NOT NULL,
 base_revision text NOT NULL, diff_digest char(64) NOT NULL, payload_digest char(64) NOT NULL,
 changes jsonb NOT NULL,
 status text NOT NULL CHECK (status IN ('draft','approving','rejected','approved','conflict','published','quarantined')),
 publication_intent_token text,
 published_revision_id text,
 result_digest char(64),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (proposal_id, version)
);
CREATE TABLE hosted_proposal_audit (
 audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 proposal_id text NOT NULL, version integer NOT NULL, status text NOT NULL,
 diff_digest char(64) NOT NULL, payload_digest char(64) NOT NULL,
 event text NOT NULL,
 publication_intent_token text,
 published_revision_id text,
 result_digest char(64),
 occurred_at timestamptz NOT NULL DEFAULT now()
);
