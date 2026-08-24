CREATE TABLE hosted_atlas_projection_checkpoint (
    campaign_id text NOT NULL CHECK (length(campaign_id) BETWEEN 3 AND 80 AND campaign_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
    revision_id text NOT NULL CHECK (length(revision_id) BETWEEN 3 AND 80 AND revision_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
    parent_revision_id text CHECK (parent_revision_id IS NULL OR (length(parent_revision_id) BETWEEN 3 AND 80 AND parent_revision_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$')),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    tree_digest char(64) NOT NULL CHECK (tree_digest ~ '^[a-f0-9]{64}$'),
    campaign_name text NOT NULL CHECK (length(campaign_name) BETWEEN 1 AND 200),
    adapter_id text NOT NULL CHECK (adapter_id = 'mothership'),
    projection_version integer NOT NULL CHECK (projection_version > 0),
    record_count integer NOT NULL CHECK (record_count >= 0),
    edge_count integer NOT NULL CHECK (edge_count >= 0),
    history_change_count integer NOT NULL CHECK (history_change_count >= 0),
    projection_digest char(64) NOT NULL CHECK (projection_digest ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY (campaign_id, revision_id),
    UNIQUE (campaign_id, ordinal)
);

CREATE TABLE hosted_atlas_record (
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    record_id text NOT NULL CHECK (length(record_id) BETWEEN 1 AND 80 AND record_id ~ '^[a-z0-9][a-z0-9-]*$'),
    record_type text NOT NULL CHECK (length(record_type) BETWEEN 1 AND 80 AND record_type ~ '^[a-z0-9][a-z0-9-]*$'),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    raw_status_kind text NOT NULL CHECK (raw_status_kind IN ('known','missing','unknown')),
    raw_status_value text CHECK (raw_status_value IS NULL OR length(raw_status_value) BETWEEN 1 AND 80),
    authority text NOT NULL CHECK (authority IN ('preparation','canon','revealed')),
    summary text NOT NULL,
    normalized_content text NOT NULL,
    content_digest char(64) NOT NULL CHECK (content_digest ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY (campaign_id, revision_id, record_id),
    FOREIGN KEY (campaign_id, revision_id)
        REFERENCES hosted_atlas_projection_checkpoint(campaign_id, revision_id)
        ON DELETE CASCADE,
    CHECK ((raw_status_kind='missing' AND raw_status_value IS NULL)
        OR (raw_status_kind<>'missing' AND raw_status_value IS NOT NULL)),
    CHECK ((raw_status_kind='known' AND raw_status_value='canon' AND authority='canon')
        OR (raw_status_kind='known' AND raw_status_value='revealed' AND authority='revealed')
        OR (authority='preparation'
            AND raw_status_value IS DISTINCT FROM 'canon'
            AND raw_status_value IS DISTINCT FROM 'revealed'))
);
CREATE INDEX hosted_atlas_record_facets_idx
    ON hosted_atlas_record(campaign_id, revision_id, record_type, authority, raw_status_kind, raw_status_value, record_id);

CREATE TABLE hosted_atlas_edge (
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    edge_id text NOT NULL CHECK (edge_id ~ '^edge_[a-f0-9]{64}$'),
    occurrence_order integer NOT NULL CHECK (occurrence_order > 0),
    source_record_id text NOT NULL,
    target_record_id text NOT NULL,
    relationship text NOT NULL CHECK (length(relationship) BETWEEN 1 AND 80 AND relationship ~ '^[a-z0-9][a-z0-9-]*$'),
    state text NOT NULL CHECK (length(state) BETWEEN 1 AND 80 AND state ~ '^[a-z0-9][a-z0-9-]*$'),
    context text NOT NULL CHECK (length(context) > 0),
    PRIMARY KEY (campaign_id, revision_id, edge_id),
    UNIQUE (campaign_id, revision_id, source_record_id, occurrence_order),
    FOREIGN KEY (campaign_id, revision_id)
        REFERENCES hosted_atlas_projection_checkpoint(campaign_id, revision_id)
        ON DELETE CASCADE,
    FOREIGN KEY (campaign_id, revision_id, source_record_id)
        REFERENCES hosted_atlas_record(campaign_id, revision_id, record_id),
    FOREIGN KEY (campaign_id, revision_id, target_record_id)
        REFERENCES hosted_atlas_record(campaign_id, revision_id, record_id)
);
CREATE INDEX hosted_atlas_edge_target_idx
    ON hosted_atlas_edge(campaign_id, revision_id, target_record_id, source_record_id, occurrence_order);

CREATE TABLE hosted_atlas_history_entry (
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    parent_revision_id text,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    tree_digest char(64) NOT NULL CHECK (tree_digest ~ '^[a-f0-9]{64}$'),
    change_digest char(64) NOT NULL CHECK (change_digest ~ '^[a-f0-9]{64}$'),
    proposal_id text CHECK (proposal_id IS NULL OR (length(proposal_id) BETWEEN 3 AND 80 AND proposal_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$')),
    proposal_version integer CHECK (proposal_version IS NULL OR proposal_version > 0),
    PRIMARY KEY (campaign_id, revision_id),
    UNIQUE (campaign_id, ordinal),
    FOREIGN KEY (campaign_id, revision_id)
        REFERENCES hosted_atlas_projection_checkpoint(campaign_id, revision_id)
        ON DELETE CASCADE,
    CHECK ((proposal_id IS NULL) = (proposal_version IS NULL))
);

CREATE TABLE hosted_atlas_history_change (
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    change_order integer NOT NULL CHECK (change_order > 0),
    record_id text NOT NULL CHECK (length(record_id) BETWEEN 1 AND 80 AND record_id ~ '^[a-z0-9][a-z0-9-]*$'),
    change_kind text NOT NULL CHECK (change_kind IN ('added','removed','content_changed','metadata_changed','authority_transition')),
    link_revision_id text NOT NULL CHECK (length(link_revision_id) BETWEEN 3 AND 80 AND link_revision_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
    before_content_digest char(64) CHECK (before_content_digest IS NULL OR before_content_digest ~ '^[a-f0-9]{64}$'),
    after_content_digest char(64) CHECK (after_content_digest IS NULL OR after_content_digest ~ '^[a-f0-9]{64}$'),
    before_status_kind text CHECK (before_status_kind IS NULL OR before_status_kind IN ('known','missing','unknown')),
    before_status_value text,
    after_status_kind text CHECK (after_status_kind IS NULL OR after_status_kind IN ('known','missing','unknown')),
    after_status_value text,
    from_authority text CHECK (from_authority IS NULL OR from_authority IN ('preparation','canon','revealed')),
    to_authority text CHECK (to_authority IS NULL OR to_authority IN ('preparation','canon','revealed')),
    PRIMARY KEY (campaign_id, revision_id, change_order),
    FOREIGN KEY (campaign_id, revision_id)
        REFERENCES hosted_atlas_history_entry(campaign_id, revision_id)
        ON DELETE CASCADE,
    CHECK ((before_status_kind IS NULL AND before_status_value IS NULL)
        OR (before_status_kind='missing' AND before_status_value IS NULL)
        OR (before_status_kind IN ('known','unknown') AND before_status_value IS NOT NULL)),
    CHECK ((after_status_kind IS NULL AND after_status_value IS NULL)
        OR (after_status_kind='missing' AND after_status_value IS NULL)
        OR (after_status_kind IN ('known','unknown') AND after_status_value IS NOT NULL))
);
CREATE INDEX hosted_atlas_history_subject_idx
    ON hosted_atlas_history_change(campaign_id, record_id, revision_id, change_order);

ALTER TABLE hosted_ai_generation
    ADD COLUMN focus_record_id text,
    ADD COLUMN focus_content_digest char(64),
    ADD CONSTRAINT hosted_ai_generation_focus_complete CHECK (
        (focus_record_id IS NULL AND focus_content_digest IS NULL)
        OR
        (focus_record_id IS NOT NULL
         AND focus_content_digest IS NOT NULL
         AND length(focus_record_id) BETWEEN 1 AND 80
         AND focus_record_id ~ '^[a-z0-9][a-z0-9-]*$'
         AND focus_content_digest ~ '^[a-f0-9]{64}$')
    );
