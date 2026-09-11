from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
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
            "ownership",
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


def _without_connection_lines(source_text: str) -> str:
    parsed, errors = parse_connections(
        source_text,
        source_id="synthetic-source",
        path=Path("synthetic.md"),
    )
    if errors:
        return ""
    connection_lines = {item.line for item in parsed}
    return "\n".join(
        line
        for line_number, line in enumerate(source_text.splitlines(), start=1)
        if line_number not in connection_lines
    )


def _adapter_record_vocabulary_failure(
    record: dict,
    adapter_definition: dict,
    path: str,
) -> str | None:
    record_type = record.get("record_type")
    if record_type not in adapter_definition.get("record_types", []):
        return f"{path}.record_type"
    definition = adapter_definition.get("record_definitions", {}).get(record_type)
    if not isinstance(definition, dict):
        return f"{path}.record_type"
    allowed_fields = set(definition.get("fields", []))
    for index, field in enumerate(record.get("fields", [])):
        if field.get("field_id") not in allowed_fields:
            return f"{path}.fields.{index}.field_id"
    allowed_sections = {section.get("id") for section in definition.get("sections", [])}
    for index, section in enumerate(record.get("sections", [])):
        if section.get("section_id") not in allowed_sections:
            return f"{path}.sections.{index}.section_id"
    metadata = {
        "id": record.get("record_id"),
        "type": record_type,
        "status": record.get("status"),
        "ownership": record.get("ownership"),
        "name": record.get("displayed_name"),
        "visibility": record.get("visibility", {}).get("audience"),
        "warden_only": str(record.get("visibility", {}).get("warden_only")).lower(),
    }
    metadata.update(
        {
            field["field_id"]: "" if field["value"] is None else str(field["value"])
            for field in record.get("fields", [])
        }
    )
    for field in definition.get("required_fields", []):
        if field not in metadata:
            return f"{path}.{field}"
    for field in definition.get("nonempty_fields", []):
        if not metadata.get(field, "").strip():
            return f"{path}.{field}"
    for field, required_value in definition.get("required_values", {}).items():
        expected = str(required_value).lower() if isinstance(required_value, bool) else str(required_value)
        if metadata.get(field) != expected:
            return f"{path}.{field}"
    forbidden_headings = {heading.casefold() for heading in definition.get("forbidden_headings", [])}
    for index, section in enumerate(record.get("sections", [])):
        if any(
            heading.casefold() in forbidden_headings
            for heading in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", section.get("body", ""))
        ):
            return f"{path}.sections.{index}.body"
    relationships = set(adapter_definition.get("relationships", []))
    connection_states = set(adapter_definition.get("connection_states", []))
    for index, connection in enumerate(record.get("connections", [])):
        if connection.get("relationship") not in relationships:
            return f"{path}.connections.{index}.relationship"
        if connection.get("state") not in connection_states:
            return f"{path}.connections.{index}.state"
    return None


def _adapter_connection_vocabulary_failure(
    connection: dict,
    adapter_definition: dict,
    path: str,
) -> str | None:
    if connection.get("relationship") not in set(adapter_definition.get("relationships", [])):
        return f"{path}.relationship"
    if connection.get("state") not in set(adapter_definition.get("connection_states", [])):
        return f"{path}.state"
    return None


def _adapter_diff_vocabulary_failure(diff: dict, adapter_definition: dict) -> str | None:
    for index, card in enumerate(diff.get("cards", [])):
        path = f"diff.cards.{index}"
        for side in ("before", "after"):
            value = card.get(side)
            if isinstance(value, dict) and "record_id" in value:
                failure = _adapter_record_vocabulary_failure(
                    value,
                    adapter_definition,
                    f"{path}.{side}",
                )
                if failure is not None:
                    return failure
        for key in ("connection", "before"):
            value = card.get(key)
            if isinstance(value, dict) and "relationship" in value and "state" in value:
                failure = _adapter_connection_vocabulary_failure(
                    value,
                    adapter_definition,
                    f"{path}.{key}",
                )
                if failure is not None:
                    return failure
    return None


