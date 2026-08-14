param(
    [string]$ProjectName = 'warden-drydock'
)
$ErrorActionPreference = 'Stop'
function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}
if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]+$') { throw 'Unsafe project name' }
$source = (Resolve-Path (Join-Path $PSScriptRoot 'secrets\db_password.txt')).Path
$volume = "${ProjectName}_database_secrets"
$image = "${ProjectName}-app"
docker image inspect $image *> $null
Assert-NativeSuccess 'application image inspection'
docker volume create $volume | Out-Null
Assert-NativeSuccess 'database secret volume creation'
docker run --rm --user 0 --entrypoint sh -v "${volume}:/target" -v "${source}:/source:ro" $image -c 'set -eu; if [ -e /target/db_password ]; then cmp -s /source /target/db_password || exit 42; else install -m 0440 -o 0 -g 20000 /source /target/db_password; fi'
Assert-NativeSuccess 'database secret initialization'
$permission = docker run --rm --user 0 --entrypoint stat -v "${volume}:/target:ro" $image -c '%a %u:%g' /target/db_password
Assert-NativeSuccess 'database secret permission verification'
if (($permission | Out-String).Trim() -ne '440 0:20000') { throw "Unexpected database secret permission: $permission" }
