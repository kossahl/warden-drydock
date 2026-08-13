param(
    [Parameter(Mandatory=$true)][string]$Destination,
    [Parameter(Mandatory=$true)][switch]$AcknowledgeUnsynchronizedBrowserData
)
$ErrorActionPreference = 'Stop'
python -c "from warden_drydock.hosted.operations.runtime_guard import check_host_runtime; check_host_runtime()"
if (Test-Path -LiteralPath $Destination) { throw 'Backup destination already exists' }
New-Item -ItemType Directory -Path $Destination | Out-Null
try {
    docker compose exec -T db psql -U drydock -d drydock -v ON_ERROR_STOP=1 -c "UPDATE hosted_runtime_state SET maintenance_mode=true, updated_at=now() WHERE singleton"
    docker compose exec -T db pg_dump -U drydock -d drydock -Fc --file=/tmp/postgres.dump
    docker compose cp db:/tmp/postgres.dump (Join-Path $Destination 'postgres.dump')
    docker compose exec -T db pg_restore --list /tmp/postgres.dump | Out-Null
    docker compose exec -T app python -c "import hashlib,pathlib,tarfile; r=pathlib.Path('/var/lib/drydock/snapshots'); fs=sorted(p for p in r.rglob('*') if p.is_file()); print(hashlib.sha256(''.join(f'{p.relative_to(r).as_posix()}:{hashlib.sha256(p.read_bytes()).hexdigest()}\n' for p in fs).encode()).hexdigest()); tarfile.open('/tmp/snapshots.tar','w').add(r,arcname='snapshots')" | Set-Content -NoNewline (Join-Path $Destination 'inventory.txt')
    docker compose cp app:/tmp/snapshots.tar (Join-Path $Destination 'snapshots.tar')
    python -c "import json,pathlib; from warden_drydock.hosted.operations.recovery import build_manifest; r=pathlib.Path(r'$Destination'); m=build_manifest(r/'postgres.dump',r/'snapshots.tar',(r/'inventory.txt').read_text().strip()); (r/'manifest.json').write_text(json.dumps(m,sort_keys=True,indent=2)+'\n',encoding='utf-8')"
    python -c "import pathlib; from warden_drydock.hosted.operations.recovery import verify_manifest; verify_manifest(pathlib.Path(r'$Destination'))"
} finally {
    docker compose exec -T db psql -U drydock -d drydock -v ON_ERROR_STOP=1 -c "UPDATE hosted_runtime_state SET maintenance_mode=false, updated_at=now() WHERE singleton"
}
