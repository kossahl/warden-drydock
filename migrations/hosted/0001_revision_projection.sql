BEGIN;

CREATE TABLE hosted_publication_intent (
    intent_id text PRIMARY KEY,
    intent_token text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('creation', 'approval')),
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    parent_revision text,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    tree_digest char(64) NOT NULL,
    change_digest char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'finalized', 'quarantined')),
    UNIQUE (intent_token, intent_id)
);
CREATE INDEX hosted_publication_intent_token_idx ON hosted_publication_intent(intent_token);

CREATE TABLE hosted_campaign_head (
    campaign_id text PRIMARY KEY,
    revision_id text NOT NULL UNIQUE,
    ordinal integer NOT NULL CHECK (ordinal > 0)
);

CREATE TABLE hosted_projection_checkpoint (
    campaign_id text PRIMARY KEY,
    revision_id text NOT NULL,
    projection_version integer NOT NULL,
    record_count integer NOT NULL,
    projection_digest char(64) NOT NULL
);

CREATE TABLE hosted_projection_record (
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    record_id text NOT NULL,
    relative_path text NOT NULL,
    body_digest char(64) NOT NULL,
    PRIMARY KEY(campaign_id, record_id)
);

CREATE TABLE hosted_projection_shadow_record (LIKE hosted_projection_record INCLUDING ALL);

COMMIT;