def source_snapshots_match(diff: dict, *, adapter_definition: dict | None = None) -> bool:
    required_metadata = {
        "id",
        "type",
        "status",
        "ownership",
        "visibility",
        "warden_only",
    }
    for source in diff.get("source_changes", []):
        subject = source.get("subject_record_id")
        resolution_cards = [
            card
            for card in diff.get("cards", [])
            if card.get("kind") == "reference_resolution"
            and card.get("subject_record_id") == subject
        ]
        connection_lists = {}
        has_structured_card = False
        for side in ("before", "after"):
            source_text = source.get(f"{side}_source")
            if source_text is None:
                continue
            metadata = frontmatter(source_text)
            if not required_metadata.issubset(metadata) or metadata.get("id") != subject:
                return False
            record_definition = (
                (adapter_definition or {}).get("record_definitions", {}).get(metadata["type"])
            )
            name_required = adapter_definition is None or record_definition is None or "name" in record_definition.get("required_fields", [])
            if name_required and "name" not in metadata:
                return False
            if metadata["ownership"] != "campaign":
                return False
            displayed_name = metadata.get("name") or subject
            structured = next(
                (
                    card[side]
                    for card in diff.get("cards", [])
                    if card.get("subject_record_id") == subject
                    and isinstance(card.get(side), dict)
                    and "record_id" in card[side]
                ),
                None,
            )
            if structured is not None:
                has_structured_card = True
                expected_metadata_keys = set(required_metadata)
                expected_metadata_keys.update(field["field_id"] for field in structured["fields"])
                if "name" in metadata or name_required:
                    expected_metadata_keys.add("name")
                if set(metadata) != expected_metadata_keys:
                    return False
                if any(
                    (
                        structured["record_type"] != metadata["type"],
                        structured["ownership"] != metadata["ownership"],
                        structured["displayed_name"] != displayed_name,
                        structured["status"] != metadata["status"],
                        structured["visibility"]["audience"] != metadata["visibility"],
                        str(structured["visibility"]["warden_only"]).lower()
                        != metadata["warden_only"],
                    )
                ):
                    return False
                for field in structured["fields"]:
                    if str(field["value"]) != metadata.get(field["field_id"]):
                        return False
                for section in structured["sections"]:
                    actual = [
                        line
                        for _, line in _section_lines(source_text, section["section_id"])
                    ]
                    while actual and not actual[0].strip():
                        actual.pop(0)
                    while actual and not actual[-1].strip():
                        actual.pop()
                    if section["body"].splitlines() != actual:
                        return False
                expected_sections = [
                    section["section_id"].casefold() for section in structured["sections"]
                ] + ["connections"]
                actual_sections = [
                    line[3:].strip().casefold()
                    for line in source_text.splitlines()
                    if line.startswith("## ")
                ]
                if actual_sections != expected_sections:
                    return False
                top_level_headings = [
                    line[2:].strip()
                    for line in source_text.splitlines()
                    if line.startswith("# ")
                ]
                if len(top_level_headings) != 1:
                    return False
                if top_level_headings[0] != displayed_name:
                    return False
            else:
                if not resolution_cards:
                    return False
            parsed, errors = parse_connections(
                source_text,
                source_id=subject,
                path=Path("synthetic.md"),
            )
            if errors:
                return False
            connection_lists[side] = [
                (item.target_id, item.relationship, item.state, item.context)
                for item in parsed
            ]
            if structured is not None and connection_lists[side] != [
                (
                    connection["target_record_id"],
                    connection["relationship"],
                    connection["state"],
                    connection["context"],
                )
                for connection in structured["connections"]
            ]:
                return False

        if resolution_cards and not has_structured_card:
            before_source = source.get("before_source")
            after_source = source.get("after_source")
            if before_source is None or after_source is None:
                return False
            if _without_connection_lines(before_source) != _without_connection_lines(after_source):
                return False
            before_connections = connection_lists["before"]
            expected_slots = [{"connection": item, "resolved": False} for item in before_connections]
            for resolution in resolution_cards:
                reference = resolution["before"]
                affected = (
                    reference["target_record_id"],
                    reference["relationship"],
                    reference["state"],
                    reference["context"],
                )
                slot = next(
                    (
                        item
                        for item in expected_slots
                        if not item["resolved"] and item["connection"] == affected
                    ),
                    None,
                )
                if slot is None:
                    return False
                slot["resolved"] = True
                action = resolution["after"]["action"]
                if action == "remove_reference":
                    slot["connection"] = None
                elif action == "redirect":
                    slot["connection"] = (
                        resolution["after"]["replacement_target_record_id"],
                        reference["relationship"],
                        reference["state"],
                        reference["context"],
                    )
                elif action != "accept_unresolved":
                    return False
            expected_after = [
                item["connection"] for item in expected_slots if item["connection"] is not None
            ]
            if connection_lists["after"] != expected_after:
                return False
    return True


