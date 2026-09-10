ALTER TABLE hosted_proposal_version
  ADD COLUMN editor_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE hosted_proposal_version
  ADD CONSTRAINT hosted_proposal_editor_metadata_object
  CHECK (jsonb_typeof(editor_metadata) = 'object');

CREATE TABLE hosted_editor_workflow (
  campaign_id text PRIMARY KEY,
  version integer NOT NULL CHECK (version >= 1)
);
