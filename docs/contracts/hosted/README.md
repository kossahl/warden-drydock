# Hosted contract package v1

This directory is the transport-neutral contract vocabulary for the localhost,
single-Warden pilot. `index-v1.json` is the machine-readable entry point. It
lists every bounded schema family, positive example, negative fixture, and
authority owner.

Validation requires both the Draft 2020-12 schema named by a family and every
normative `x-invariants` rule it declares. The versioned registry is
`semantic-invariants-v1.json` and is mandatory, not advisory. A consumer that
runs only a stock JSON Schema validator has not validated a hosted contract.
The executable golden vectors in `tests/hosted/contracts` bind each rule to a
stable failure category.

All transport-neutral payloads declare `contract_name` and `contract_version`.
Their accepted version remains 1. Schemas use JSON Schema Draft 2020-12, reject
unknown object properties, and contain no endpoint, database, provider, device
storage, import, or export design.

The contracts describe data that later implementations may exchange. They do
not authorize a caller, provider, engine, or browser to publish snapshots,
approve proposals, promote canon, select filesystem paths, run generic tools,
or silently change retrieval or live-session grounding.

See [compatibility.md](compatibility.md) for evolution rules and
[authority-redaction.md](authority-redaction.md) for authority and safe-output
requirements.

The transport-neutral v1 package remains unchanged. The HTTP registry at
[`http/index.json`](http/index.json) lists the complete active HTTP package set:
general hosted HTTP v2 and Campaign Atlas HTTP v2. General hosted HTTP v1 and
Campaign Atlas HTTP v1 stay at their stable paths for historical traceability
but are no longer active.
