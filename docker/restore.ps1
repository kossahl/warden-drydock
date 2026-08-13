param(
    [Parameter(Mandatory=$true)][string]$Backup,
    [Parameter(Mandatory=$true)][string]$RestoreProject
)
$ErrorActionPreference = 'Stop'
python -c "from warden_drydock.hosted.operations.runtime_guard import check_host_runtime; check_host_runtime()"
python -c "import pathlib; from warden_drydock.hosted.operations.recovery import verify_manifest; verify_manifest(pathlib.Path(r'$Backup'))"
if ($RestoreProject -notmatch '^[a-z0-9][a-z0-9_-]+$') { throw 'Unsafe restore project name' }
docker compose --project-name $RestoreProject up -d db
docker compose --project-name $RestoreProject cp (Join-Path $Backup 'postgres.dump') db:/tmp/postgres.dump
docker compose --project-name $RestoreProject exec -T db pg_restore -U drydock -d drydock --clean --if-exists --exit-on-error --single-transaction /tmp/postgres.dump
docker compose --project-name $RestoreProject up -d app
docker compose --project-name $RestoreProject cp (Join-Path $Backup 'snapshots.tar') app:/tmp/snapshots.tar
docker compose --project-name $RestoreProject exec -T app python -c "import pathlib,tarfile; from warden_drydock.hosted.operations.recovery import safe_members; t=tarfile.open('/tmp/snapshots.tar'); t.extractall('/var/lib/drydock',members=safe_members(t.getmembers()),filter='data')"
docker compose --project-name $RestoreProject exec -T app python -m warden_drydock.hosted.operations.health --ready
Write-Output "Restore verified in fresh project '$RestoreProject'. Original project volumes were not modified or removed. Provider secrets must be reconfigured."
