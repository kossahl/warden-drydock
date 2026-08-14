param(
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][switch]$AcknowledgeUnsynchronizedBrowserData
)
$ErrorActionPreference = 'Stop'
if (-not $AcknowledgeUnsynchronizedBrowserData.IsPresent) { throw 'Unsynchronized browser data acknowledgement is required' }
function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}
python -c "from warden_drydock.hosted.operations.runtime_guard import check_host_runtime; check_host_runtime()"
Assert-NativeSuccess 'runtime version check'
if (Test-Path -LiteralPath $Destination) { throw 'Backup destination already exists' }
New-Item -ItemType Directory -Path $Destination | Out-Null
$appWasStopped = $false
$dumpPath = '/var/lib/postgresql/data/.drydock-backup.dump'
$primaryError = $null
try {
    docker compose stop app
    Assert-NativeSuccess 'application maintenance stop'
    $appWasStopped = $true
    docker compose exec -T db psql -U drydock -d drydock -v ON_ERROR_STOP=1 -c "DO `$`$ BEGIN IF EXISTS (SELECT 1 FROM hosted_publication_intent WHERE status='pending') THEN RAISE EXCEPTION 'pending publication intents'; END IF; END `$`$; UPDATE hosted_runtime_state SET maintenance_mode=true, reconciliation_complete=true, updated_at=now() WHERE singleton"
    Assert-NativeSuccess 'maintenance barrier'
    docker compose exec -T db pg_dump -U drydock -d drydock -Fc --file=$dumpPath
    Assert-NativeSuccess 'PostgreSQL dump'
    docker compose exec -T db pg_restore --list $dumpPath | Out-Null
    Assert-NativeSuccess 'PostgreSQL archive verification'
    docker compose cp "db:${dumpPath}" (Join-Path $Destination 'postgres.dump')
    Assert-NativeSuccess 'PostgreSQL dump copy'
    docker compose cp app:/var/lib/drydock/snapshots (Join-Path $Destination 'snapshot-source')
    Assert-NativeSuccess 'snapshot volume copy'
    python -c "import pathlib; from warden_drydock.hosted.operations.recovery import create_snapshot_archive; r=pathlib.Path(r'$Destination'); print(create_snapshot_archive(r/'snapshot-source',r/'snapshots.tar'))" | Set-Content -NoNewline (Join-Path $Destination 'inventory.txt')
    Assert-NativeSuccess 'snapshot archive creation'
    python -c "import json,pathlib; from warden_drydock.hosted.operations.recovery import build_manifest; r=pathlib.Path(r'$Destination'); m=build_manifest(r/'postgres.dump',r/'snapshots.tar',(r/'inventory.txt').read_text().strip()); (r/'manifest.json').write_text(json.dumps(m,sort_keys=True,indent=2)+'\n',encoding='utf-8')"
    Assert-NativeSuccess 'backup manifest creation'
    python -c "import pathlib; from warden_drydock.hosted.operations.recovery import verify_manifest; verify_manifest(pathlib.Path(r'$Destination'))"
    Assert-NativeSuccess 'backup manifest verification'
} catch {
    $primaryError = $_
} finally {
    $cleanupErrors = @()
    try {
        docker compose exec -T db rm -f $dumpPath | Out-Null
        Assert-NativeSuccess 'database dump staging cleanup'
    } catch { $cleanupErrors += $_.Exception.Message }
    try {
        docker compose exec -T db psql -U drydock -d drydock -v ON_ERROR_STOP=1 -c "UPDATE hosted_runtime_state SET maintenance_mode=false, updated_at=now() WHERE singleton" | Out-Null
        Assert-NativeSuccess 'maintenance cleanup'
    } catch { $cleanupErrors += $_.Exception.Message }
    if ($appWasStopped) {
        try {
            docker compose start app | Out-Null
            Assert-NativeSuccess 'application restart'
        } catch { $cleanupErrors += $_.Exception.Message }
    }
    if ($primaryError) {
        if ($cleanupErrors.Count -gt 0) { throw "$($primaryError.Exception.Message) Cleanup failures: $($cleanupErrors -join '; ')" }
        throw $primaryError
    }
    if ($cleanupErrors.Count -gt 0) {
        throw "Backup cleanup failures: $($cleanupErrors -join '; ')"
    }
}
