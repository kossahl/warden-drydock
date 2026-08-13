param(
    [Parameter(Mandatory=$true)][string]$Backup,
    [Parameter(Mandatory=$true)][string]$RestoreProject
)
$ErrorActionPreference = 'Stop'
function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}
python -c "from warden_drydock.hosted.operations.runtime_guard import check_host_runtime; check_host_runtime()"
Assert-NativeSuccess 'runtime version check'
python -c "import pathlib; from warden_drydock.hosted.operations.recovery import verify_manifest; verify_manifest(pathlib.Path(r'$Backup'))"
Assert-NativeSuccess 'backup manifest verification'
if ($RestoreProject -notmatch '^[a-z0-9][a-z0-9_-]+$') { throw 'Unsafe restore project name' }
$env:DRYDOCK_PORT = '0'
$existing = docker compose --project-name $RestoreProject ps --all --quiet
Assert-NativeSuccess 'restore project inspection'
if ($existing) { throw 'Restore project already has containers; choose a fresh project name' }
foreach ($suffix in @('postgres_data','snapshots','provider_secrets')) {
    docker volume inspect "${RestoreProject}_${suffix}" *> $null
    if ($LASTEXITCODE -eq 0) { throw "Restore volume already exists: ${RestoreProject}_${suffix}" }
}
$stagingRoot = Join-Path $Backup 'snapshot-restore-staging'
if (Test-Path -LiteralPath $stagingRoot) { throw 'Restore staging path already exists' }
python -c "import pathlib; from warden_drydock.hosted.operations.recovery import extract_snapshot_archive; print(extract_snapshot_archive(pathlib.Path(r'$Backup')/'snapshots.tar',pathlib.Path(r'$Backup')))"
Assert-NativeSuccess 'snapshot archive validation'
docker compose --project-name $RestoreProject up -d db
Assert-NativeSuccess 'fresh database startup'
docker compose --project-name $RestoreProject cp (Join-Path $Backup 'postgres.dump') db:/tmp/postgres.dump
Assert-NativeSuccess 'PostgreSQL dump copy'
docker compose --project-name $RestoreProject exec -T db pg_restore -U drydock -d drydock --clean --if-exists --exit-on-error --single-transaction /tmp/postgres.dump
Assert-NativeSuccess 'PostgreSQL restore'
docker compose --project-name $RestoreProject create app
Assert-NativeSuccess 'application recovery container creation'
docker compose --project-name $RestoreProject cp (Join-Path $stagingRoot 'snapshots\.') app:/var/lib/drydock/snapshots
Assert-NativeSuccess 'snapshot restore copy'
docker compose --project-name $RestoreProject run --rm --no-deps app python -m warden_drydock.hosted.operations.recover
Assert-NativeSuccess 'intent reconciliation and projection rebuild'
docker compose --project-name $RestoreProject up -d app
Assert-NativeSuccess 'application startup'
docker compose --project-name $RestoreProject exec -T app python -m warden_drydock.hosted.operations.health --ready
Assert-NativeSuccess 'restored readiness check'
Write-Output "Restore verified in fresh project '$RestoreProject'. Original project volumes were not modified or removed. Provider secrets must be reconfigured."
