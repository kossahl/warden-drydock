# Hosted contract package registries

This directory is the transport-neutral contract vocabulary for the localhost,
single-Warden pilot. `index-v1.json` is the preserved legacy registry.
`index-v2.json` is the current machine-readable registry. It keeps the legacy
families and adds explicitly versioned contracts, currently `canon_proposal`
v2. Each registry lists its bounded schema families, positive examples,
negative fixtures, and authority owners.

Validation requires both the Draft 2020-12 schema named by a family and every
normative `x-invariants` rule it declares. The legacy families use the shared
`semantic-invariants-v1.json` registry; versioned contracts carry their own
invariant file. These rules are mandatory, not advisory. A consumer that runs
only a stock JSON Schema validator has not validated a hosted contract.
The executable golden vectors in `tests/hosted/contracts` bind each rule to a
stable failure category.

Legacy transport-neutral payloads declare `contract_name` and
`contract_version: 1`. Versioned contracts declare their own explicit version
in `index-v2.json`. Schemas use JSON Schema Draft 2020-12, reject unknown
object properties, and contain no endpoint, database, provider, device
storage, import, or export design.

The contracts describe data that later implementations may exchange. They do
not authorize a caller, provider, engine, or browser to publish snapshots,
approve proposals, promote canon, select filesystem paths, run generic tools,
or silently change retrieval or live-session grounding.

See [compatibility.md](compatibility.md) for evolution rules and
[authority-redaction.md](authority-redaction.md) for authority and safe-output
requirements.

The transport-neutral v1 package remains unchanged. The current transport
registry is [`http/index-v5.json`](http/index-v5.json); its preserved v4
registry remains at [`http/index.json`](http/index.json). The current package
set includes general hosted HTTP v2, Campaign Atlas HTTP v2, Live HTTP v1, and
record editor HTTP v1. Older packages stay at stable paths for historical
traceability but are not active.
