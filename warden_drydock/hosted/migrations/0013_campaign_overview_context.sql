ALTER TABLE hosted_live_session
    ADD COLUMN IF NOT EXISTS ended_at timestamptz;

ALTER TABLE hosted_publication_intent
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE hosted_campaign_profile (
    campaign_id text PRIMARY KEY CHECK (length(campaign_id) BETWEEN 3 AND 80 AND campaign_id ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'),
    created_at timestamptz NOT NULL,
    display_timezone text NOT NULL DEFAULT 'UTC' CHECK (length(display_timezone) BETWEEN 1 AND 64),
    players jsonb NOT NULL DEFAULT '[]'::jsonb
);

INSERT INTO hosted_campaign_profile(campaign_id, created_at)
SELECT campaign_id, min(created_at)
FROM hosted_publication_intent
WHERE kind = 'creation'
GROUP BY campaign_id
ON CONFLICT (campaign_id) DO NOTHING;
