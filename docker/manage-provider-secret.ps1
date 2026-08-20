param(
    [ValidateSet('Set', 'Remove', 'Verify')]
    [string]$Action = 'Set',
    [string]$ProjectName = 'warden-drydock'
)
$ErrorActionPreference = 'Stop'
function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}
if ($ProjectName -notmatch '^[a-z0-9][a-z0-9_-]+$') { throw 'Unsafe project name' }

$image = "${ProjectName}-app"
$volume = "${ProjectName}_provider_secrets"

if ($Action -eq 'Verify') {
    docker compose -p $ProjectName exec -T app python -c 'from warden_drydock.hosted.ai.provider import OpenAIResponsesAdapter; raise SystemExit(0 if OpenAIResponsesAdapter().verify() else 1)'
    Assert-NativeSuccess 'provider credential presence verification'
    return
}

docker image inspect $image *> $null
Assert-NativeSuccess 'application image inspection'
docker volume create $volume | Out-Null
Assert-NativeSuccess 'provider secret volume creation'

if ($Action -eq 'Set') {
    $secret = Read-Host 'OpenAI API key' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ([string]::IsNullOrWhiteSpace($plain)) { throw 'Empty provider credential' }
        $plain | docker run --rm -i --user 10001:10001 --entrypoint python --mount "type=volume,source=${volume},target=/var/lib/drydock/secrets" $image -c 'import sys; from pathlib import Path; from warden_drydock.hosted.operations.secrets import SecretStore; SecretStore(Path("/var/lib/drydock/secrets")).replace("openai_api_key", sys.stdin.buffer.read())'
        Assert-NativeSuccess 'provider credential replacement'
    }
    finally {
        $plain = $null
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    return
}

docker run --rm --user 10001:10001 --entrypoint python --mount "type=volume,source=${volume},target=/var/lib/drydock/secrets" $image -c 'from pathlib import Path; from warden_drydock.hosted.operations.secrets import SecretStore; SecretStore(Path("/var/lib/drydock/secrets")).remove("openai_api_key")'
Assert-NativeSuccess 'provider credential removal'
