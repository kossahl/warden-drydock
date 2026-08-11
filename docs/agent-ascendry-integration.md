# Agent Ascendry integration

Warden Drydock opts into Agent Ascendry through `.agent-ascendry.yaml` and a
Codex `Stop` hook. The original G2 parity review established that shared event,
audit, proposal, and approval fixtures produced identical results and that all
existing agents remained byte-identical. The published v0.1.1 release has
passed the same wheel-only Drydock integration checks. The superseded generic
local capture and audit implementation was therefore removed. Drydock's
curator, skills, durable evolution memory, and evaluation datasets remain
local.

## Published artifact

Install the immutable [Agent Ascendry v0.1.1 release](https://github.com/kossahl/agent-ascendry/releases/tag/v0.1.1)
from its wheel, never from an Agent Ascendry source checkout:

```text
agent_ascendry-0.1.1-py3-none-any.whl
https://github.com/kossahl/agent-ascendry/releases/download/v0.1.1/agent_ascendry-0.1.1-py3-none-any.whl
SHA-256 f1aa85454a8cf115457c217511c354dfdd18dc0fb82b101266bba31c68103050
source commit c18f85654bee64e1683564710783e2f01f27e5a3 (annotated tag v0.1.1)
```

Download the release wheel, verify its digest, and only then install it:

```powershell
$url = "https://github.com/kossahl/agent-ascendry/releases/download/v0.1.1/agent_ascendry-0.1.1-py3-none-any.whl"
$expected = "f1aa85454a8cf115457c217511c354dfdd18dc0fb82b101266bba31c68103050"
$temporary = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ("agent-ascendry-" + [guid]::NewGuid()))
$wheel = Join-Path $temporary "agent_ascendry-0.1.1-py3-none-any.whl"
try {
    Invoke-WebRequest -Uri $url -OutFile $wheel
    $actual = (Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "Agent Ascendry wheel SHA-256 mismatch: $actual" }
    python -m pip install --no-deps $wheel
    agent-ascendry init . --platform codex
    agent-ascendry validate .
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force
}
```

`init` is the only supported bootstrap mechanism. It reuses
`.codex/agents/agent_curator.toml` because the configuration sets
`install_if_missing: false`; it does not create a curator or install the generic
curation skill. Repeating `init` is byte-idempotent.

The command writes runtime ownership and evidence under the ignored
`.agent-ascendry/` directory and adds `/.agent-ascendry/` to the repository's
local `.git/info/exclude`. It does not edit the tracked `.gitignore`. A fresh
clone must run `init` once after installing the pinned artifact so local
ownership is established.

## Capture and review flow

The installed fail-open wrapper invokes the package with the active Python
interpreter. If the package is missing or capture fails, Codex completion is not
blocked and the wrapper prints only a bounded diagnostic. Diagnose locally with
`agent-ascendry validate .`.

For an isolated smoke check, submit bounded metadata without transcript content:

```powershell
'{"session_id":"pilot","turn_id":"capture-1","hook_event_name":"Stop","agent":"agent_curator","skills":["improve-drydock-agents"],"outcome":"completed","test_status":"passed"}' |
  agent-ascendry capture .
agent-ascendry audit .
```

The review sequence is `capture -> audit -> propose -> approve -> apply`.
No proposal can be applied without an explicit approval bound to its exact
SHA-256 hash. Drydock's agent evaluation cases, rubrics, and durable evolution
evidence remain in this repository. Only the Drydock-specific controlled
experiment output under `.agent-experience/experiments/` remains. Agent
Ascendry is the sole capture and audit implementation.

## Verification

The wheel-backed integration test is opt-in so the normal Drydock test suite
does not download from the network. Point it at a local copy of the published
wheel; the test enforces the public digest before installation:

```powershell
$env:AGENT_ASCENDRY_WHEEL = (Resolve-Path path\to\agent_ascendry-0.1.1-py3-none-any.whl)
python -m unittest tests.test_agent_ascendry_integration
```

The test models a fresh clone containing the tracked Ascendry wrapper and exact
single Stop hook. It verifies the wheel digest and import location,
install/reinstall idempotence, curator reuse, byte preservation for pre-existing
agents, skills, hook registry, and wrapper, ignored local state, fail-open hook
capture, capture idempotence, privacy filtering, and audit output. Release
metadata is also checked against this document without accessing the network.
