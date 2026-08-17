"""Literal synthetic fixture and canonical digests for the provider bake-off."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

CAMPAIGN_ID = "campaign-erebos"
BASE_REVISION = "fixture-erebos-r3"
FIXTURE_VERSION = "provider-bakeoff-fixture.v1"

RECORDS = {
    "location-erebos": """---
id: location-erebos
type: location
name: Erebos Airlock
status: canon
connections:
  - npc-vale
  - faction-helix
---

## Current state

The Erebos airlock is open.

## Connections

Vale has access to the airlock. The airlock control relay belongs to Helix.
""",
    "npc-vale": """---
id: npc-vale
type: npc
name: Vale
status: canon
connections:
  - location-erebos
  - faction-helix
---

## Current record

No canon death event is recorded for Vale.
""",
    "faction-helix": """---
id: faction-helix
type: faction
name: Helix
status: canon
connections:
  - location-erebos
---

## Current state

The Helix relay is intact.
""",
    "location-infirmary": """---
id: location-infirmary
type: location
name: Erebos Infirmary
status: canon
connections:
  - location-erebos
---

## Current state

The infirmary is powered and available to the crew.
""",
    "prep-erebos": """---
id: prep-erebos
type: preparation
name: Erebos contingency
status: draft
connections:
  - location-erebos
  - npc-vale
---

## Prediction

Vale will die and the Erebos airlock will be sealed.
""",
    "session-003": """---
id: session-003
type: session
name: Relay breach
status: canon
connections:
  - location-erebos
  - npc-vale
  - faction-helix
---

## Approved events

At 22:10, the crew opened the Erebos airlock. It remains open.
""",
}

LIVE_OPERATIONS = {
    "op-confirmed-001": {"base_revision": BASE_REVISION, "device_id": "device-fixture-a", "local_order": 1, "operation_id": "op-confirmed-001", "operation_type": "confirmed_fact", "payload": {"text": "Vale entered the Helix relay and activated the beacon."}, "session_id": "session-live-004"},
    "op-question-001": {"base_revision": BASE_REVISION, "device_id": "device-fixture-a", "local_order": 2, "operation_id": "op-question-001", "operation_type": "unresolved_question", "payload": {"text": "Who sent the signal?"}, "session_id": "session-live-004"},
}

ENVELOPES = {
    "ask-airlock-v1": {"base_revision": BASE_REVISION, "campaign_id": CAMPAIGN_ID, "contract_version": "source_envelope.v1", "excluded_source_ids": ["prep-erebos", "op-question-001"], "included_source_ids": ["location-erebos", "session-003"], "task_id": "ask-airlock-v1"},
    "check-vale-death-v1": {"base_revision": BASE_REVISION, "campaign_id": CAMPAIGN_ID, "contract_version": "source_envelope.v1", "excluded_source_ids": ["op-question-001"], "included_source_ids": ["npc-vale", "prep-erebos", "op-confirmed-001"], "task_id": "check-vale-death-v1"},
    "generate-infirmary-v1": {"base_revision": BASE_REVISION, "campaign_id": CAMPAIGN_ID, "contract_version": "source_envelope.v1", "excluded_source_ids": ["op-confirmed-001", "op-question-001", "prep-erebos"], "included_source_ids": ["location-infirmary"], "task_id": "generate-infirmary-v1"},
    "tool-beacon-debrief-v1": {"base_revision": BASE_REVISION, "campaign_id": CAMPAIGN_ID, "contract_version": "source_envelope.v1", "excluded_source_ids": ["op-question-001", "prep-erebos"], "included_source_ids": ["faction-helix", "op-confirmed-001", "session-003"], "task_id": "tool-beacon-debrief-v1"},
}

def canonical_json(value: Any) -> bytes:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return unicodedata.normalize("NFC", text).encode("utf-8")

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def source_value(source_id: str) -> Any:
    if source_id in RECORDS:
        return RECORDS[source_id]
    return LIVE_OPERATIONS[source_id]

def resolved_envelope(task_id: str) -> dict[str, Any]:
    envelope = dict(ENVELOPES[task_id])
    envelope["sources"] = [{"source_id": source_id, "content": source_value(source_id)} for source_id in envelope["included_source_ids"]]
    return envelope

def envelope_digest(task_id: str) -> str:
    return sha256(canonical_json(resolved_envelope(task_id)))

def build_manifest() -> dict[str, Any]:
    entries = []
    for source_id in sorted(RECORDS):
        data = unicodedata.normalize("NFC", RECORDS[source_id]).encode("utf-8")
        entries.append({"kind": "record", "source_id": source_id, "sha256": sha256(data), "bytes": len(data)})
    for source_id in sorted(LIVE_OPERATIONS):
        data = canonical_json(LIVE_OPERATIONS[source_id])
        entries.append({"kind": "live_operation", "source_id": source_id, "sha256": sha256(data), "bytes": len(data)})
    envelopes = [{"task_id": task_id, "sha256": envelope_digest(task_id)} for task_id in sorted(ENVELOPES)]
    body = {"base_revision": BASE_REVISION, "campaign_id": CAMPAIGN_ID, "contract_version": FIXTURE_VERSION, "entries": entries, "envelopes": envelopes}
    return {**body, "manifest_sha256": sha256(canonical_json(body))}

TOOL_SCHEMAS = {
    "fixture_read_source": {"$schema": "https://json-schema.org/draft/2020-12/schema", "additionalProperties": False, "properties": {"base_revision": {"const": BASE_REVISION}, "campaign_id": {"const": CAMPAIGN_ID}, "source_id": {"type": "string"}, "source_set_digest": {"pattern": "^[0-9a-f]{64}$", "type": "string"}}, "required": ["campaign_id", "base_revision", "source_set_digest", "source_id"], "type": "object"},
    "fixture_read_revision_context": {"$schema": "https://json-schema.org/draft/2020-12/schema", "additionalProperties": False, "properties": {"anchor_source_id": {"type": "string"}, "base_revision": {"const": BASE_REVISION}, "campaign_id": {"const": CAMPAIGN_ID}, "limit": {"maximum": 5, "minimum": 1, "type": "integer"}, "query_type": {"enum": ["relationship", "history"], "type": "string"}, "source_set_digest": {"pattern": "^[0-9a-f]{64}$", "type": "string"}}, "required": ["campaign_id", "base_revision", "source_set_digest", "query_type", "anchor_source_id", "limit"], "type": "object"},
    "fixture_emit_proposal_draft": {"$schema": "https://json-schema.org/draft/2020-12/schema", "additionalProperties": False, "properties": {"base_revision": {"const": BASE_REVISION}, "campaign_id": {"const": CAMPAIGN_ID}, "proposal_kind": {"const": "beacon_debrief"}, "source_ids": {"items": {"type": "string"}, "minItems": 1, "type": "array", "uniqueItems": True}, "source_set_digest": {"pattern": "^[0-9a-f]{64}$", "type": "string"}, "suggested_changes": {"items": {"type": "string"}, "maxItems": 5, "minItems": 1, "type": "array"}, "title": {"const": "Beacon debrief"}}, "required": ["campaign_id", "base_revision", "source_set_digest", "proposal_kind", "title", "source_ids", "suggested_changes"], "type": "object"},
}
