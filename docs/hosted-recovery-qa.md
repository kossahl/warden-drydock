# Hosted recovery QA

This runbook records the security and recovery evidence required by Issue #43.
Use synthetic campaign data. Record the tested commit and command results, but
never copy campaign text, prompts, credentials, or provider output into the
repository or a public issue.

## Acceptance evidence

| Requirement | Evidence |
| --- | --- |
| Unsafe paths, symlinks, prompt injection, authority widening, stale revisions, duplicates, crashes, and restarts fail safely. | `tests/hosted/engine/test_engine.py`, `tests/hosted/revisions/test_revisions.py`, `tests/hosted/http/test_live_http.py`, and `tests/hosted/ai/test_ai_live.py`. The synthetic prompt-injection case checks that the request contains no mutation tools and that an attempted tool request fails with empty content and no session mutation. It does not measure model obedience or test the authority of a completed generation. |
| PostgreSQL operations serialize safely and roll back without partial durable effects. | `PostgresLiveSessionIntegrationTests` in `tests/hosted/http/test_postgres_http.py` races takeover against capture and injects a receipt-write failure, then checks retry, final rows, receipts, workflow authority, proposals, and immutable snapshots. Existing tests cover concurrent session start and exact capture replay. `PostgresProposalIntegrationTests` covers competing approval and transactional rollback. |
| Only loopback app exposure exists; credentials stay out of snapshots, browser state, and logs. | `test_two_services_and_only_app_publishes_loopback`, `test_secrets_are_not_environment_values`, `test_provider_credential_stays_out_of_image_and_browser_sources`, `test_http_access_log_never_emits_request_content`, and `test_recovery_scripts_fail_closed` in `tests/hosted/operations/test_operations.py`; sanitized provider failures are covered in `tests/hosted/ai/test_ai_live.py`. |
| Fresh-volume restore verifies integrity, heads, reconciliation, and projection rebuild while preserving rollback volumes. | Run the restore drill below using `docker/backup.ps1` and `docker/restore.ps1`. Unit checks for manifests, archives, and fail-closed script ordering are in `tests/hosted/operations/test_operations.py`. |
| Unsynchronized browser data is disclosed and never represented as backed up. | `web/tests/browser/live-cockpit.spec.ts` and `web/tests/unit/captureStore.test.ts` distinguish device-saved data from server acknowledgement. `docker/backup.ps1` requires `-AcknowledgeUnsynchronizedBrowserData`; see [hosted operations](hosted-operations.md#backup). |
| CLI and generated standalone behavior stay compatible. | The path-free engine and generated-script parity cases in `tests/hosted/engine/test_engine.py`. |
| Independent review has no actionable findings. | Record the independent review outcome and any fix/re-review commits with the PR. |

## Run the checks

Run the canonical repository gate from the focused branch:

```bash
./scripts/review-check.sh
```

It runs the Python, frontend, browser, live PostgreSQL, clean-onboarding, and
whitespace checks in isolated environments. Do not treat skipped opt-in
PostgreSQL tests as evidence. The canonical gate supplies a live test database
for them.

For a fresh-volume restore drill, use a dedicated Compose project containing
only synthetic campaign data. Choose new project names for both the source and
restore projects; never use the normal `warden-drydock` project as the source.
Follow [the backup and restore procedure](hosted-operations.md#backup) and
[restore instructions](hosted-operations.md#restore-drill-and-rollback). Confirm
the restore command completes its integrity check, recovery reconciliation,
projection rebuild, and readiness check. Keep the original project and volumes
intact as the rollback target until the restored heads and projection digests
have been accepted.

Compare the restored database with a temporary database loaded from the
verified backup dump. This checks against the exact backup snapshot, rather
than mutable source data that may have changed after backup. Run from the
repository root. First provision and start the dedicated source project with
its own database secret and at least one synthetic campaign. Keep host ports
`18081` and `18080` free for the source and restore apps. The commands set the
project names used for the isolated drill:

```powershell
$source_project = 'drydock-recovery-qa-source'
$restore_project = 'drydock-recovery-qa-restore'
$backup_directory = Join-Path $env:TEMP ("drydock-recovery-qa-" + [guid]::NewGuid().ToString('N'))
$env:COMPOSE_PROJECT_NAME = $source_project
$env:DRYDOCK_PORT = '18081'
$env:DRYDOCK_ALLOWED_HOSTS = 'localhost:18081,127.0.0.1:18081'
$env:DRYDOCK_SEED_FIXTURES = '0'
docker compose --project-name $source_project build app
if ($LASTEXITCODE -ne 0) { throw 'Source application image build failed' }
./docker/initialize-secrets.ps1 -ProjectName $source_project
docker compose --project-name $source_project up -d --wait
if ($LASTEXITCODE -ne 0) { throw 'Source project startup failed' }
# Open http://127.0.0.1:18081 and create one campaign with one synthetic record.
./docker/backup.ps1 -Destination $backup_directory -AcknowledgeUnsynchronizedBrowserData
./docker/restore.ps1 -Backup $backup_directory -RestoreProject $restore_project
$comparison_database = 'drydock_backup_comparison'
$comparison_dump = '/var/lib/postgresql/data/.drydock-backup-comparison.dump'
$comparison_database_url = "postgresql://drydock@db:5432/$comparison_database"
$database_query = 'SELECT h.campaign_id,h.revision_id,p.record_count,p.projection_digest FROM hosted_campaign_head h LEFT JOIN hosted_projection_checkpoint p ON p.campaign_id=h.campaign_id AND p.revision_id=h.revision_id ORDER BY h.campaign_id'
$missing_checkpoint_query = 'SELECT count(*) FROM hosted_campaign_head h LEFT JOIN hosted_projection_checkpoint p ON p.campaign_id=h.campaign_id AND p.revision_id=h.revision_id WHERE p.campaign_id IS NULL'
$comparison_created = $false
try {
    # Mark the path before cp so finally removes even a partial copy.
    docker compose --project-name $restore_project exec -T db rm -f $comparison_dump
    if ($LASTEXITCODE -ne 0) { throw 'Stale backup comparison dump cleanup failed' }
    docker compose --project-name $restore_project cp (Join-Path $backup_directory 'postgres.dump') "db:$comparison_dump"
    if ($LASTEXITCODE -ne 0) { throw 'Backup dump staging failed' }
    docker compose --project-name $restore_project exec -T db createdb -U drydock $comparison_database
    if ($LASTEXITCODE -ne 0) { throw 'Backup comparison database creation failed' }
    $comparison_created = $true
    docker compose --project-name $restore_project exec -T db pg_restore -U drydock -d $comparison_database --exit-on-error --single-transaction $comparison_dump
    if ($LASTEXITCODE -ne 0) { throw 'Backup comparison database restore failed' }
    # The normal app entrypoint migrates and recovers this copy from the restored snapshot volume.
    docker compose --project-name $restore_project run --rm --no-deps -e "DATABASE_URL=$comparison_database_url" app python -c "pass"
    if ($LASTEXITCODE -ne 0) { throw 'Backup comparison recovery failed' }
    $restored_missing = docker compose --project-name $restore_project exec -T db psql -U drydock -d drydock -X -At -v ON_ERROR_STOP=1 -c $missing_checkpoint_query
    if ($LASTEXITCODE -ne 0 -or [int]($restored_missing.Trim()) -ne 0) { throw 'Restored database has a head without a projection checkpoint' }
    $backup_missing = docker compose --project-name $restore_project exec -T db psql -U drydock -d $comparison_database -X -At -v ON_ERROR_STOP=1 -c $missing_checkpoint_query
    if ($LASTEXITCODE -ne 0 -or [int]($backup_missing.Trim()) -ne 0) { throw 'Backup database has a head without a projection checkpoint' }
    $backup_rows = docker compose --project-name $restore_project exec -T db psql -U drydock -d $comparison_database -X -At -F "`t" -v ON_ERROR_STOP=1 -c $database_query
    if ($LASTEXITCODE -ne 0 -or $backup_rows.Count -eq 0) { throw 'Backup head and digest query failed or returned no campaigns' }
    $restored_rows = docker compose --project-name $restore_project exec -T db psql -U drydock -d drydock -X -At -F "`t" -v ON_ERROR_STOP=1 -c $database_query
    if ($LASTEXITCODE -ne 0) { throw 'Restored head and digest query failed' }
    if (($backup_rows -join "`n") -cne ($restored_rows -join "`n")) { throw 'Restored heads or projection digests differ from the backup' }
} finally {
    $cleanup_errors = @()
    docker compose --project-name $restore_project exec -T db rm -f $comparison_dump | Out-Null
    if ($LASTEXITCODE -ne 0) { $cleanup_errors += 'comparison dump removal failed' }
    if ($comparison_created) {
        docker compose --project-name $restore_project exec -T db dropdb -U drydock --if-exists $comparison_database
        if ($LASTEXITCODE -ne 0) { $cleanup_errors += 'comparison database removal failed' }
    }
    if ($cleanup_errors.Count -gt 0) { throw "QA cleanup failures: $($cleanup_errors -join '; ')" }
}
docker volume inspect "${source_project}_postgres_data" "${source_project}_snapshots" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Original rollback volumes are unavailable' }
```

Replace project names and the backup directory with those used for the
isolated drill. The restore scripts check manifest and archive hashes before
loading data. Recovery verifies snapshot lineage and rebuilds projections from
immutable snapshots. Record only sanitized hashes, counts, readiness, exit
status, and whether the original volumes remain available.

## Previous isolated restore result (2026-09-30)

**Not passed with the stock restore steps.** The Linux Docker drill restored
the database and rebuilt projections, but app startup failed during readiness:
snapshot files copied into the fresh volume were owned by UID/GID `1000:1000`,
while the app runs as `10001:10001`. Startup raised `PermissionError` while
removing a runtime workspace staging directory. The host had no PowerShell,
so the corresponding Compose operations from `docker/restore.ps1` were run
manually. A test-only ownership correction in the synthetic restore volume
allowed readiness and the comparison against a second recovery from the
verified backup to pass; this does not count as a successful stock restore.

The restore ownership defect was tracked in the [P4-COMPOSE follow-up (#308)](https://github.com/kossahl/warden-drydock/issues/308).
That failure is historical; the next result records the rerun after the fix
merged.

## Latest isolated restore result (2026-10-02)

**Passed with the unmodified PowerShell backup and restore scripts** from
`master` at `01bee88e2d025a56c37579f0fdb39857542cb171` (after #310 merged).
PowerShell 7.6.6 ran `docker/backup.ps1` and `docker/restore.ps1` against fresh,
dedicated projects and one synthetic campaign with one record. The restore
completed database restore, snapshot archive validation and extraction,
recovery reconciliation, projection rebuild, and application readiness.
Both PowerShell scripts and the final readiness check exited with status 0.

Verified backup artifact SHA-256 values: `postgres.dump`
`cbe47597a5ce0eec4863a9f03a5e0530caf791fb8ac163c6309872d42277c2bf`,
`snapshots.tar`
`c310ee3af7068e5c6c3c0f7985f1ea5606e5310684ebc8b5570214810d20d935`, and
`manifest.json`
`1c020834f6c5e14076ed0da58525fba14255bcc69fac5485627d3cd5d3cd1b5a`.

The restored database was compared with a temporary database loaded from the
verified backup: one campaign head and one record, no head missing a projection
checkpoint, and identical campaign head, revision, record count, and projection
digest rows. The sanitized comparison row SHA-256 was
`debb6ffe08b825962ed58ef9d38a1df7b04da497361031f97e7da7349658c45f`.
Restored snapshot files were owned by UID/GID `10001:10001`. All source and
restore database, snapshot, and secret volumes remained available.

The host's loopback port `18080` was already occupied, so a temporary external
Compose override published the restored app on `127.0.0.1:18082` and matched
its allowed-host setting. The repository files and restore script were not
changed for this accommodation. The restore acceptance criterion now passes.
