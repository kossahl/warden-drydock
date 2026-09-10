from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from warden_drydock.standalone import _section_lines, frontmatter, parse_connections


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


def record_documents(value: object):
    if isinstance(value, dict):
        if all(
            key in value
            for key in (
                "record_id",
                "record_type",
                "displayed_name",
                "status",
                "authority",
                "visibility",
                "fields",
                "sections",
                "connections",
                "content_digest",
            )
        ):
            yield value
        for child in value.values():
            yield from record_documents(child)
    elif isinstance(value, list):
        for child in value:
            yield from record_documents(child)


def projection_digest(value: dict, definition: dict) -> str:
    excluded = set(definition["exclude_fields"])
    projection = {
        key: value[key]
        for key in definition["include_fields"]
        if key in value and key not in excluded
    }
    return canonical_digest(projection)


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
        self.assertEqual(
            [],
            list(property_validator.iter_errors({
                "property": "status",
                "before": {"classification": "unknown", "value": "legacy-state"},
                "after": "review",
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

    def test_positive_examples_match_declared_digest_projections(self) -> None:
        projections = self.invariants["digest_projections"]
        for example in self.examples:
            payload = example["payload"]
            with self.subTest(example=example["name"]):
                for record in record_documents(payload):
                    self.assertEqual(record_content_digest(record), record["content_digest"])
                if payload.get("contract_name") == "editor_removal_impact":
                    self.assertEqual(
                        projection_digest(payload, projections["impact_digest"]),
                        payload["impact_digest"],
                    )
                if payload.get("contract_name") == "editor_proposal_view":
                    self.assertEqual(
                        projection_digest(payload["diff"], projections["diff_digest"]),
                        payload["diff"]["diff_digest"],
                    )
                    self.assertEqual(
                        payload["diff"]["diff_digest"],
                        payload["core_proposal"]["proposal"]["diff_digest"],
                    )
                    self.assertEqual(
                        projection_digest(payload, projections["proposal_payload_digest"]),
                        payload["proposal_payload_digest"],
                    )
                if "operation_request" in payload:
                    self.assertEqual(
                        projection_digest(payload, projections["operation_payload_digest"]),
                        payload["operation_request"]["payload_digest"],
                    )

    def test_empty_revision_creation_context_is_record_independent(self) -> None:
        context = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "creation_context_empty_revision"
        )
        route = next(
            item for item in self.routes["routes"] if item["id"] == "editor_creation_context_read"
        )
        self.assertEqual(route["response"], context["contract_name"])
        self.assertEqual(context["viewed_revision"], context["head_revision"])
        self.assertIn(
            "editor_creation_context_binding",
            {rule["id"] for rule in self.invariants["rules"]},
        )

    def test_mutation_routes_advertise_structured_validation_errors(self) -> None:
        self.assertEqual("error_response", self.routes["error_response"])
        self.assertEqual(3, self.routes["error_response_version"])
        validation_error = next(item["payload"] for item in self.examples if item["name"] == "validation_error")
        self.assertEqual(3, validation_error["contract_version"])
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
        self.assertIn("diff_digest", projections["operation_payload_digest"]["include_fields"])
        self.assertEqual(
            ["contract_name", "contract_version", "operation_request"],
            projections["operation_payload_digest"]["exclude_fields"],
        )

    def test_source_snapshots_match_change_type(self) -> None:
        proposal = next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        for change_type, before_source, after_source in (
            ("create", "before", None),
            ("update", "before", "after"),
            ("delete", "before", None),
        ):
            with self.subTest(change_type=change_type):
                candidate = deepcopy(proposal)
                source = candidate["diff"]["source_changes"][0]
                source["change_type"] = change_type
                source["before_source"] = before_source
                source["after_source"] = after_source
                if change_type == "create":
                    source["before_source"] = "before"
                elif change_type == "update":
                    source["before_source"] = None
                elif change_type == "delete":
                    source["after_source"] = "after"
                self.assertTrue(list(self.validator.iter_errors(candidate)))

    def test_source_snapshots_match_structured_records_and_connections(self) -> None:
        for name in ("editor_proposal_view", "removal_proposal_with_outgoing_connections"):
            proposal = next(item["payload"] for item in self.examples if item["name"] == name)
            for source in proposal["diff"]["source_changes"]:
                subject = source["subject_record_id"]
                resolution = next(
                    (
                        card
                        for card in proposal["diff"]["cards"]
                        if card["kind"] == "reference_resolution"
                        and card["subject_record_id"] == subject
                    ),
                    None,
                )
                for side, source_text in (
                    ("before", source.get("before_source")),
                    ("after", source.get("after_source")),
                ):
                    if source_text is None:
                        continue
                    with self.subTest(example=name, subject=subject, side=side):
                        metadata = frontmatter(source_text)
                        self.assertTrue(
                            {
                                "id",
                                "type",
                                "status",
                                "ownership",
                                "name",
                                "visibility",
                                "warden_only",
                            }.issubset(metadata)
                        )
                        self.assertEqual(subject, metadata["id"])
                        self.assertEqual(
                            f"# {metadata['name']}",
                            next(
                                line
                                for line in source_text.splitlines()
                                if line.startswith("# ")
                            ),
                        )
                        parsed_connections, errors = parse_connections(
                            source_text,
                            source_id=subject,
                            path=Path("synthetic.md"),
                        )
                        self.assertEqual([], errors)

                        structured = next(
                            (
                                card[side]
                                for card in proposal["diff"]["cards"]
                                if card["subject_record_id"] == subject
                                and isinstance(card.get(side), dict)
                                and "record_id" in card[side]
                            ),
                            None,
                        )
                        if structured is not None:
                            self.assertEqual(structured["record_type"], metadata["type"])
                            self.assertEqual(structured["displayed_name"], metadata["name"])
                            self.assertEqual(structured["status"], metadata["status"])
                            self.assertEqual(
                                structured["visibility"]["audience"],
                                metadata["visibility"],
                            )
                            self.assertEqual(
                                str(structured["visibility"]["warden_only"]).lower(),
                                metadata["warden_only"],
                            )
                            for field in structured["fields"]:
                                self.assertEqual(str(field["value"]), metadata[field["field_id"]])
                            for section in structured["sections"]:
                                self.assertEqual(
                                    section["body"].splitlines(),
                                    [
                                        line
                                        for _, line in _section_lines(
                                            source_text, section["section_id"]
                                        )
                                        if line.strip()
                                    ],
                                )
                            expected_connections = [
                                (
                                    connection["target_record_id"],
                                    connection["relationship"],
                                    connection["state"],
                                    connection["context"],
                                )
                                for connection in structured["connections"]
                            ]
                        else:
                            self.assertIsNotNone(resolution)
                            reference = resolution["before"]
                            target = (
                                resolution["after"]["replacement_target_record_id"]
                                if side == "after"
                                else reference["target_record_id"]
                            )
                            expected_connections = [
                                (
                                    target,
                                    reference["relationship"],
                                    reference["state"],
                                    reference["context"],
                                )
                            ]
                        self.assertEqual(
                            expected_connections,
                            [
                                (item.target_id, item.relationship, item.state, item.context)
                                for item in parsed_connections
                            ],
                        )

    def test_approval_requests_bind_changes_without_source_snapshots(self) -> None:
        approval = next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        self.assertNotIn("cards", approval["diff"])
        self.assertNotIn("source_changes", approval["diff"])
        self.assertEqual(
            {
                "diff_digest",
                "confirmed_change_ids",
                "confirmed_authority_change_ids",
                "confirmed_visibility_change_ids",
            },
            set(approval["diff"]),
        )


if __name__ == "__main__":
    unittest.main()
