-- V1 HTTP responses cannot be replayed through the active V2 runtime.
-- These receipts are transport caches, not campaign or workflow authority.
DELETE FROM hosted_http_operation_receipt;
