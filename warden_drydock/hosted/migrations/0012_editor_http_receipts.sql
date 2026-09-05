ALTER TABLE hosted_http_operation_receipt
  DROP CONSTRAINT IF EXISTS hosted_http_operation_receipt_operation_check;

ALTER TABLE hosted_http_operation_receipt
  ADD CONSTRAINT hosted_http_operation_receipt_operation_check
  CHECK (operation IN (
    'provider_consent', 'campaign_create', 'proposal_create',
    'proposal_correct', 'proposal_reject', 'proposal_approve',
    'editor_record_create', 'editor_record_edit', 'editor_record_remove',
    'editor_proposal_correct', 'editor_proposal_reject',
    'editor_proposal_approve'
  ));