def evaluate_semantic_failure(fixture: dict) -> tuple[str, str] | None:
    """Return the first contract violation found in a negative fixture."""
    instance = fixture["instance"]
    operation = instance.get("operation_request", {})
    binding = instance.get("binding", {})
    candidate = instance.get("candidate")

    bound_revision = binding.get("base_revision", {})
    if not isinstance(bound_revision, dict):
        bound_revision = instance.get("base_revision", {})
    if isinstance(bound_revision, dict):
        bound_revision = bound_revision.get("revision_id")
    if (
        "expected_revision" in operation
        and
        bound_revision is not None
        and operation.get("expected_revision") != bound_revision
    ):
        return "unsafe_binding", "operation_request.expected_revision"

    if "expected_editor_workflow_version" in operation:
        for binding_source in (binding, instance):
            if (
                "expected_editor_workflow_version" in binding_source
                and operation["expected_editor_workflow_version"]
                != binding_source["expected_editor_workflow_version"]
            ):
                return "unsafe_binding", "operation_request.expected_editor_workflow_version"

    if operation.get("operation") == "editor_proposal_correct":
        prior_ref = instance.get("prior_proposal", {})
        if operation.get("subject_id") != prior_ref.get("proposal_id"):
            return "unsafe_binding", "operation_request.subject_id"
    elif operation.get("operation") in {
        "editor_proposal_approve",
        "editor_proposal_reject",
    }:
        proposal = instance.get("proposal", {})
        if operation.get("subject_id") != proposal.get("proposal_id"):
            return "unsafe_binding", "operation_request.subject_id"
    elif operation and operation.get("operation") in {"editor_record_create", "editor_record_edit", "editor_record_remove"} and operation.get("subject_id") != binding.get("record_id"):
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

    if (
        operation.get("operation") in {"editor_record_edit", "editor_proposal_correct"}
        and isinstance(candidate, dict)
        and context.get("current_record_type") is not None
        and candidate.get("record_type") != context["current_record_type"]
    ):
        return "invalid_record_type", "candidate.record_type"

    if isinstance(candidate, dict):
        if candidate.get("ownership") != "campaign":
            return "proposal_validation_failure", "candidate.ownership"
        adapter_definition = context.get("adapter_definition")
        if adapter_definition is not None:
            failure = _adapter_record_vocabulary_failure(
                candidate,
                adapter_definition,
                "candidate",
            )
            if failure is not None:
                return "proposal_validation_failure", failure
        expected_authority = {
            "canon": "canon",
            "revealed": "revealed",
        }.get(candidate.get("status"), "preparation")
        if candidate.get("authority") != expected_authority:
            return "invalid_authority_transition", "candidate.authority"

        available_ids = set(context.get("available_record_ids", []))
        for index, connection in enumerate(candidate.get("connections", [])):
            if "available_record_ids" in context and connection.get("target_record_id") not in available_ids:
                return "invalid_connections", f"candidate.connections.{index}.target_record_id"

    prior_proposal = context.get("prior_proposal")
    if prior_proposal and (
        instance.get("mutation_kind") != prior_proposal.get("mutation_kind")
        or binding.get("record_id") != prior_proposal.get("record_id")
    ):
        return "invalid_correction", "prior_proposal"

    if operation.get("operation") == "editor_proposal_correct" and instance.get("mutation_kind") == "remove":
        impact_binding = instance.get("impact_binding")
        if not isinstance(impact_binding, dict):
            return "proposal_validation_failure", "impact_binding"
        if instance.get("impact_digest") != impact_binding.get("impact_digest"):
            return "proposal_validation_failure", "impact_digest"
        if impact_binding.get("binding") != binding:
            return "proposal_validation_failure", "impact_binding.binding"
        current_impact_digest = context.get("current_removal_impact_digest")
        if current_impact_digest is not None and instance.get("impact_digest") != current_impact_digest:
            return "proposal_validation_failure", "impact_digest"

    impact = fixture.get("impact", {})
    required_reference_id = impact.get("required_reference_id")
    resolutions = {item.get("reference_id") for item in instance.get("resolutions", [])}
    if required_reference_id and not impact.get("permitted_unresolved") and required_reference_id not in resolutions:
        return "incomplete_removal_resolution", "resolutions"

    diff = instance.get("diff")
    if isinstance(diff, dict):
        adapter_definition = context.get("adapter_definition")
        if adapter_definition is not None:
            failure = _adapter_diff_vocabulary_failure(diff, adapter_definition)
            if failure is not None:
                return "proposal_validation_failure", failure
        if operation.get("operation") in {"editor_proposal_approve", "editor_proposal_reject"}:
            if (
                instance.get("diff_digest") is not None
                and operation.get("intent_digest") != instance.get("diff_digest")
            ):
                return "proposal_approval_conflict", "operation_request.intent_digest"
        if instance.get("contract_name") == "editor_proposal_view":
            card_subjects = {card.get("subject_record_id") for card in diff.get("cards", [])}
            source_subjects = [source.get("subject_record_id") for source in diff.get("source_changes", [])]
            if len(source_subjects) != len(set(source_subjects)) or set(source_subjects) != card_subjects:
                return "mutation_consistency", "diff.source_changes"
            if not source_snapshots_match(
                diff,
                adapter_definition=context.get("adapter_definition"),
            ):
                return "mutation_consistency", "diff.source_changes"
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

    if instance.get("contract_name") == "editor_removal_impact":
        record_connections = instance.get("record", {}).get("connections", [])
        outgoing_connections = instance.get("outgoing_connections", [])
        record_connection_ids = [item.get("connection_id") for item in record_connections]
        outgoing_connection_ids = [item.get("connection_id") for item in outgoing_connections]
        if (
            len(record_connection_ids) != len(set(record_connection_ids))
            or len(outgoing_connection_ids) != len(set(outgoing_connection_ids))
            or record_connections != outgoing_connections
        ):
            return "mutation_consistency", "outgoing_connections"

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

    def test_editor_type_source_and_intent_bindings_are_enforced(self) -> None:
        edit = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "edit_record_with_connections_request")
        )
        edit["candidate"]["record_type"] = "faction"
        self.assertEqual(
            ("invalid_record_type", "candidate.record_type"),
            evaluate_semantic_failure({
                "instance": edit,
                "semantic_context": {"current_record_type": "location"},
            }),
        )
        edit["candidate"]["ownership"] = "shared"
        self.assertEqual(
            ("proposal_validation_failure", "candidate.ownership"),
            evaluate_semantic_failure({"instance": edit}),
        )

        correction = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        correction["candidate"]["record_type"] = "faction"
        self.assertEqual(
            ("invalid_record_type", "candidate.record_type"),
            evaluate_semantic_failure({
                "instance": correction,
                "semantic_context": {"current_record_type": "location"},
            }),
        )

        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        removal["diff"]["source_changes"].pop()
        self.assertEqual(
            ("mutation_consistency", "diff.source_changes"),
            evaluate_semantic_failure({"instance": removal}),
        )

        approval = deepcopy(next(item["payload"] for item in self.examples if item["name"] == "approval_request"))
        approval["operation_request"]["intent_digest"] = "f" * 64
        self.assertEqual(
            ("proposal_approval_conflict", "operation_request.intent_digest"),
            evaluate_semantic_failure({"instance": approval}),
        )

    def test_adapter_vocabulary_binds_candidates_and_proposal_cards(self) -> None:
        adapter_definition = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")[
                "adapter_definition"
            ]
        )
        adapter_definition["relationships"].append("signals")
        adapter_definition["record_definitions"]["location"]["required_values"] = {}
        context = {"adapter_definition": adapter_definition}
        edit = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "edit_record_with_connections_request"
        )
        self.assertIsNone(
            evaluate_semantic_failure({"instance": edit, "semantic_context": context})
        )

        invalid_candidates = (
            ("record_type", "npc", "candidate.record_type"),
            ("fields", [{"field_id": "unknown", "value": "value"}], "candidate.fields.0.field_id"),
            ("sections", [{"section_id": "unknown", "body": "value"}], "candidate.sections.0.section_id"),
        )
        for field, value, path in invalid_candidates:
            with self.subTest(path=path):
                invalid = deepcopy(edit)
                invalid["candidate"][field] = value
                self.assertEqual(
                    ("proposal_validation_failure", path),
                    evaluate_semantic_failure({"instance": invalid, "semantic_context": context}),
                )
        for field, value, path in (
            ("relationship", "unknown", "candidate.connections.0.relationship"),
            ("state", "unknown", "candidate.connections.0.state"),
        ):
            with self.subTest(path=path):
                invalid = deepcopy(edit)
                invalid["candidate"]["connections"][0][field] = value
                self.assertEqual(
                    ("proposal_validation_failure", path),
                    evaluate_semantic_failure({"instance": invalid, "semantic_context": context}),
                )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["cards"][0]["before"]["record_type"] = "npc"
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.0.before.record_type"),
            evaluate_semantic_failure({"instance": proposal, "semantic_context": context}),
        )

    def test_adapter_record_definition_rules_are_enforced(self) -> None:
        adapter_definition = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")[
                "adapter_definition"
            ]
        )
        context = {"adapter_definition": adapter_definition}
        edit = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        self.assertEqual(
            ("proposal_validation_failure", "candidate.visibility"),
            evaluate_semantic_failure({"instance": edit, "semantic_context": context}),
        )

        valid = deepcopy(edit)
        valid["candidate"]["visibility"] = {"audience": "warden", "warden_only": True}
        definition = adapter_definition["record_definitions"]["location"]
        definition["required_fields"].append("date")
        invalid = deepcopy(valid)
        invalid["candidate"]["fields"] = []
        self.assertEqual(
            ("proposal_validation_failure", "candidate.date"),
            evaluate_semantic_failure({"instance": invalid, "semantic_context": context}),
        )

        definition["nonempty_fields"] = ["date"]
        invalid = deepcopy(valid)
        invalid["candidate"]["fields"][0]["value"] = ""
        self.assertEqual(
            ("proposal_validation_failure", "candidate.date"),
            evaluate_semantic_failure({"instance": invalid, "semantic_context": context}),
        )

        definition["forbidden_headings"] = ["Secrets"]
        invalid = deepcopy(valid)
        invalid["candidate"]["sections"][0]["body"] = "### Secrets\nHidden."
        self.assertEqual(
            ("proposal_validation_failure", "candidate.sections.0.body"),
            evaluate_semantic_failure({"instance": invalid, "semantic_context": context}),
        )

    def test_operation_revision_workflow_and_empty_target_set_are_bound(self) -> None:
        edit = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        edit["operation_request"]["expected_revision"] = "revision_11"
        self.assertEqual(
            ("unsafe_binding", "operation_request.expected_revision"),
            evaluate_semantic_failure({"instance": edit}),
        )

        edit = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        edit["operation_request"]["expected_editor_workflow_version"] = 8
        self.assertEqual(
            ("unsafe_binding", "operation_request.expected_editor_workflow_version"),
            evaluate_semantic_failure({"instance": edit}),
        )

        for name in ("approval_request", "rejection_request"):
            action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            action["operation_request"]["expected_editor_workflow_version"] = 7
            with self.subTest(action=f"{name}-workflow"):
                self.assertEqual(
                    ("unsafe_binding", "operation_request.expected_editor_workflow_version"),
                    evaluate_semantic_failure({"instance": action}),
                )

            action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            action["operation_request"]["subject_id"] = "another-proposal"
            with self.subTest(action=name):
                self.assertEqual(
                    ("unsafe_binding", "operation_request.subject_id"),
                    evaluate_semantic_failure({"instance": action}),
                )

        edit = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        self.assertEqual(
            ("invalid_connections", "candidate.connections.0.target_record_id"),
            evaluate_semantic_failure({
                "instance": edit,
                "semantic_context": {"available_record_ids": []},
            }),
        )

    def test_non_redirect_resolution_snapshots_preserve_their_declared_action(self) -> None:
        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        resolution_card = next(
            card for card in removal["diff"]["cards"] if card["kind"] == "reference_resolution"
        )
        source = next(
            source
            for source in removal["diff"]["source_changes"]
            if source["subject_record_id"] == resolution_card["subject_record_id"]
        )
        unrelated_connection = "- `signals` → [[record-other|Other]] (`current`) — The station signals elsewhere."
        second_removed_reference = "- `knows` → [[record-company|The Company]] (`former`) — The station knew the company."
        for side in ("before_source", "after_source"):
            source[side] = source[side].replace(
                "## Connections\n",
                f"## Connections\n\n{unrelated_connection}\n{second_removed_reference}\n",
            )
        self.assertTrue(source_snapshots_match(removal["diff"]))
        for action in ("remove_reference", "accept_unresolved"):
            with self.subTest(action=action):
                candidate = deepcopy(removal)
                card = next(
                    card
                    for card in candidate["diff"]["cards"]
                    if card["kind"] == "reference_resolution"
                )
                card["after"] = {
                    "reference_id": card["after"]["reference_id"],
                    "action": action,
                    "replacement_target_record_id": None,
                }
                card["resolution"] = deepcopy(card["after"])
                candidate_source = next(
                    item
                    for item in candidate["diff"]["source_changes"]
                    if item["subject_record_id"] == card["subject_record_id"]
                )
                if action == "remove_reference":
                    candidate_source["after_source"] = "\n".join(
                        line
                        for line in candidate_source["after_source"].splitlines()
                        if "[[record-ship|The Ship]]" not in line
                    ) + "\n"
                else:
                    candidate_source["after_source"] = candidate_source["after_source"].replace(
                        "[[record-ship|The Ship]]", "[[record-company|The Company]]"
                    )
                self.assertTrue(source_snapshots_match(candidate["diff"]))

        invalid = deepcopy(removal)
        invalid["diff"]["source_changes"][1]["after_source"] = invalid["diff"]["source_changes"][1]["after_source"].replace(
            "ownership: campaign", "ownership: shared"
        )
        self.assertFalse(source_snapshots_match(invalid["diff"]))

        invalid = deepcopy(removal)
        invalid["diff"]["source_changes"][1]["after_source"] = invalid["diff"]["source_changes"][1]["after_source"].replace(
            "## Summary\n\nThe station handles salvage contracts.",
            "## Summary\n\nAn unreviewed prose change.",
        )
        self.assertFalse(source_snapshots_match(invalid["diff"]))

    def test_source_snapshots_apply_all_resolution_cards_for_a_subject(self) -> None:
        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        resolution_card = next(
            card for card in removal["diff"]["cards"] if card["kind"] == "reference_resolution"
        )
        source = next(
            source
            for source in removal["diff"]["source_changes"]
            if source["subject_record_id"] == resolution_card["subject_record_id"]
        )
        source["before_source"] += (
            "\n- `knows` → [[record-company|The Company]] (`former`) — The station knew the company."
        )
        source["after_source"] += (
            "\n- `knows` → [[record-other|Other]] (`former`) — The station knew the company."
        )
        second = deepcopy(resolution_card)
        second["change_id"] = "change_resolve_station_company_again"
        second["before"] = {
            **second["before"],
            "reference_id": "reference_station_company_again",
            "connection_id": "connection_two",
            "relationship": "knows",
            "state": "former",
            "context": "The station knew the company.",
        }
        second["after"] = {
            "reference_id": "reference_station_company_again",
            "action": "redirect",
            "replacement_target_record_id": "record-other",
        }
        second["resolution"] = deepcopy(second["after"])
        removal["diff"]["cards"].append(second)
        self.assertTrue(source_snapshots_match(removal["diff"]))

    def test_source_snapshot_comparisons_preserve_blank_lines(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        source = proposal["diff"]["source_changes"][0]
        card = proposal["diff"]["cards"][0]
        card["before"]["sections"][0]["body"] = "First paragraph.\n\nSecond paragraph."
        card["after"]["sections"][0]["body"] = "First paragraph.\n\nSecond paragraph."
        source["before_source"] = source["before_source"].replace(
            "A quiet station.", "First paragraph.\n\nSecond paragraph."
        )
        source["after_source"] = source["after_source"].replace(
            "A station with a public dock.", "First paragraph.\n\nSecond paragraph."
        )
        self.assertTrue(source_snapshots_match(proposal["diff"]))

        resolution = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        source = next(
            source
            for source in resolution["diff"]["source_changes"]
            if source["subject_record_id"] == "record-station"
        )
        source["before_source"] = source["before_source"].replace(
            "The station handles salvage contracts.",
            "First paragraph.\n\nSecond paragraph.",
        )
        self.assertFalse(source_snapshots_match(resolution["diff"]))

    def test_removal_correction_rebinds_current_impact(self) -> None:
        correction = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        current_revision = {
            "revision_id": "revision_13",
            "ordinal": 13,
            "tree_digest": "c" * 64,
        }
        current_impact_digest = "d" * 64
        correction["mutation_kind"] = "remove"
        correction["candidate"] = None
        correction["binding"]["base_revision"] = current_revision
        correction["operation_request"]["expected_revision"] = current_revision["revision_id"]
        correction["impact_digest"] = current_impact_digest
        correction["impact_binding"] = {
            "binding": {
                **correction["binding"],
            },
            "impact_digest": current_impact_digest,
        }
        context = {
            "current_head_revision": current_revision["revision_id"],
            "current_removal_impact_digest": current_impact_digest,
        }
        self.assertIsNone(evaluate_semantic_failure({"instance": correction, "semantic_context": context}))

        correction["impact_binding"]["binding"]["expected_editor_workflow_version"] = 7
        self.assertEqual(
            ("proposal_validation_failure", "impact_binding.binding"),
            evaluate_semantic_failure({"instance": correction, "semantic_context": context}),
        )

        correction["impact_binding"]["binding"]["expected_editor_workflow_version"] = 8
        correction["impact_digest"] = "e" * 64
        correction["impact_binding"]["impact_digest"] = "e" * 64
        self.assertEqual(
            ("proposal_validation_failure", "impact_digest"),
            evaluate_semantic_failure({"instance": correction, "semantic_context": context}),
        )

    def test_removal_impact_outgoing_connections_are_exact_unique_projection(self) -> None:
        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        removal = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "removal_proposal_with_outgoing_connections"
        )
        connection = deepcopy(
            next(card for card in removal["diff"]["cards"] if card["kind"] == "connection_removed")["connection"]
        )
        impact["record"]["connections"] = [connection]
        impact["outgoing_connections"] = [deepcopy(connection)]
        self.assertIsNone(evaluate_semantic_failure({"instance": impact}))

        impact["outgoing_connections"] = []
        self.assertEqual(
            ("mutation_consistency", "outgoing_connections"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact["outgoing_connections"] = [deepcopy(connection), deepcopy(connection)]
        impact["record"]["connections"] = [deepcopy(connection), deepcopy(connection)]
        self.assertEqual(
            ("mutation_consistency", "outgoing_connections"),
            evaluate_semantic_failure({"instance": impact}),
        )

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
                    if payload.get("contract_name") in {
                        "editor_proposal_approval_request",
                        "editor_proposal_rejection_request",
                    }:
                        self.assertEqual(
                            payload["diff_digest"],
                            payload["operation_request"]["intent_digest"],
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
        record_route = next(
            item for item in self.routes["routes"] if item["id"] == "editor_record_read"
        )
        removal_route = next(
            item for item in self.routes["routes"] if item["id"] == "editor_removal_impact"
        )
        self.assertEqual(route["response"], context["contract_name"])
        self.assertEqual(context["viewed_revision"], context["head_revision"])
        self.assertEqual(
            {"snapshot_integrity_failure", "snapshot_lineage_failure"},
            set(record_route["error_status"]["409"]),
        )
        self.assertEqual(
            {"snapshot_integrity_failure", "snapshot_lineage_failure"},
            set(removal_route["error_status"]["409"]),
        )
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
        for route_id in ("editor_proposal_reject", "editor_proposal_approve"):
            route = next(item for item in self.routes["routes"] if item["id"] == route_id)
            with self.subTest(route=route_id):
                self.assertIn("proposal_approval_conflict", route["error_status"]["409"])
                self.assertNotIn("proposal_approval_conflict", route["error_status"]["422"])
                self.assertIn("proposal_validation_failure", route["error_status"]["422"])
        approval_route = next(
            item for item in self.routes["routes"] if item["id"] == "editor_proposal_approve"
        )
        self.assertIn("quarantine_failure", approval_route["error_status"]["409"])
        self.assertIn("publication_intent_failure", approval_route["error_status"]["503"])

    def test_corrections_use_mutation_candidates(self) -> None:
        correction = deepcopy(next(item["payload"] for item in self.examples_document["examples"] if item["name"] == "correction_request"))
        correction["candidate"]["status"] = "missing"
        self.assertTrue(list(self.validator.iter_errors(correction)))

    def test_digest_projections_name_every_source_field(self) -> None:
        projections = self.invariants["digest_projections"]
        expected_fields = {
            "record_content_digest": {"record_id", "record_type", "displayed_name", "ownership", "status", "authority", "visibility", "fields", "sections", "connections", "content_digest"},
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
                                "visibility",
                                "warden_only",
                            }.issubset(metadata)
                        )
                        self.assertEqual(subject, metadata["id"])
                        if metadata.get("name"):
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
                            self.assertEqual(structured["ownership"], metadata["ownership"])
                            self.assertEqual(
                                structured["displayed_name"],
                                metadata.get("name") or subject,
                            )
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
                            if side == "before":
                                expected_connections = [
                                    (
                                        reference["target_record_id"],
                                        reference["relationship"],
                                        reference["state"],
                                        reference["context"],
                                    )
                                ]
                            elif resolution["after"]["action"] == "redirect":
                                expected_connections = [
                                    (
                                        resolution["after"]["replacement_target_record_id"],
                                        reference["relationship"],
                                        reference["state"],
                                        reference["context"],
                                    )
                                ]
                            elif resolution["after"]["action"] == "accept_unresolved":
                                expected_connections = [
                                    (
                                        reference["target_record_id"],
                                        reference["relationship"],
                                        reference["state"],
                                        reference["context"],
                                    )
                                ]
                            else:
                                expected_connections = []
                        self.assertEqual(
                            expected_connections,
                            [
                                (item.target_id, item.relationship, item.state, item.context)
                                for item in parsed_connections
                            ],
                        )

    def test_source_snapshots_use_record_id_for_nameless_session_records(self) -> None:
        record_types = ("session", "session-prep", "debrief")
        adapter_definition = {
            "record_definitions": {
                record_type: {
                    "required_fields": [
                        "id",
                        "type",
                        "status",
                        "ownership",
                        "date",
                        "visibility",
                        "warden_only",
                    ]
                }
                for record_type in record_types
            }
        }
        for record_type in record_types:
            subject = f"record-{record_type.replace('-', '')}"
            source = "\n".join([
                "---",
                f"id: {subject}",
                f"type: {record_type}",
                "status: draft",
                "ownership: campaign",
                "date: 2187-04-03",
                "visibility: warden",
                "warden_only: true",
                "---",
                "",
                f"# {subject}",
                "",
                "## Summary",
                "",
                "A session record.",
                "",
                "## Connections",
                "",
            ])
            record = {
                "record_id": subject,
                "record_type": record_type,
                "displayed_name": subject,
                "ownership": "campaign",
                "status": "draft",
                "authority": "preparation",
                "visibility": {"audience": "warden", "warden_only": True},
                "fields": [{"field_id": "date", "value": "2187-04-03"}],
                "sections": [{"section_id": "summary", "body": "A session record."}],
                "connections": [],
                "content_digest": "a" * 64,
            }
            diff = {
                "cards": [{"subject_record_id": subject, "before": record, "after": record}],
                "source_changes": [{
                    "subject_record_id": subject,
                    "before_source": source,
                    "after_source": source,
                }],
            }
            with self.subTest(record_type=record_type):
                self.assertTrue(
                    source_snapshots_match(
                        diff,
                        adapter_definition=adapter_definition,
                    )
                )

        wrong_heading = deepcopy(diff)
        wrong_heading["source_changes"][0]["before_source"] = wrong_heading["source_changes"][0]["before_source"].replace(
            f"# {subject}", "# Completely Wrong"
        )
        self.assertFalse(
            source_snapshots_match(
                wrong_heading,
                adapter_definition=adapter_definition,
            )
        )

        named = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        named["diff"]["source_changes"][0]["before_source"] = named["diff"]["source_changes"][0]["before_source"].replace(
            "name: Synthetic Station\n", ""
        )
        self.assertFalse(
            source_snapshots_match(
                named["diff"],
                adapter_definition={
                    "record_definitions": {
                        "location": {
                            "required_fields": [
                                "id",
                                "type",
                                "status",
                                "name",
                                "visibility",
                                "warden_only",
                            ]
                        }
                    }
                },
            )
        )

        extra_metadata = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        extra_metadata["diff"]["source_changes"][0]["before_source"] = extra_metadata["diff"]["source_changes"][0]["before_source"].replace(
            "date: 2187-04-03\n---", "date: 2187-04-03\nextra: value\n---"
        )
        self.assertFalse(source_snapshots_match(extra_metadata["diff"]))

        extra_section = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        extra_section["diff"]["source_changes"][0]["before_source"] = extra_section["diff"]["source_changes"][0]["before_source"].replace(
            "## Connections\n", "## Extra\n\nUnreviewed content.\n\n## Connections\n"
        )
        self.assertFalse(source_snapshots_match(extra_section["diff"]))

        wrong_heading = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        wrong_heading["diff"]["source_changes"][0]["before_source"] = wrong_heading["diff"]["source_changes"][0]["before_source"].replace(
            "# Synthetic Station", "# Unrelated Title"
        )
        self.assertFalse(source_snapshots_match(wrong_heading["diff"]))

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
