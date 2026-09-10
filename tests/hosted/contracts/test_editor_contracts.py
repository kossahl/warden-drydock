from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
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
                self._assert_semantic_mutation(fixture)

    def _assert_semantic_mutation(self, fixture: dict) -> None:
        instance = fixture["instance"]
        category = fixture["expected_category"]
        if category == "incomplete_removal_resolution":
            self.assertEqual([], instance["resolutions"])
        elif category == "invalid_authority_transition":
            expected = "canon" if instance["candidate"]["status"] == "canon" else "revealed" if instance["candidate"]["status"] == "revealed" else "preparation"
            self.assertNotEqual(expected, instance["candidate"]["authority"])
        elif category == "invalid_connections":
            target = instance["candidate"]["connections"][0]["target_record_id"]
            self.assertEqual("record-unknown", target)
        elif category == "mutation_consistency":
            removed = next(card for card in instance["diff"]["cards"] if card["kind"] == "record_removed")
            expected = {connection["connection_id"]: connection for connection in removed["before"]["connections"]}
            actual = {card["connection"]["connection_id"]: card["connection"] for card in instance["diff"]["cards"] if card["kind"] == "connection_removed"}
            self.assertTrue(set(expected) != set(actual) or any(expected[key] != actual[key] for key in set(expected) & set(actual)))
        elif category == "replay_mismatch":
            ignored = {"contract_name", "contract_version", "operation_request", "request_id", "idempotency_key", "payload_digest"}
            payload = {key: value for key, value in instance.items() if key not in ignored}
            self.assertEqual(instance["operation_request"]["payload_digest"], canonical_digest(payload))
            self.assertEqual("idem_replay", instance["operation_request"]["idempotency_key"])
        elif category == "stale_record_digest":
            self.assertNotEqual(instance["binding"]["record_digest"], instance["candidate"]["content_digest"])
        elif category == "unsafe_binding":
            self.assertIsNone(re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", instance["operation_request"]["subject_id"]))
        elif category == "workflow_conflict":
            self.assertEqual(instance["operation_request"]["expected_editor_workflow_version"], instance["binding"]["expected_editor_workflow_version"])
        elif category == "invalid_correction":
            self.assertEqual("editor_proposal_correct", instance["operation_request"]["operation"])
            self.assertEqual(instance["operation_request"]["subject_id"], instance["prior_proposal"]["proposal_id"])
        elif category == "stale_revision":
            self.assertEqual(instance["operation_request"]["expected_revision"], instance["binding"]["base_revision"]["revision_id"])
        else:
            self.fail(f"No semantic fixture assertion for {category}")

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


if __name__ == "__main__":
    unittest.main()
