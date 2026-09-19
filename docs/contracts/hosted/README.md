# Hosted contract package

This directory is the transport-neutral contract vocabulary for the localhost,
single-Warden pilot. `index-v2.json` is the current machine-readable entry
point and selector. It lists the current versioned contracts, the still-used
transport-neutral v1 families, their examples and negative fixtures, and the
active HTTP registry. `index-v1.json` remains a supported legacy contract
registry for consumers of the original transport-neutral v1 families.

Validation requires both the Draft 2020-12 schema named by a family and every
normative `x-invariants` rule it declares. The versioned registry is
`semantic-invariants-v1.json` and is mandatory, not advisory. A consumer that
runs only a stock JSON Schema validator has not validated a hosted contract.
The executable golden vectors in `tests/hosted/contracts` bind each rule to a
stable failure category.

All payloads declare `contract_name` and `contract_version`. The current index
includes the v2 `canon_proposal` contract alongside transport-neutral v1
families; v1 is supported, not the only accepted version. Schemas use JSON
Schema Draft 2020-12, reject unknown object properties, and contain no
endpoint, database, provider, device storage, import, or export design.

The contracts describe data that later implementations may exchange. They do
not authorize a caller, provider, engine, or browser to publish snapshots,
approve proposals, promote canon, select filesystem paths, run generic tools,
or silently change retrieval or live-session grounding.

See [compatibility.md](compatibility.md) for evolution rules and
[authority-redaction.md](authority-redaction.md) for authority and safe-output
requirements.

The HTTP registry at [`http/index.json`](http/index.json) lists the complete
active HTTP package set:
general hosted HTTP v2 and Campaign Atlas HTTP v2. General hosted HTTP v1 and
Campaign Atlas HTTP v1 stay at their stable paths for historical traceability
but are no longer active.
