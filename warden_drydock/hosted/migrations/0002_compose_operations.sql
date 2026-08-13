CREATE TABLE hosted_runtime_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    maintenance_mode boolean NOT NULL DEFAULT false,
    reconciliation_complete boolean NOT NULL DEFAULT false,
    schema_compatibility integer NOT NULL DEFAULT 1 CHECK (schema_compatibility > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO hosted_runtime_state(singleton) VALUES (true)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE hosted_backup_record (
    backup_id text PRIMARY KEY,
    manifest_digest char(64) NOT NULL,
    schema_compatibility integer NOT NULL,
    snapshot_inventory_digest char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz
);
