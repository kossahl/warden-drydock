CREATE TABLE hosted_provider_consent (
    consent_id text PRIMARY KEY,
    credential_revision_fingerprint char(64) NOT NULL,
    adapter_version text NOT NULL,
    endpoint_id text NOT NULL,
    region text NOT NULL,
    storage_mode text NOT NULL,
    retrieval_policy_version integer NOT NULL CHECK (retrieval_policy_version > 0),
    notice_digest char(64) NOT NULL,
    consented_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE UNIQUE INDEX hosted_provider_one_current_consent_idx
    ON hosted_provider_consent((true)) WHERE revoked_at IS NULL;

CREATE TABLE hosted_ai_generation (
    generation_id text PRIMARY KEY,
    campaign_id text NOT NULL,
    revision_id text NOT NULL,
    session_id text,
    action text NOT NULL CHECK (action IN ('ask','check','generate')),
    prompt text NOT NULL,
    request_digest char(64) NOT NULL,
    source_set_digest char(64) NOT NULL,
    source_envelope jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','complete','failed','cancelled')),
    terminal_draft text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hosted_ai_stream_event (
    generation_id text NOT NULL REFERENCES hosted_ai_generation(generation_id),
    sequence integer NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL CHECK (event_type IN ('start','delta','tool_request','tool_result','usage','completion','cancel','failure')),
    payload jsonb NOT NULL,
    PRIMARY KEY (generation_id, sequence)
);

CREATE TABLE hosted_live_session (
    session_id text PRIMARY KEY,
    campaign_id text NOT NULL,
    base_revision text NOT NULL,
    reported_head_revision text NOT NULL,
    workflow_version integer NOT NULL CHECK (workflow_version > 0),
    controller_epoch integer NOT NULL CHECK (controller_epoch > 0),
    controller_id text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('active','ended_review_pending','ended'))
);
CREATE UNIQUE INDEX hosted_live_one_active_campaign_idx
    ON hosted_live_session(campaign_id) WHERE mode = 'active';

CREATE TABLE hosted_live_capture (
    session_id text NOT NULL REFERENCES hosted_live_session(session_id),
    event_id text NOT NULL,
    device_id text NOT NULL,
    operation_id text NOT NULL,
    device_order integer NOT NULL CHECK (device_order > 0),
    capture_type text NOT NULL CHECK (capture_type IN ('confirmed_fact','unresolved_question')),
    payload_digest char(64) NOT NULL,
    content text NOT NULL,
    PRIMARY KEY (session_id, event_id),
    UNIQUE (session_id, device_id, operation_id)
);

CREATE TABLE hosted_live_receipt (
    session_id text NOT NULL REFERENCES hosted_live_session(session_id),
    device_id text NOT NULL,
    operation_id text NOT NULL,
    payload_digest char(64) NOT NULL,
    PRIMARY KEY (session_id, device_id, operation_id)
);
