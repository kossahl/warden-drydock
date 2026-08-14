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
docker run --rm --user 0 --entrypoint sh -v "${volume}:/target" -v "${source}:/source:ro" $image -c 'set -eu; install -m 0440 -o 0 -g 20000 /source /target/db_password'
Assert-NativeSuccess 'database secret initialization'
docker run --rm --user 0 --entrypoint stat -v "${volume}:/target:ro" $image -c '%a %u:%g' /target/db_password
Assert-NativeSuccess 'database secret permission verification'
