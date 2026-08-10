# Agent Ascendry pilot integration

Warden Drydock opts into Agent Ascendry through `.agent-ascendry.yaml` and a
Codex `Stop` hook. The original G2 parity review established that shared event,
audit, proposal, and approval fixtures produced identical results and that all
existing agents remained byte-identical. The final local candidate below has
passed the same wheel-only Drydock integration checks; its final parity and
release reviews remain gated. The superseded generic local capture and audit
implementation was therefore removed. Drydock's curator, skills, durable
evolution memory, and evaluation datasets remain local.

## Candidate artifact

The accepted v0.1 candidate is installed from the wheel, never imported from an
Agent Ascendry source checkout:

```text
agent_ascendry-0.1.0-py3-none-any.whl
SHA-256 f7736c5a9767f12221a98d2e9342c7b99134f642442befefa0dc5dc45c3cb8bc
source commit 3df0f96f468105d18bdbb04bf8d5bceb009547d3
```

Verify the digest before installing the local release candidate:

```powershell
Get-FileHash -Algorithm SHA256 path\to\agent_ascendry-0.1.0-py3-none-any.whl
python -m pip install --no-deps path\to\agent_ascendry-0.1.0-py3-none-any.whl
agent-ascendry init . --platform codex
agent-ascendry validate .
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
does not depend on an unpublished local path:

```powershell
$env:AGENT_ASCENDRY_WHEEL = (Resolve-Path path\to\agent_ascendry-0.1.0-py3-none-any.whl)
python -m unittest tests.test_agent_ascendry_integration
```

The test models a fresh clone containing the tracked Ascendry wrapper and exact
single Stop hook. It verifies the wheel digest and import location,
install/reinstall idempotence, curator reuse, byte preservation for pre-existing
agents, skills, hook registry, and wrapper, ignored local state, fail-open hook
capture, capture idempotence, privacy filtering, and audit output. Release
pinning replaces the local wheel instruction only after the public-release
review passes.
