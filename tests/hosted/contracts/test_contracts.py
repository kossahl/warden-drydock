import json
import re
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "docs" / "contracts" / "hosted"
INDEX_PATH = CONTRACT_ROOT / "index-v1.json"
EXPECTED_CATEGORY_EVIDENCE = {
    "unsupported_contract_version": "contract_version",
    "unsafe_binding": "identifier",
    "source_digest_conflict": "source",
    "stale_workflow_version": "workflow",
    "stale_controller_epoch": "epoch",
    "stream_sequence_conflict": "sequence",
    "proposal_validation_failure": "validation",
    "proposal_approval_conflict": "approval binding",
    "idempotency_digest_conflict": "digest",
    "capability_rejected": "authority",
    "publication_intent_failure": "intent",
    "snapshot_integrity_failure": "required property",
    "device_replay_conflict": "acknowledgement",
    "live_barrier_conflict": "ready barrier",
    "audit_redaction_failure": "safe_detail",
    "retrieval_consistency_failure": "count",
}
IMPLEMENTED_INVARIANTS = {
    "api_approval_binding", "api_idempotency_binding", "proposal_exact_binding",
    "proposal_validation_gate", "live_revision_pinning", "live_device_replay",
    "live_end_barrier", "snapshot_lineage_quarantine", "provider_stream_order",
    "provider_tool_binding", "retrieval_determinism", "operations_reconciliation",
}
HOSTED_SCHEMA_ID_PREFIX = "https://warden-drydock.invalid/contracts/hosted/v1/"
EXPECTED_SHARED_DEFINITIONS = ["common.schema.json"]
EXPECTED_FAMILY_CONTRACTS = {
    "api": {
        "schema": "api.schema.json",
        "example": "api.json",
        "negative_fixtures": [
            "unknown-version.json",
            "traversal-identifier.json",
            "stale-workflow-version.json",
            "changed-idempotency-digest.json",
            "idempotency-exact-replay-mismatch.json",
        ],
    },
    "atlas": {
        "schema": "atlas.schema.json",
        "example": "atlas.json",
        "negative_fixtures": ["atlas-unsafe-head.json"],
    },
    "engine": {
        "schema": "engine.schema.json",
        "example": "engine.json",
        "negative_fixtures": ["engine-publication-field.json"],
    },
    "snapshot": {
        "schema": "snapshot.schema.json",
        "example": "snapshot.json",
        "negative_fixtures": ["snapshot-unsafe-path.json", "snapshot-missing-bindings.json"],
    },
    "retrieval": {
        "schema": "retrieval.schema.json",
        "example": "retrieval.json",
        "negative_fixtures": ["missing-source-binding.json", "retrieval-count-mismatch.json"],
    },
    "provider": {
        "schema": "provider.schema.json",
        "example": "provider.json",
        "negative_fixtures": [
            "duplicate-stream-sequence.json",
            "secret-leakage.json",
            "provider-authority-widening.json",
            "provider-unbound-tool.json",
        ],
    },
    "live": {
        "schema": "live.schema.json",
        "example": "live.json",
        "negative_fixtures": [
            "stale-controller-epoch.json",
            "live-unaccepted-barrier.json",
            "live-replay-digest-mismatch.json",
        ],
    },
    "proposal": {
        "schema": "proposal.schema.json",
        "example": "proposal.json",
        "negative_fixtures": ["invalid-authority-transition.json", "mismatched-approval-binding.json"],
    },
    "operations": {
        "schema": "operations.schema.json",
        "example": "operations.json",
        "negative_fixtures": [
            "private-path-leakage.json",
            "ambiguous-publication-intent.json",
            "audit-free-text.json",
        ],
    },
}


def reconcile_index_paths(index, contract_root=CONTRACT_ROOT):
    indexed_schemas = {item["schema"] for item in index["families"]} | set(index["shared_definitions"])
    indexed_examples = {item["example"] for item in index["families"]}
    indexed_negative = {fixture for item in index["families"] for fixture in item["negative_fixtures"]}
    disk_schemas = {path.relative_to(contract_root).as_posix() for path in (contract_root / "schemas").rglob("*.json")}
    disk_examples = {path.relative_to(contract_root).as_posix() for path in (contract_root / "examples").rglob("*.json")}
    disk_negative = {path.relative_to(contract_root).as_posix() for path in (contract_root / "negative").rglob("*.json")}
    return {
        "schemas": (indexed_schemas, disk_schemas),
        "examples": (indexed_examples, disk_examples),
        "negative": (indexed_negative, disk_negative),
    }


