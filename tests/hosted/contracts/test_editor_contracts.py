from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http" / "editor" / "v1"


def canonical_digest(value: object, *, ensure_ascii: bool = True) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_content_digest(record: dict) -> str:
    projection = {
        key: record[key]
        for key in (
            "record_id",
            "record_type",
            "displayed_name",
            "status",
            "authority",
            "visibility",
            "fields",
            "connections",
        )
    }
    projection["sections"] = [
        {**section, "body": section["body"].replace("\r\n", "\n").replace("\r", "\n")}
        for section in record["sections"]
    ]
    return canonical_digest(projection, ensure_ascii=False)


def evaluate_semantic_failure(fixture: dict) -> tuple[str, str] | None:
    """Return the first contract violation found in a negative fixture."""
    instance = fixture["instance"]
    operation = instance.get("operation_request", {})
    binding = instance.get("binding", {})
    candidate = instance.get("candidate")

    if operation.get("operation") == "editor_proposal_correct":
        prior_ref = instance.get("prior_proposal", {})
        if operation.get("subject_id") != prior_ref.get("proposal_id"):
            return "unsafe_binding", "operation_request.subject_id"
    elif operation and operation.get("subject_id") != binding.get("record_id"):
        return "unsafe_binding", "operation_request.subject_id"

    if isinstance(candidate, dict) and candidate.get("record_id") != binding.get("record_id"):
        return "unsafe_binding", "candidate.record_id"

    receipt = fixture.get("stored_receipt")
    if receipt and receipt.get("idempotency_key") == operation.get("idempotency_key"):
        if receipt.get("payload_digest") != operation.get("payload_digest"):
            return "replay_mismatch", "operation_request.payload_digest"

    context = fixture.get("semantic_context", {})
    base_revision = binding.get("base_revision", {})
    if context.get("current_head_revision") != base_revision.get("revision_id"):
        if "current_head_revision" in context:
            return "stale_revision", "binding.base_revision"
    if context.get("current_record_digest") != binding.get("record_digest"):
        if "current_record_digest" in context:
            return "stale_record_digest", "binding.record_digest"
    if context.get("current_editor_workflow_version") != binding.get("expected_editor_workflow_version"):
        if "current_editor_workflow_version" in context:
            return "workflow_conflict", "binding.expected_editor_workflow_version"

    if isinstance(candidate, dict):
        expected_authority = {
            "canon": "canon",
            "revealed": "revealed",
        }.get(candidate.get("status"), "preparation")
        if candidate.get("authority") != expected_authority:
            return "invalid_authority_transition", "candidate.authority"

        available_ids = set(context.get("available_record_ids", []))
        for index, connection in enumerate(candidate.get("connections", [])):
            if available_ids and connection.get("target_record_id") not in available_ids:
                return "invalid_connections", f"candidate.connections.{index}.target_record_id"

    prior_proposal = context.get("prior_proposal")
    if prior_proposal and (
        instance.get("mutation_kind") != prior_proposal.get("mutation_kind")
        or binding.get("record_id") != prior_proposal.get("record_id")
    ):
        return "invalid_correction", "prior_proposal"

    impact = fixture.get("impact", {})
    required_reference_id = impact.get("required_reference_id")
    resolutions = {item.get("reference_id") for item in instance.get("resolutions", [])}
    if required_reference_id and not impact.get("permitted_unresolved") and required_reference_id not in resolutions:
        return "incomplete_removal_resolution", "resolutions"

    diff = instance.get("diff")
    if isinstance(diff, dict):
        removed_card = next(
            (card for card in diff.get("cards", []) if card.get("kind") == "record_removed"),
            None,
        )
        if removed_card:
            expected = {
                connection["connection_id"]: connection
                for connection in removed_card.get("before", {}).get("connections", [])
            }
            actual_cards = [
                card for card in diff.get("cards", []) if card.get("kind") == "connection_removed"
            ]
            actual = {
                card["connection"]["connection_id"]: card["connection"]
                for card in actual_cards
            }
            if set(expected) != set(actual):
                return "mutation_consistency", "diff.cards.connection_delta"
            if any(expected[key] != actual[key] for key in expected):
                return "mutation_consistency", "diff.cards.connection"

    return None


class HostedRecordEditorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads((CONTRACT_ROOT / "index.json").read_text(encoding="utf-8"))
        cls.schema = json.loads((CONTRACT_ROOT / cls.index["schema"]).read_text(encoding="utf-8"))
        cls.examples_document = json.loads((CONTRACT_ROOT / cls.index["examples"]).read_text(encoding="utf-8"))
        cls.examples = cls.examples_document["examples"]
        cls.routes = json.loads((CONTRACT_ROOT / cls.index["routes"]).read_text(encoding="utf-8"))
        cls.invariants = json.loads((CONTRACT_ROOT / cls.index["semantic_invariants"]).read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(cls.schema)

    def test_editor_index_is_explicit_and_complete(self) -> None:
        fixtures = self.index["negative_fixtures"]
        self.assertIsInstance(fixtures, list)
        self.assertEqual(sorted(fixtures), [f"negative/{path.name}" for path in sorted((CONTRACT_ROOT / "negative").glob("*.json"))])
        self.assertEqual(self.schema["x-invariants"], [rule["id"] for rule in self.invariants["rules"]])
        self.assertEqual(
            set(self.invariants["digest_projections"]),
            {"canonical_digest", "text_digest", "record_content_digest", "impact_digest", "diff_digest", "validation_digest", "proposal_payload_digest", "operation_payload_digest", "duplicate_bindings"},
        )

    def test_every_editor_example_is_schema_valid(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        for example in self.examples:
            with self.subTest(example=example["name"]):
                self.assertEqual([], list(self.validator.iter_errors(example["payload"])))

    def test_validation_errors_carry_findings_and_field_paths_allow_underscores(self) -> None:
        validation_error = next(item["payload"] for item in self.examples if item["name"] == "validation_error")
        self.assertEqual([], list(self.validator.iter_errors(validation_error)))
        missing_findings = deepcopy(validation_error)
        del missing_findings["error"]["findings"]
        self.assertTrue(list(self.validator.iter_errors(missing_findings)))

        property_schema = {
            "$schema": self.schema["$schema"],
            "$defs": self.schema["$defs"],
            "$ref": "#/$defs/property_change",
        }
        property_validator = Draft202012Validator(property_schema)
        self.assertEqual(
            [],
            list(property_validator.iter_errors({
                "property": "fields.current_status",
                "before": "old",
                "after": "new",
            })),
        )

    def test_negative_fixtures_are_schema_valid_and_exercise_declared_rules(self) -> None:
        mapping = self.invariants["error_category_mapping"]
        for relative in self.index["negative_fixtures"]:
            fixture = json.loads((CONTRACT_ROOT / relative).read_text(encoding="utf-8"))
            instance = fixture["instance"]
            with self.subTest(fixture=relative):
                self.assertEqual([], list(self.validator.iter_errors(instance)))
                self.assertTrue(fixture["expected_path"])
                self.assertIn(fixture["expected_category"], set(mapping) | set(mapping.values()) | {"unsafe_binding"})
                semantic_failure = evaluate_semantic_failure(fixture)
                self.assertIsNotNone(semantic_failure)
                self.assertEqual((fixture["expected_category"], fixture["expected_path"]), semantic_failure)

    def test_digest_projections_are_deterministic(self) -> None:
        head = next(item["payload"] for item in self.examples if item["name"] == "head_record_view")
        record = deepcopy(head["record"])
        first = record_content_digest(record)
        record["content_digest"] = "f" * 64
        self.assertEqual(first, record_content_digest(record))
        record["sections"][0]["body"] = record["sections"][0]["body"].replace("\n", "\r\n")
        self.assertEqual(first, record_content_digest(record))
        record["displayed_name"] = "A different station"
        self.assertNotEqual(first, record_content_digest(record))

    def test_mutation_routes_advertise_structured_validation_errors(self) -> None:
        self.assertEqual("error_response", self.routes["error_response"])
        mutation_routes = [
            route for route in self.routes["routes"]
            if route["method"] == "POST" and route["id"] in {
                "editor_record_create",
                "editor_record_edit",
                "editor_record_remove",
                "editor_proposal_correct",
            }
        ]
        self.assertTrue(mutation_routes)
        for route in mutation_routes:
            with self.subTest(route=route["id"]):
                self.assertIn("proposal_validation_failure", route["error_status"]["422"])

    def test_corrections_use_mutation_candidates(self) -> None:
        correction = deepcopy(next(item["payload"] for item in self.examples_document["examples"] if item["name"] == "correction_request"))
        correction["candidate"]["status"] = "missing"
        self.assertTrue(list(self.validator.iter_errors(correction)))

    def test_digest_projections_name_every_source_field(self) -> None:
        projections = self.invariants["digest_projections"]
        expected_fields = {
            "record_content_digest": {"record_id", "record_type", "displayed_name", "status", "authority", "visibility", "fields", "sections", "connections", "content_digest"},
            "impact_digest": {"contract_name", "contract_version", "binding", "impact_digest", "record", "outgoing_connections", "incoming_references", "backlink_policy"},
            "diff_digest": {"diff_digest", "cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest", "source_changes", "summary"},
            "validation_digest": {"status", "validation_digest", "error_count", "findings"},
            "proposal_payload_digest": {"contract_name", "contract_version", "proposal_id", "proposal_version", "campaign_id", "source_revision", "base_revision", "expected_campaign_head", "editor_workflow_version", "proposal_payload_digest", "mutation_kind", "record_bindings", "core_proposal", "correction_of", "diff", "impact_digest", "impact_binding", "resolutions", "validation", "authority_outcome", "visibility_outcome", "publication"},
        }
        for name, fields in expected_fields.items():
            with self.subTest(digest=name):
                projection = projections[name]
                self.assertEqual(fields, set(projection["include_fields"]))
                self.assertTrue(set(projection["exclude_fields"]).issubset(fields))
        self.assertEqual(
            ["contract_name", "contract_version", "operation_request"],
            projections["operation_payload_digest"]["exclude_fields"],
        )


if __name__ == "__main__":
    unittest.main()