class ContractValidationError(AssertionError):
    def __init__(self, path, message, category="validation_finding"):
        self.path = path
        self.message = message
        self.category = category
        super().__init__(f"{path or '<root>'}: {message}")


def _json_type_matches(value, expected):
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return checks[expected](value)


def _resolve_local_ref(root_schema, reference):
    if not reference.startswith("#/"):
        raise ContractValidationError("", f"unsupported non-local reference {reference}")
    value = root_schema
    for token in reference[2:].split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    return value


def _errors(instance, schema, root_schema=None, path=""):
    root_schema = root_schema or schema
    if "$ref" in schema:
        yield from _errors(instance, _resolve_local_ref(root_schema, schema["$ref"]), root_schema, path)
        return

    if "oneOf" in schema:
        branch_errors = [list(_errors(instance, branch, root_schema, path)) for branch in schema["oneOf"]]
        if sum(not errors for errors in branch_errors) != 1:
            yield ContractValidationError(path, "oneOf did not select exactly one branch")
        return

    if "events sequence values are strictly increasing in array order" in schema.get("x-invariants", []):
        events = instance.get("events", []) if isinstance(instance, dict) else []
        sequences = [event.get("sequence") for event in events if isinstance(event, dict)]
        if any(current is None or previous is None or current <= previous for previous, current in zip(sequences, sequences[1:])):
            child = f"{path}.events" if path else "events"
            yield ContractValidationError(child, "event sequences are not strictly increasing")

    if "type" in schema:
        expected = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_json_type_matches(instance, kind) for kind in expected):
            yield ContractValidationError(path, f"expected type {expected}")
            return

    if "const" in schema and instance != schema["const"]:
        yield ContractValidationError(path, f"expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        yield ContractValidationError(path, f"value is not in enum {schema['enum']!r}")

    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            yield ContractValidationError(path, "string is shorter than minLength")
        if len(instance) > schema.get("maxLength", len(instance)):
            yield ContractValidationError(path, "string is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            yield ContractValidationError(path, f"string does not match pattern {schema['pattern']}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            yield ContractValidationError(path, "number is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            yield ContractValidationError(path, "number is above maximum")

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            yield ContractValidationError(path, "array is shorter than minItems")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in instance]
            if len(canonical) != len(set(canonical)):
                yield ContractValidationError(path, "array items are not unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                child = f"{path}.{index}" if path else str(index)
                yield from _errors(item, schema["items"], root_schema, child)

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in instance:
                yield ContractValidationError(path, f"missing required property {required}")
        if schema.get("additionalProperties") is False:
            for key in instance.keys() - properties.keys():
                child = f"{path}.{key}" if path else key
                yield ContractValidationError(child, "additional property is forbidden")
        for key, value in instance.items():
            child = f"{path}.{key}" if path else key
            if key in properties:
                yield from _errors(value, properties[key], root_schema, child)
            elif isinstance(schema.get("additionalProperties"), dict):
                yield from _errors(value, schema["additionalProperties"], root_schema, child)


def _semantic_errors(instance, schema):
    name = instance.get("contract_name") if isinstance(instance, dict) else None
    if name == "operation_request":
        receipt = instance.get("prior_receipt")
        if instance.get("operation") == "proposal_approve" and (
            instance.get("expected_revision") is None
            or instance.get("expected_workflow_version") is None
        ):
            yield ContractValidationError("expected_revision", "proposal approval requires revision and workflow binding", "proposal_approval_conflict")
        if receipt and receipt.get("idempotency_key") != instance.get("idempotency_key"):
            yield ContractValidationError("prior_receipt.idempotency_key", "idempotency key binding mismatch", "idempotency_digest_conflict")
        if receipt:
            same = receipt.get("payload_digest") == instance.get("payload_digest")
            if same != receipt.get("same_payload_digest"):
                yield ContractValidationError("prior_receipt.payload_digest", "idempotency digest classification mismatch", "idempotency_digest_conflict")
            if same and receipt.get("outcome") not in {"accepted", "exact_replay"}:
                yield ContractValidationError("prior_receipt.outcome", "same digest must be accepted or exact replay", "idempotency_digest_conflict")
            if not same and receipt.get("outcome") != "digest_conflict":
                yield ContractValidationError("prior_receipt.outcome", "changed digest must conflict", "idempotency_digest_conflict")
    elif name == "canon_proposal":
        proposal, binding, validation = instance["proposal"], instance["approval_binding"], instance["validation"]
        if proposal.get("status") in {"approving", "approved"} and binding is None:
            yield ContractValidationError("approval_binding", "approval state requires Warden binding", "proposal_approval_conflict")
        if proposal.get("status") not in {"approving", "approved"} and binding is not None:
            yield ContractValidationError("approval_binding", "non-approval state cannot be Warden-confirmed", "proposal_approval_conflict")
        if binding is not None:
            for key in ("proposal_id", "proposal_version", "diff_digest", "base_revision", "source_revision"):
                if proposal.get(key) != binding.get(key):
                    yield ContractValidationError(f"approval_binding.{key}", "approval binding mismatch", "proposal_approval_conflict")
            if binding.get("expected_campaign_head") != proposal.get("base_revision"):
                yield ContractValidationError("approval_binding.expected_campaign_head", "expected campaign head must equal proposal base", "proposal_approval_conflict")
        if proposal.get("status") in {"needs_review", "approving", "approved"} and (
            validation.get("status") != "passed" or validation.get("error_count") != 0
        ):
            yield ContractValidationError("validation.status", "review or approval requires passing validation", "proposal_validation_failure")
    elif name == "live_session":
        events = {}
        for event in instance["events"]:
            key = (event["device_id"], event["operation_id"])
            if key in events:
                yield ContractValidationError("events", "device operation identity is duplicated", "device_replay_conflict")
            events[key] = event
            if event.get("base_revision") != instance.get("base_revision"):
                yield ContractValidationError("events.base_revision", "event is not pinned to session base revision", "stale_revision")
        if instance["overlay"].get("base_revision") != instance.get("base_revision"):
            yield ContractValidationError("overlay.base_revision", "overlay is not pinned to session base revision", "stale_revision")
        accepted = set()
        for ack in instance["acknowledgements"]:
            event = events.get((ack["device_id"], ack["operation_id"]))
            if event is None or event["payload_digest"] != ack["payload_digest"]:
                yield ContractValidationError("acknowledgements", "acknowledgement does not bind exact device operation digest", "device_replay_conflict")
            if ack["outcome"] in {"accepted", "exact_replay"}:
                accepted.add(ack["operation_id"])
        barrier = instance["end_barrier"]
        if barrier["ready_for_proposal"]:
            required = set(barrier["accepted_operation_ids"]) | {barrier["end_operation_id"]}
            if not required or not required.issubset(accepted):
                yield ContractValidationError("end_barrier.ready_for_proposal", "ready barrier requires accepted operations and end intent", "live_barrier_conflict")
            end_events = [event for event in events.values() if event["operation_id"] == barrier["end_operation_id"]]
            if len(end_events) != 1 or end_events[0]["event_type"] != "end_intent":
                yield ContractValidationError("end_barrier.end_operation_id", "barrier end operation must be an accepted end intent", "live_barrier_conflict")
    elif name == "provider_generation":
        sequences = [event.get("sequence") for event in instance["events"]]
        if any(current is None or previous is None or current <= previous for previous, current in zip(sequences, sequences[1:])):
            yield ContractValidationError("events.sequence", "stream sequence is not strictly increasing", "stream_sequence_conflict")
        seen_calls = set()
        for event in instance["events"]:
            if event["event_type"] == "tool_request":
                required = ("tool", "call_id", "source_id", "campaign_id", "revision_id", "source_set_digest")
                if any(key not in event for key in required):
                    yield ContractValidationError("events", "tool request is missing authority binding", "capability_rejected")
                call_id = event.get("call_id")
                if call_id in seen_calls:
                    yield ContractValidationError("events", "provider tool call is repeated", "capability_rejected")
                seen_calls.add(call_id)
                for key in ("campaign_id", "revision_id", "source_set_digest"):
                    if event.get(key) != instance.get(key):
                        yield ContractValidationError("events", "provider tool call widened generation binding", "capability_rejected")
    elif name == "snapshot_manifest":
        intent = instance["publication_intent"]
        verified = instance["lineage_status"] == "verified_linear"
        if verified and (intent.get("classification") != "matched" or intent.get("matching_intent_count") != 1 or "quarantine" in instance):
            yield ContractValidationError("lineage_status", "verified lineage requires exactly one matched intent and no quarantine", "publication_intent_failure")
        if not verified and (intent.get("classification") != "quarantined" or "quarantine" not in instance):
            yield ContractValidationError("quarantine", "quarantined lineage requires quarantined intent and reason", "publication_intent_failure")
    elif name == "retrieval_source_envelope":
        citations = instance["citations"]
        orders = [item["order"] for item in citations]
        if orders != list(range(1, len(citations) + 1)):
            yield ContractValidationError("citations", "citation order must be contiguous and deterministic", "retrieval_consistency_failure")
        if instance["source_count"] != len({item["source_id"] for item in citations}):
            yield ContractValidationError("source_count", "source count does not match citations", "retrieval_consistency_failure")
        excerpts = [excerpt for item in citations for excerpt in item.get("excerpts", [])]
        if instance["excerpt_count"] != len(excerpts):
            yield ContractValidationError("excerpt_count", "excerpt count does not match citations", "retrieval_consistency_failure")
        for citation in citations:
            if citation.get("excerpt_count") != len(citation.get("excerpts", [])):
                yield ContractValidationError("citations.excerpt_count", "citation excerpt count mismatch", "retrieval_consistency_failure")
        for excerpt in excerpts:
            encoded = excerpt["text"].encode("utf-8")
            if excerpt["character_count"] != len(excerpt["text"]) or excerpt["byte_count"] != len(encoded):
                yield ContractValidationError("citations.excerpts", "excerpt byte or character count mismatch", "retrieval_consistency_failure")
    elif name == "operational_status":
        reconciliation = instance["reconciliation"]
        if reconciliation["classification"] == "finalized" and reconciliation["matching_intent_count"] != 1:
            yield ContractValidationError("reconciliation.matching_intent_count", "finalized intent requires exactly one match", "publication_intent_failure")
        if reconciliation["classification"] == "quarantined" and "quarantine_code" not in reconciliation:
            yield ContractValidationError("reconciliation.quarantine_code", "quarantine requires a reason code", "publication_intent_failure")


def contract_errors(instance, schema):
    for error in Draft202012Validator(schema).iter_errors(instance):
        path = ".".join(str(part) for part in error.absolute_path)
        name = instance.get("contract_name", "") if isinstance(instance, dict) else ""
        if "contract_version" in path:
            category = "unsupported_contract_version"
        elif name == "engine_staged_result" and any(token in (path + " " + error.message) for token in ("publication", "approve")):
            category = "capability_rejected"
        elif name == "provider_generation" and "events" in path:
            category = "capability_rejected"
        elif name == "snapshot_manifest":
            category = "snapshot_integrity_failure"
        elif name == "canon_proposal":
            category = "proposal_validation_failure"
        elif name == "live_session" and "controller" in path:
            category = "stale_controller_epoch"
        elif name == "operation_request" and "workflow" in path:
            category = "stale_workflow_version"
        elif name == "retrieval_source_envelope":
            category = "source_digest_conflict"
        elif name == "operational_status" and "safe_detail" in (path + " " + error.message):
            category = "audit_redaction_failure"
        elif name == "operational_status" and "reconciliation" in path:
            category = "publication_intent_failure"
        else:
            category = "unsafe_binding"
        yield ContractValidationError(path, error.message, category)
    yield from _semantic_errors(instance, schema)


def validate(instance, schema):
    Draft202012Validator.check_schema(schema)
    failures = list(contract_errors(instance, schema))
    if failures:
        raise failures[0]


class HostedContractPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))

    def _assert_closed_and_complete(self, index, contract_root=CONTRACT_ROOT):
        families = index["families"]
        self.assertEqual(index["contract_version"], 1)
        self.assertEqual(index["contract_index"], "warden_drydock_hosted_contracts")
        self.assertEqual(len(families), 9)
        self.assertEqual(len({item["family"] for item in families}), len(families))
        self.assertTrue(all(item["negative_fixtures"] for item in families))
        for area, (indexed, disk) in reconcile_index_paths(index, contract_root).items():
            self.assertEqual(indexed, disk, area)
        indexed_all = {relative for indexed, _ in reconcile_index_paths(index, contract_root).values() for relative in indexed}
        for relative in indexed_all:
            self.assertTrue((contract_root / relative).is_file(), relative)

    def _assert_index_groupings(self, index, contract_root=CONTRACT_ROOT):
        for family in index["families"]:
            schema = json.loads((contract_root / family["schema"]).read_text(encoding="utf-8"))
            self.assertTrue(schema["$id"].endswith(family["schema"].rsplit("/", 1)[1]))
            for relative in family["negative_fixtures"]:
                fixture = json.loads((contract_root / relative).read_text(encoding="utf-8"))
                self.assertEqual(fixture["schema"], family["schema"])

    def _assert_family_contracts_match_authority(self, index, contract_root=CONTRACT_ROOT):
        family_names = {item["family"] for item in index["families"]}
        self.assertEqual(family_names, set(EXPECTED_FAMILY_CONTRACTS))
        self.assertEqual(
            [relative.rsplit("/", 1)[1] for relative in index["shared_definitions"]],
            EXPECTED_SHARED_DEFINITIONS,
            "shared definition basenames",
        )
        for family in index["families"]:
            canonical = EXPECTED_FAMILY_CONTRACTS[family["family"]]
            self.assertEqual(family["schema"].rsplit("/", 1)[1], canonical["schema"], f"{family['family']} schema basename")
            self.assertEqual(family["example"].rsplit("/", 1)[1], canonical["example"], f"{family['family']} example basename")
            self.assertEqual(
                [relative.rsplit("/", 1)[1] for relative in family["negative_fixtures"]],
                canonical["negative_fixtures"],
                f"{family['family']} negative fixture basenames",
            )
            schema = json.loads((contract_root / family["schema"]).read_text(encoding="utf-8"))
            self.assertEqual(schema["$id"], HOSTED_SCHEMA_ID_PREFIX + canonical["schema"], f"{family['family']} $id")

    def test_index_is_closed_versioned_and_complete(self):
        self._assert_closed_and_complete(self.index)

    def test_index_closure_check_fails_when_a_listed_fixture_is_removed(self):
        missing_fixture = deepcopy(self.index)
        missing_fixture["families"][0]["negative_fixtures"].pop()
        with self.assertRaises(AssertionError):
            self._assert_closed_and_complete(missing_fixture)

        missing_family = deepcopy(self.index)
        missing_family["families"].pop()
        with self.assertRaises(AssertionError):
            self._assert_closed_and_complete(missing_family)

    def test_index_groupings_are_confirmed_by_fixture_and_schema_declarations(self):
        self._assert_index_groupings(self.index)

    def test_family_contracts_are_pinned_by_canonical_authority(self):
        self._assert_family_contracts_match_authority(self.index)

    def _mutated_contract_root(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name) / "contracts"
        shutil.copytree(CONTRACT_ROOT, root)
        return root, deepcopy(self.index)

    def test_lockstep_example_rename_fails_authority_while_old_checks_stay_green(self):
        root, mutated = self._mutated_contract_root()
        for family in mutated["families"]:
            if family["family"] == "api":
                (root / family["example"]).rename(root / "examples" / "v1" / "api-renamed.json")
                family["example"] = "examples/v1/api-renamed.json"
        self._assert_closed_and_complete(mutated, root)
        self._assert_index_groupings(mutated, root)
        with self.assertRaises(AssertionError):
            self._assert_family_contracts_match_authority(mutated, root)

    def test_lockstep_negative_fixture_rename_fails_authority_while_old_checks_stay_green(self):
        root, mutated = self._mutated_contract_root()
        for family in mutated["families"]:
            if family["family"] == "api":
                source = "negative/v1/unknown-version.json"
                target = "negative/v1/unknown-version-renamed.json"
                (root / source).rename(root / target)
                index = family["negative_fixtures"].index(source)
                family["negative_fixtures"][index] = target
        self._assert_closed_and_complete(mutated, root)
        self._assert_index_groupings(mutated, root)
        with self.assertRaises(AssertionError):
            self._assert_family_contracts_match_authority(mutated, root)

    def test_lockstep_shared_definition_rename_fails_authority_while_old_checks_stay_green(self):
        root, mutated = self._mutated_contract_root()
        (root / mutated["shared_definitions"][0]).rename(root / "schemas" / "v1" / "common-renamed.schema.json")
        mutated["shared_definitions"] = ["schemas/v1/common-renamed.schema.json"]
        self._assert_closed_and_complete(mutated, root)
        self._assert_index_groupings(mutated, root)
        with self.assertRaises(AssertionError):
            self._assert_family_contracts_match_authority(mutated, root)

    def test_schema_metadata_and_closed_top_level_objects(self):
        for family in self.index["families"]:
            with self.subTest(family=family["family"]):
                schema = json.loads((CONTRACT_ROOT / family["schema"]).read_text(encoding="utf-8"))
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertTrue(schema["$id"].startswith("https://warden-drydock.invalid/"))
                self.assertTrue(schema["title"].endswith("v1"))
                self.assertFalse(schema["additionalProperties"])
                self.assertIn("contract_name", schema["required"])
                self.assertIn("contract_version", schema["required"])
                self.assertEqual(schema["properties"]["contract_version"]["const"], 1)

    def test_semantic_invariants_are_indexed_and_bound_to_schemas(self):
        relative = self.index["semantic_invariants"]
        specification = json.loads((CONTRACT_ROOT / relative).read_text(encoding="utf-8"))
        self.assertTrue(specification["mandatory_with_json_schema"])
        rules = {rule["id"]: rule for rule in specification["rules"]}
        declared = set()
        for family in self.index["families"]:
            schema = json.loads((CONTRACT_ROOT / family["schema"]).read_text(encoding="utf-8"))
            for rule_id in schema.get("x-invariants", []):
                self.assertIn(rule_id, rules)
                self.assertEqual(rules[rule_id]["schema"], family["schema"])
                declared.add(rule_id)
        self.assertEqual(declared, set(rules))
        self.assertEqual(declared, IMPLEMENTED_INVARIANTS)

    def test_every_positive_example_validates(self):
        for family in self.index["families"]:
            schema = json.loads((CONTRACT_ROOT / family["schema"]).read_text(encoding="utf-8"))
            example = json.loads((CONTRACT_ROOT / family["example"]).read_text(encoding="utf-8"))
            with self.subTest(family=family["family"]):
                validate(example, schema)

    def test_every_negative_fixture_fails_at_expected_binding(self):
        fixture_paths = [fixture for family in self.index["families"] for fixture in family["negative_fixtures"]]
        for relative in fixture_paths:
            fixture = json.loads((CONTRACT_ROOT / relative).read_text(encoding="utf-8"))
            schema = json.loads((CONTRACT_ROOT / fixture["schema"]).read_text(encoding="utf-8"))
            failures = list(contract_errors(fixture["instance"], schema))
            with self.subTest(fixture=relative, category=fixture["expected_category"]):
                self.assertTrue(failures, "negative fixture unexpectedly validated")
                categories = {failure.category for failure in failures}
                self.assertIn(fixture["expected_category"], categories)
                evidence = " ".join(f"{failure.path} {failure.message}" for failure in failures if failure.category == fixture["expected_category"])
                expected = fixture.get(
                    "expected_evidence",
                    fixture.get("expected_path", EXPECTED_CATEGORY_EVIDENCE[fixture["expected_category"]]).split(".")[-1],
                )
                self.assertIn(expected.lower(), evidence.lower())

    def test_required_negative_categories_are_represented(self):
        required = {
            "unsupported_contract_version", "unsafe_binding", "source_digest_conflict",
            "stale_workflow_version", "stale_controller_epoch", "stream_sequence_conflict",
            "proposal_validation_failure", "idempotency_digest_conflict",
            "capability_rejected", "publication_intent_failure",
        }
        fixtures = [
            json.loads((CONTRACT_ROOT / relative).read_text(encoding="utf-8"))
            for family in self.index["families"] for relative in family["negative_fixtures"]
        ]
        self.assertTrue(required.issubset({fixture["expected_category"] for fixture in fixtures}))

    def test_engine_and_provider_cannot_publish_or_approve(self):
        engine = (CONTRACT_ROOT / "schemas/v1/engine.schema.json").read_text(encoding="utf-8")
        self.assertNotRegex(engine, r'"(?:publish|publication|approve|promote|path|url|sql|shell)"\s*:')
        provider_schema = json.loads((CONTRACT_ROOT / "schemas/v1/provider.schema.json").read_text(encoding="utf-8"))
        tools = provider_schema["$defs"]["event"]["oneOf"][0]["properties"]["tool"]["enum"]
        self.assertEqual(tools, [
            "read_bound_source", "read_bound_relationships", "read_bound_history", "emit_proposal_draft"
        ])

    def test_shared_vocabulary_is_canonical(self):
        corpus = "\n".join(path.read_text(encoding="utf-8") for path in CONTRACT_ROOT.rglob("*.json"))
        expected_terms = {
            "campaign", "revision", "entity", "connection", "live_session", "event",
            "overlay", "draft", "proposal", "citation", "audit_event", "operation_request",
        }
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, corpus.lower())

    def test_deferred_and_redacted_boundaries_are_documented(self):
        compatibility = (CONTRACT_ROOT / "compatibility.md").read_text(encoding="utf-8").lower()
        authority = (CONTRACT_ROOT / "authority-redaction.md").read_text(encoding="utf-8").lower()
        for deferred in ("endpoint", "storage", "import/export", "provider selection", "device persistence"):
            self.assertIn(deferred, compatibility)
        for forbidden in ("credentials", "prompts", "private paths", "provider-native events"):
            self.assertIn(forbidden, authority)

    def test_reviewed_authority_counterexamples_fail_closed(self):
        cases = []
        for family_name in ("api", "proposal", "provider", "retrieval"):
            family = next(item for item in self.index["families"] if item["family"] == family_name)
            schema = json.loads((CONTRACT_ROOT / family["schema"]).read_text(encoding="utf-8"))
            example = json.loads((CONTRACT_ROOT / family["example"]).read_text(encoding="utf-8"))
            if family_name == "api":
                broken = deepcopy(example)
                broken["expected_revision"] = None
                cases.append(("approval_without_revision", broken, schema))
            elif family_name == "proposal":
                broken = deepcopy(example)
                broken["proposal"]["status"] = "approved"
                broken["validation"]["status"] = "failed"
                broken["validation"]["error_count"] = 1
                cases.append(("approved_failed_validation", broken, schema))
            elif family_name == "provider":
                broken = deepcopy(example)
                tool = {
                    "sequence": 2, "event_type": "tool_request", "call_id": "call_one",
                    "tool": "read_bound_source", "source_id": "entity_one",
                    "campaign_id": broken["campaign_id"], "revision_id": broken["revision_id"],
                    "source_set_digest": broken["source_set_digest"],
                }
                broken["events"] = [tool, {**tool, "sequence": 3}]
                cases.append(("repeated_provider_call", broken, schema))
            else:
                broken = deepcopy(example)
                broken["citations"].append(deepcopy(broken["citations"][0]))
                broken["citations"][1]["citation_id"] = "citation_two"
                cases.append(("duplicate_retrieval_order", broken, schema))
        for name, instance, schema in cases:
            with self.subTest(case=name):
                self.assertTrue(list(contract_errors(instance, schema)))

    def test_quarantined_ambiguous_snapshot_is_representable(self):
        family = next(item for item in self.index["families"] if item["family"] == "snapshot")
        schema = json.loads((CONTRACT_ROOT / family["schema"]).read_text(encoding="utf-8"))
        instance = json.loads((CONTRACT_ROOT / family["example"]).read_text(encoding="utf-8"))
        instance["publication_intent"] = {"classification": "quarantined", "matching_intent_count": 2}
        instance["lineage_status"] = "quarantined"
        instance["quarantine"] = {"reason_code": "ambiguous_intent"}
        validate(instance, schema)


if __name__ == "__main__":
    unittest.main()
