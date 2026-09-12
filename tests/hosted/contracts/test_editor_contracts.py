from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import unittest

from jsonschema import Draft202012Validator

from warden_drydock.standalone import (
    VALID_STATUSES,
    _section_lines,
    frontmatter,
    parse_connections,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http" / "editor" / "v1"
SEMANTIC_INVARIANTS = json.loads(
    (CONTRACT_ROOT / "semantic-invariants.json").read_text(encoding="utf-8")
)
OPERATION_PAYLOAD_PROJECTION = SEMANTIC_INVARIANTS["digest_projections"][
    "operation_payload_digest"
]
DIGEST_PROJECTIONS = SEMANTIC_INVARIANTS["digest_projections"]


def _normalize_text_values(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, dict):
        return {
            _normalize_text_values(key): _normalize_text_values(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_normalize_text_values(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_normalize_text_values(child) for child in value)
    return value


def canonical_digest(value: object, *, ensure_ascii: bool = True) -> str:
    encoded = json.dumps(
        _normalize_text_values(value),
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
    return canonical_digest(
        projection,
        ensure_ascii=definition.get("json_ascii_escaping", True),
    )


def _record_document_paths(value: object, path: str = ""):
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
            yield path, value
            return
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            yield from _record_document_paths(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _record_document_paths(child, f"{path}.{index}")


def _authoritative_before_record(context: dict, record_id: object) -> dict | None:
    records = context.get("authoritative_before_records")
    if isinstance(records, dict):
        record = records.get(record_id)
        if isinstance(record, dict):
            return record
    elif isinstance(records, list):
        for record in records:
            if isinstance(record, dict) and record.get("record_id") == record_id:
                return record
    return None


def _record_binding_digest_failure(
    instance: dict,
    context: dict | None = None,
) -> tuple[str, str] | None:
    diff = instance.get("diff")
    if not isinstance(diff, dict):
        diff = {}
    record_cards = {
        card.get("subject_record_id"): card
        for card in diff.get("cards", [])
        if card.get("kind") in {"record_created", "record_updated", "record_removed"}
    }
    for card in diff.get("cards", []):
        if card.get("kind") != "reference_resolution":
            continue
        before = card.get("before")
        if isinstance(before, dict) and before.get("source_record_id"):
            record_cards.setdefault(before["source_record_id"], {"before": before})
    source_changes = {
        source.get("subject_record_id"): source
        for source in diff.get("source_changes", [])
    }
    resolution_cards = {}
    for card in diff.get("cards", []):
        if card.get("kind") == "reference_resolution":
            resolution_cards.setdefault(card.get("subject_record_id"), []).append(card)
    for index, binding in enumerate(instance.get("record_bindings", [])):
        card = record_cards.get(binding.get("record_id"))
        before = card.get("before") if isinstance(card, dict) else None
        expected_digest = before.get("content_digest") if isinstance(before, dict) else None
        if expected_digest is None:
            source = source_changes.get(binding.get("record_id"), {})
            source_text = source.get("before_source")
            if isinstance(source_text, str):
                expected_digest = _source_record_digest(
                    source_text,
                    resolution_cards.get(binding.get("record_id"), []),
                    _authoritative_before_record(
                        context or {},
                        binding.get("record_id"),
                    ),
                )
                if expected_digest is None:
                    return "idempotency_digest_conflict", f"record_bindings.{index}.record_digest"
        if expected_digest is not None and binding.get("record_digest") != expected_digest:
            return "idempotency_digest_conflict", f"record_bindings.{index}.record_digest"
    return None


def _source_record_digest(
    source_text: object,
    resolution_cards: list[dict],
    authoritative_before: dict | None = None,
) -> str | None:
    if not isinstance(source_text, str):
        return None
    metadata = frontmatter(source_text)
    if not {"id", "type", "ownership", "visibility", "warden_only"}.issubset(metadata):
        return None
    parsed, errors = parse_connections(
        source_text,
        source_id=metadata["id"],
        path=Path("synthetic.md"),
    )
    if errors:
        return None
    authoritative_connections = (
        authoritative_before.get("connections", [])
        if isinstance(authoritative_before, dict)
        else []
    )
    if not isinstance(authoritative_connections, list):
        authoritative_connections = []
    used_cards = set()
    used_authoritative_connections = set()
    connections = []
    for connection in parsed:
        match = None
        for index, card in enumerate(resolution_cards):
            if index in used_cards:
                continue
            reference = card.get("before", {})
            if (
                reference.get("target_record_id") == connection.target_id
                and reference.get("relationship") == connection.relationship
                and reference.get("state") == connection.state
                and reference.get("context") == connection.context
            ):
                match = reference
                used_cards.add(index)
                break
        if match is not None and authoritative_connections:
            for index, authoritative in enumerate(authoritative_connections):
                if index in used_authoritative_connections:
                    continue
                if (
                    authoritative.get("connection_id") == match.get("connection_id")
                    and authoritative.get("target_record_id") == connection.target_id
                    and authoritative.get("relationship") == connection.relationship
                    and authoritative.get("state") == connection.state
                    and authoritative.get("context") == connection.context
                ):
                    used_authoritative_connections.add(index)
                    break
        if match is None and authoritative_connections:
            for index, authoritative in enumerate(authoritative_connections):
                if index in used_authoritative_connections:
                    continue
                if (
                    authoritative.get("target_record_id") == connection.target_id
                    and authoritative.get("relationship") == connection.relationship
                    and authoritative.get("state") == connection.state
                    and authoritative.get("context") == connection.context
                ):
                    match = authoritative
                    used_authoritative_connections.add(index)
                    break
        if match is None:
            return None
        connections.append(
            {
                "connection_id": match.get("connection_id"),
                "target_record_id": connection.target_id,
                "relationship": connection.relationship,
                "state": connection.state,
                "context": connection.context,
            }
        )
    if len(used_cards) != len(resolution_cards):
        return None
    if authoritative_connections and len(used_authoritative_connections) != len(
        authoritative_connections
    ):
        return None

    metadata_keys = {"id", "type", "status", "ownership", "name", "visibility", "warden_only"}
    sections = []
    for line in source_text.splitlines():
        if not line.startswith("## "):
            continue
        section_id = line[3:].strip().casefold()
        if section_id.casefold() == "connections":
            continue
        body_lines = [line for _, line in _section_lines(source_text, section_id)]
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        sections.append({"section_id": section_id, "body": "\n".join(body_lines)})

    warden_only = metadata["warden_only"].casefold()
    if warden_only not in {"true", "false"}:
        return None
    raw_status = metadata.get("status")
    if raw_status is None:
        read_status = {"classification": "missing", "value": None}
    elif raw_status in VALID_STATUSES:
        read_status = raw_status
    else:
        read_status = {"classification": "unknown", "value": raw_status}
    record = {
        "record_id": metadata["id"],
        "record_type": metadata["type"],
        "displayed_name": metadata.get("name") or metadata["id"],
        "ownership": metadata["ownership"],
        "status": read_status,
        "authority": _authority_for_status(read_status),
        "visibility": {
            "audience": metadata["visibility"],
            "warden_only": warden_only == "true",
        },
        "fields": [
            {"field_id": key, "value": value}
            for key, value in metadata.items()
            if key not in metadata_keys
        ],
        "sections": sections,
        "connections": connections,
    }
    reconstructed_digest = record_content_digest(record)
    if (
        isinstance(authoritative_before, dict)
        and "content_digest" in authoritative_before
        and authoritative_before.get("content_digest") != reconstructed_digest
    ):
        return None
    return reconstructed_digest


def _visibility_scope(visibility: dict) -> set[str]:
    if visibility.get("warden_only"):
        return {"warden"}
    return {
        "warden": {"warden"},
        "players": {"players"},
        "shared": {"warden", "players"},
    }[visibility["audience"]]


def _expected_transitions(diff: dict) -> tuple[list[dict], list[dict]]:
    authority_changes = []
    visibility_changes = []
    for card in diff.get("cards", []):
        before = card.get("before")
        after = card.get("after")
        if not (
            isinstance(before, dict)
            and isinstance(after, dict)
            and "record_id" in before
            and "record_id" in after
        ):
            continue
        change_id = card.get("change_id")
        record_id = card.get("subject_record_id")
        if before.get("authority") != after.get("authority"):
            authority_changes.append(
                {
                    "change_id": change_id,
                    "record_id": record_id,
                    "from": before.get("authority"),
                    "to": after.get("authority"),
                    "explicit_in_diff": True,
                    "warden_approval_required": True,
                }
            )
        if before.get("visibility") != after.get("visibility"):
            visibility_changes.append(
                {
                    "change_id": change_id,
                    "record_id": record_id,
                    "before": before.get("visibility"),
                    "after": after.get("visibility"),
                    "audience_broadens": _visibility_scope(after["visibility"])
                    > _visibility_scope(before["visibility"]),
                    "explicit_in_diff": True,
                    "warden_approval_required": True,
                }
            )
    return authority_changes, visibility_changes


def _transition_completeness_failure(
    instance: dict,
    context: dict,
) -> tuple[str, str] | None:
    if instance.get("contract_name") == "editor_proposal_view":
        source = instance
    elif instance.get("contract_name") == "editor_proposal_approval_request":
        source = context.get("loaded_proposal")
        if isinstance(source, dict) and isinstance(source.get("payload"), dict):
            source = source["payload"]
    else:
        return None
    if not isinstance(source, dict) or not isinstance(source.get("diff"), dict):
        return None

    expected_authority, expected_visibility = _expected_transitions(source["diff"])
    if instance.get("contract_name") == "editor_proposal_view":
        if instance["diff"].get("authority_changes") != expected_authority:
            return "proposal_validation_failure", "diff.authority_changes"
        if instance["diff"].get("visibility_changes") != expected_visibility:
            return "proposal_validation_failure", "diff.visibility_changes"
        if instance.get("authority_outcome") != expected_authority:
            return "proposal_validation_failure", "authority_outcome"
        if instance.get("visibility_outcome") != expected_visibility:
            return "proposal_validation_failure", "visibility_outcome"
        core_proposal = instance.get("core_proposal", {}).get("proposal", {})
        if core_proposal.get("authority_change_ids") != [
            item["change_id"] for item in expected_authority
        ]:
            return "proposal_validation_failure", "core_proposal.proposal.authority_change_ids"
        if core_proposal.get("visibility_change_ids") != [
            item["change_id"] for item in expected_visibility
        ]:
            return "proposal_validation_failure", "core_proposal.proposal.visibility_change_ids"
        return None

    if instance.get("authority_outcome") != expected_authority:
        return "proposal_validation_failure", "authority_outcome"
    if instance.get("visibility_outcome") != expected_visibility:
        return "proposal_validation_failure", "visibility_outcome"
    for field, expected in (
        (
            "confirmed_authority_change_ids",
            [item["change_id"] for item in expected_authority],
        ),
        (
            "confirmed_visibility_change_ids",
            [item["change_id"] for item in expected_visibility],
        ),
    ):
        if instance.get(field) != expected:
            return "proposal_validation_failure", field
        if instance.get("diff", {}).get(field) != expected:
            return "proposal_validation_failure", f"diff.{field}"
    return None


def _declared_digest_failure(
    instance: dict,
    context: dict | None = None,
) -> tuple[str, str] | None:
    for path, record in _record_document_paths(instance):
        if record["content_digest"] != record_content_digest(record):
            return "idempotency_digest_conflict", f"{path}.content_digest"

    binding_failure = _record_binding_digest_failure(instance, context)
    if binding_failure is not None:
        return binding_failure

    if instance.get("contract_name") == "editor_removal_impact":
        if instance.get("impact_digest") != projection_digest(
            instance, DIGEST_PROJECTIONS["impact_digest"]
        ):
            return "idempotency_digest_conflict", "impact_digest"

    diff = instance.get("diff")
    if isinstance(diff, dict) and "cards" in diff and "diff_digest" in diff:
        if diff["diff_digest"] != projection_digest(
            diff, DIGEST_PROJECTIONS["diff_digest"]
        ):
            return "idempotency_digest_conflict", "diff.diff_digest"

    validation = instance.get("validation")
    if isinstance(validation, dict) and "validation_digest" in validation:
        if validation["validation_digest"] != projection_digest(
            validation, DIGEST_PROJECTIONS["validation_digest"]
        ):
            return "idempotency_digest_conflict", "validation.validation_digest"

    core = instance.get("core_proposal")
    core_proposal = core.get("proposal") if isinstance(core, dict) else None
    core_validation = core.get("validation") if isinstance(core, dict) else None
    if (
        isinstance(core_proposal, dict)
        and isinstance(diff, dict)
        and core_proposal.get("diff_digest") != diff.get("diff_digest")
    ):
        return "unsafe_binding", "core_proposal.proposal.diff_digest"
    if (
        isinstance(core_validation, dict)
        and isinstance(validation, dict)
        and core_validation.get("validation_digest") != validation.get("validation_digest")
    ):
        return "unsafe_binding", "core_proposal.validation.validation_digest"
    if instance.get("contract_name") == "editor_proposal_view":
        if instance.get("proposal_payload_digest") != projection_digest(
            instance, DIGEST_PROJECTIONS["proposal_payload_digest"]
        ):
            return "idempotency_digest_conflict", "proposal_payload_digest"

    core_identity_failure = _core_identity_failure(instance)
    if core_identity_failure is not None:
        return core_identity_failure

    for path, left, right in (
        ("impact_digest", instance.get("impact_digest"), diff.get("impact_digest") if isinstance(diff, dict) else None),
        (
            "impact_binding.impact_digest",
            instance.get("impact_digest"),
            instance.get("impact_binding", {}).get("impact_digest")
            if isinstance(instance.get("impact_binding"), dict)
            else None,
        ),
    ):
        if left is not None and right is not None and left != right:
            return "idempotency_digest_conflict", path
    return None


def _operation_payload_digest_failure(instance: dict) -> tuple[str, str] | None:
    operation = instance.get("operation_request")
    if not isinstance(operation, dict):
        return None
    if operation.get("payload_digest") != projection_digest(
        instance,
        OPERATION_PAYLOAD_PROJECTION,
    ):
        return "replay_mismatch", "operation_request.payload_digest"
    return None


def _refresh_operation_payload_digest(instance: dict) -> None:
    instance["operation_request"]["payload_digest"] = projection_digest(
        instance,
        OPERATION_PAYLOAD_PROJECTION,
    )


def _duplicate_ids(items: object, member_id: str | None, path: str) -> str | None:
    if not isinstance(items, list):
        return None
    seen = set()
    for index, item in enumerate(items):
        value = item if member_id is None else item.get(member_id)
        if value in seen:
            suffix = f".{member_id}" if member_id is not None else ""
            return f"{path}.{index}{suffix}"
        seen.add(value)
    return None


def _logical_id_failure(instance: dict, diff: dict | None = None) -> str | None:
    core = instance.get("core_proposal", {})
    core_proposal = core.get("proposal", {}) if isinstance(core, dict) else {}
    approval_binding = core.get("approval_binding") if isinstance(core, dict) else None
    if not isinstance(approval_binding, dict):
        approval_binding = {}
    for items, member_id, path in (
        (instance.get("record_bindings"), "record_id", "record_bindings"),
        (instance.get("incoming_references"), "reference_id", "incoming_references"),
        (instance.get("authority_outcome"), "change_id", "authority_outcome"),
        (instance.get("visibility_outcome"), "change_id", "visibility_outcome"),
        (core_proposal.get("changes"), "change_id", "core_proposal.proposal.changes"),
        (core_proposal.get("authority_change_ids"), None, "core_proposal.proposal.authority_change_ids"),
        (core_proposal.get("visibility_change_ids"), None, "core_proposal.proposal.visibility_change_ids"),
        (approval_binding.get("authority_change_ids"), None, "core_proposal.approval_binding.authority_change_ids"),
        (approval_binding.get("visibility_change_ids"), None, "core_proposal.approval_binding.visibility_change_ids"),
    ):
        failure = _duplicate_ids(items, member_id, path)
        if failure is not None:
            return failure

    if isinstance(diff, dict):
        for items, member_id, path in (
            (diff.get("cards"), "change_id", "diff.cards"),
            (diff.get("source_changes"), "change_id", "diff.source_changes"),
            (diff.get("authority_changes"), "change_id", "diff.authority_changes"),
            (diff.get("visibility_changes"), "change_id", "diff.visibility_changes"),
        ):
            failure = _duplicate_ids(items, member_id, path)
            if failure is not None:
                return failure
        resolution_references = [
            card.get("before")
            for card in diff.get("cards", [])
            if card.get("kind") == "reference_resolution"
        ]
        failure = _duplicate_ids(
            resolution_references,
            "reference_id",
            "diff.cards.reference_resolution.before",
        )
        if failure is not None:
            return failure

    for items, member_id, path in (
        (instance.get("confirmed_change_ids"), None, "confirmed_change_ids"),
        (instance.get("confirmed_authority_change_ids"), None, "confirmed_authority_change_ids"),
        (instance.get("confirmed_visibility_change_ids"), None, "confirmed_visibility_change_ids"),
    ):
        failure = _duplicate_ids(items, member_id, path)
        if failure is not None:
            return failure
    return None


def _reference_resolution_binding_failure(diff: dict) -> tuple[str, str] | None:
    for index, card in enumerate(diff.get("cards", [])):
        if card.get("kind") == "reference_resolution":
            before = card.get("before")
            if (
                isinstance(before, dict)
                and before.get("source_record_id") != card.get("subject_record_id")
            ):
                return "unsafe_binding", f"diff.cards.{index}.before.source_record_id"
        if (
            card.get("kind") == "reference_resolution"
            and card.get("resolution") != card.get("after")
        ):
            return "unsafe_binding", f"diff.cards.{index}.resolution"
    return None


def _removal_impact_reference_binding_failure(
    diff: dict,
    impact: object,
) -> tuple[str, str] | None:
    if not isinstance(impact, dict) or "incoming_references" not in impact:
        return None

    expected = {
        reference.get("reference_id"): reference
        for reference in impact.get("incoming_references", [])
        if isinstance(reference, dict) and reference.get("reference_id")
    }
    actual_ids = []
    for index, card in enumerate(diff.get("cards", [])):
        if card.get("kind") != "reference_resolution":
            continue
        before = card.get("before")
        reference_id = before.get("reference_id") if isinstance(before, dict) else None
        if reference_id not in expected or before != expected[reference_id]:
            return "unsafe_binding", f"diff.cards.{index}.before"
        actual_ids.append(reference_id)

    if len(actual_ids) != len(expected) or set(actual_ids) != set(expected):
        return "unsafe_binding", "diff.cards"
    return None


def _record_property_changes(before: dict, after: dict) -> list[dict]:
    changes = []
    for property_name in ("displayed_name", "status", "authority", "visibility"):
        before_present = property_name in before
        after_present = property_name in after
        if (
            before.get(property_name) != after.get(property_name)
            or before_present != after_present
        ):
            changes.append(
                {
                    "property": property_name,
                    "before": before.get(property_name),
                    "after": after.get(property_name),
                    "before_present": before_present,
                    "after_present": after_present,
                }
            )
    for collection, member_id, value_key in (
        ("fields", "field_id", "value"),
        ("sections", "section_id", "body"),
    ):
        missing = object()
        before_members = {
            item[member_id]: item[value_key] for item in before.get(collection, [])
        }
        after_members = {
            item[member_id]: item[value_key] for item in after.get(collection, [])
        }
        member_ids = list(before_members)
        member_ids.extend(
            member_id for member_id in after_members if member_id not in before_members
        )
        for member_id in member_ids:
            before_value = before_members.get(member_id, missing)
            after_value = after_members.get(member_id, missing)
            before_present = before_value is not missing
            after_present = after_value is not missing
            if before_value != after_value or before_present != after_present:
                changes.append(
                    {
                        "property": f"{collection}.{member_id}",
                        "before": None if before_value is missing else before_value,
                        "after": None if after_value is missing else after_value,
                        "before_present": before_present,
                        "after_present": after_present,
                    }
                )
    return changes


def _record_property_change_failure(diff: dict) -> tuple[str, str] | None:
    for index, card in enumerate(diff.get("cards", [])):
        if card.get("kind") != "record_updated":
            continue
        before = card.get("before")
        after = card.get("after")
        if isinstance(before, dict) and isinstance(after, dict):
            if card.get("property_changes") != _record_property_changes(before, after):
                return "mutation_consistency", f"diff.cards.{index}.property_changes"
    return None


def _record_authority_failure(diff: dict) -> tuple[str, str] | None:
    for index, card in enumerate(diff.get("cards", [])):
        for side in ("before", "after"):
            record = card.get(side)
            if (
                isinstance(record, dict)
                and "record_id" in record
                and record.get("authority") != _authority_for_status(record.get("status"))
            ):
                return "invalid_authority_transition", f"diff.cards.{index}.{side}.authority"
    return None


def _record_subject_failure(diff: dict) -> tuple[str, str] | None:
    for index, card in enumerate(diff.get("cards", [])):
        for side in ("before", "after"):
            record = card.get(side)
            if (
                isinstance(record, dict)
                and "record_id" in record
                and record.get("record_id") != card.get("subject_record_id")
            ):
                return "unsafe_binding", f"diff.cards.{index}.{side}.record_id"
    return None


def _resolution_set_failure(instance: dict, diff: dict) -> tuple[str, str] | None:
    if "resolutions" not in instance:
        return None
    expected = [
        card.get("after")
        for card in diff.get("cards", [])
        if card.get("kind") == "reference_resolution"
    ]
    if instance.get("resolutions") != expected:
        return "unsafe_binding", "resolutions"
    return None


def _removal_redirect_failure(
    diff: dict,
    context: dict,
) -> tuple[str, str] | None:
    removed_record_ids = {
        card.get("subject_record_id")
        for card in diff.get("cards", [])
        if card.get("kind") == "record_removed"
    }
    available_record_ids = set(context.get("available_record_ids", []))
    for index, card in enumerate(diff.get("cards", [])):
        if card.get("kind") != "reference_resolution":
            continue
        before = card.get("before", {})
        after = card.get("after", {})
        if after.get("action") != "redirect":
            continue
        target = after.get("replacement_target_record_id")
        if target in removed_record_ids:
            return "proposal_validation_failure", f"diff.cards.{index}.after.replacement_target_record_id"
        if target == before.get("target_record_id"):
            return "proposal_validation_failure", f"diff.cards.{index}.after.replacement_target_record_id"
        if "available_record_ids" in context and target not in available_record_ids:
            return "proposal_validation_failure", f"diff.cards.{index}.after.replacement_target_record_id"
    return None


def _removal_request_redirect_failure(
    instance: dict,
    impact: dict,
    context: dict,
) -> tuple[str, str] | None:
    operation = instance.get("operation_request", {})
    if not (
        operation.get("operation") == "editor_record_remove"
        or (
            operation.get("operation") == "editor_proposal_correct"
            and instance.get("mutation_kind") == "remove"
        )
    ):
        return None

    references = {}
    if isinstance(impact, dict):
        references.update(
            {
                reference.get("reference_id"): reference
                for reference in impact.get("incoming_references", [])
                if isinstance(reference, dict) and reference.get("reference_id")
            }
        )
    removal_impact = context.get("removal_impact")
    if isinstance(removal_impact, dict):
        references.update(
            {
                reference.get("reference_id"): reference
                for reference in removal_impact.get("incoming_references", [])
                if isinstance(reference, dict) and reference.get("reference_id")
            }
        )

    removed_record_id = instance.get("binding", {}).get("record_id")
    available_record_ids = set(context.get("available_record_ids", []))
    for index, resolution in enumerate(instance.get("resolutions", [])):
        if resolution.get("action") != "redirect":
            continue
        target = resolution.get("replacement_target_record_id")
        reference = references.get(resolution.get("reference_id"), {})
        if target == removed_record_id or target == reference.get("target_record_id"):
            return "proposal_validation_failure", f"resolutions.{index}.replacement_target_record_id"
        if "available_record_ids" in context and target not in available_record_ids:
            return "proposal_validation_failure", f"resolutions.{index}.replacement_target_record_id"
    return None


def _removal_resolution_set_failure(
    instance: dict,
    impact: dict,
) -> tuple[str, str] | None:
    if "resolutions" not in instance or not isinstance(impact, dict):
        return None
    expected_ids = {
        reference.get("reference_id")
        for reference in impact.get("incoming_references", [])
        if isinstance(reference, dict) and reference.get("reference_id")
    }
    required_reference_id = impact.get("required_reference_id")
    if required_reference_id:
        expected_ids.add(required_reference_id)
    if {
        resolution.get("reference_id") for resolution in instance.get("resolutions", [])
    } != expected_ids:
        return "unsafe_binding", "resolutions"
    return None


def _removal_reference_policy_failure(
    instance: dict,
    diff: dict | None = None,
) -> tuple[str, str] | None:
    if instance.get("contract_name") == "editor_removal_impact":
        for index, reference in enumerate(instance.get("incoming_references", [])):
            if reference.get("target_record_id") != instance.get("binding", {}).get("record_id"):
                return "proposal_validation_failure", f"incoming_references.{index}.target_record_id"
            if reference.get("resolution_required") is not True:
                return "proposal_validation_failure", f"incoming_references.{index}.resolution_required"
            if reference.get("permitted_unresolved") is not False:
                return "proposal_validation_failure", f"incoming_references.{index}.permitted_unresolved"
        return None

    if (
        instance.get("contract_name") == "editor_proposal_view"
        and instance.get("mutation_kind") == "remove"
        and isinstance(diff, dict)
    ):
        for index, card in enumerate(diff.get("cards", [])):
            if card.get("kind") != "reference_resolution":
                continue
            before = card.get("before")
            if not isinstance(before, dict):
                continue
            if before.get("resolution_required") is not True:
                return "proposal_validation_failure", f"diff.cards.{index}.before.resolution_required"
            if before.get("permitted_unresolved") is not False:
                return "proposal_validation_failure", f"diff.cards.{index}.before.permitted_unresolved"
    return None


def _record_type_transition_failure(diff: dict) -> tuple[str, str] | None:
    for index, card in enumerate(diff.get("cards", [])):
        if card.get("kind") != "record_updated":
            continue
        before = card.get("before")
        after = card.get("after")
        if (
            isinstance(before, dict)
            and isinstance(after, dict)
            and before.get("record_type") != after.get("record_type")
        ):
            return "proposal_validation_failure", f"diff.cards.{index}.after.record_type"
    return None


def _unresolved_reference_count_failure(diff: dict) -> tuple[str, str] | None:
    if "unresolved_reference_count" not in diff:
        return None
    expected = sum(
        card.get("after", {}).get("action") == "accept_unresolved"
        for card in diff.get("cards", [])
        if card.get("kind") == "reference_resolution"
        and isinstance(card.get("after"), dict)
    )
    if diff.get("unresolved_reference_count") != expected:
        return "mutation_consistency", "diff.unresolved_reference_count"
    return None


def _removal_impact_binding_failure(instance: dict) -> tuple[str, str] | None:
    if (
        instance.get("contract_name") == "editor_proposal_view"
        and instance.get("mutation_kind") == "remove"
    ):
        impact_binding = instance.get("impact_binding")
        if not isinstance(impact_binding, dict):
            return "proposal_validation_failure", "impact_binding"
        removed_card = next(
            (
                card
                for card in instance.get("diff", {}).get("cards", [])
                if card.get("kind") == "record_removed"
            ),
            None,
        )
        before = removed_card.get("before") if isinstance(removed_card, dict) else None
        binding = impact_binding.get("binding")
        if not isinstance(before, dict) or not isinstance(binding, dict):
            return None
        for key, expected in {
            "campaign_id": instance.get("campaign_id"),
            "base_revision": instance.get("base_revision"),
            "record_id": before.get("record_id"),
            "record_digest": before.get("content_digest"),
        }.items():
            if binding.get(key) != expected:
                return "proposal_validation_failure", f"impact_binding.binding.{key}"
        return None

    operation = instance.get("operation_request", {})
    if not (
        operation.get("operation") == "editor_record_remove"
        or (
            operation.get("operation") == "editor_proposal_correct"
            and instance.get("mutation_kind") == "remove"
        )
    ):
        return None
    impact_binding = instance.get("impact_binding")
    binding = instance.get("binding")
    if (
        isinstance(impact_binding, dict)
        and isinstance(binding, dict)
        and impact_binding.get("binding") != binding
    ):
        return "proposal_validation_failure", "impact_binding.binding"
    return None


def _removal_impact_digest_failure(
    instance: dict,
    impact: object,
) -> tuple[str, str] | None:
    if (
        not isinstance(impact, dict)
        or "impact_digest" not in impact
        or instance.get("mutation_kind") != "remove"
    ):
        return None
    authoritative_digest = projection_digest(
        impact,
        DIGEST_PROJECTIONS["impact_digest"],
    )
    for key in ("impact_digest",):
        if key in instance and instance.get(key) != authoritative_digest:
            return "proposal_validation_failure", key
    impact_binding = instance.get("impact_binding")
    if (
        isinstance(impact_binding, dict)
        and impact_binding.get("impact_digest") != authoritative_digest
    ):
        return "proposal_validation_failure", "impact_binding.impact_digest"
    diff = instance.get("diff")
    if isinstance(diff, dict) and "impact_digest" in diff:
        if diff.get("impact_digest") != authoritative_digest:
            return "proposal_validation_failure", "diff.impact_digest"
    return None


def _resolution_policy_failure(
    instance: dict,
    impact: dict,
    diff: dict | None = None,
) -> tuple[str, str] | None:
    permitted_by_reference = {}
    if isinstance(impact, dict):
        required_reference_id = impact.get("required_reference_id")
        if required_reference_id:
            permitted_by_reference[required_reference_id] = impact.get(
                "permitted_unresolved"
            )
        for reference in impact.get("incoming_references", []):
            if isinstance(reference, dict) and reference.get("reference_id"):
                permitted_by_reference[reference["reference_id"]] = reference.get(
                    "permitted_unresolved"
                )
    if isinstance(diff, dict):
        for card in diff.get("cards", []):
            if card.get("kind") != "reference_resolution":
                continue
            before = card.get("before")
            if isinstance(before, dict) and before.get("reference_id"):
                permitted_by_reference[before["reference_id"]] = before.get(
                    "permitted_unresolved"
                )

    for index, resolution in enumerate(instance.get("resolutions", [])):
        if (
            resolution.get("action") == "accept_unresolved"
            and permitted_by_reference.get(resolution.get("reference_id")) is not True
        ):
            return "incomplete_removal_resolution", f"resolutions.{index}.action"
    return None


def _prior_proposal_identity_failure(
    instance: dict,
    context: dict,
) -> tuple[str, str] | None:
    loaded = context.get("prior_proposal")
    if not isinstance(loaded, dict):
        loaded = context.get("loaded_proposal")
    if not isinstance(loaded, dict):
        return None
    if isinstance(loaded.get("payload"), dict):
        loaded = loaded["payload"]
    reference = loaded.get("proposal")
    if not isinstance(reference, dict):
        reference = loaded
    requested = instance.get("prior_proposal")
    if not isinstance(requested, dict):
        return None
    for key in ("proposal_id", "proposal_version"):
        if key in reference and requested.get(key) != reference.get(key):
            return "invalid_correction", "prior_proposal"
    return None


def _proposal_subject_record_id(proposal: object) -> object:
    if not isinstance(proposal, dict):
        return None
    if isinstance(proposal.get("payload"), dict):
        proposal = proposal["payload"]
    if proposal.get("record_id") is not None:
        return proposal["record_id"]
    removed_ids = {
        card.get("subject_record_id")
        for card in proposal.get("diff", {}).get("cards", [])
        if isinstance(card, dict)
        and card.get("kind") == "record_removed"
        and card.get("subject_record_id")
    }
    if len(removed_ids) == 1:
        return next(iter(removed_ids))
    binding_ids = {
        binding.get("record_id")
        for binding in proposal.get("record_bindings", [])
        if isinstance(binding, dict) and binding.get("record_id")
    }
    if len(binding_ids) == 1:
        return next(iter(binding_ids))
    card_ids = {
        card.get("subject_record_id")
        for card in proposal.get("diff", {}).get("cards", [])
        if isinstance(card, dict) and card.get("subject_record_id")
    }
    if len(card_ids) == 1:
        return next(iter(card_ids))
    return None


def _correction_reference_failure(instance: dict) -> tuple[str, str] | None:
    correction_of = instance.get("correction_of")
    if not isinstance(correction_of, dict):
        return None
    if correction_of.get("proposal_id") != instance.get("proposal_id"):
        return "unsafe_binding", "correction_of.proposal_id"
    if instance.get("proposal_version") <= correction_of.get("proposal_version"):
        return "unsafe_binding", "proposal_version"
    return None


def _record_binding_failure(instance: dict, diff: dict) -> tuple[str, str] | None:
    expected_ids = []
    for card in diff.get("cards", []):
        subject = card.get("subject_record_id")
        if subject not in expected_ids:
            expected_ids.append(subject)
    bindings = instance.get("record_bindings", [])
    binding_by_id = {binding.get("record_id"): binding for binding in bindings}
    if set(binding_by_id) != set(expected_ids):
        return "unsafe_binding", "record_bindings"

    expected_values = {
        "campaign_id": instance.get("campaign_id"),
        "base_revision": instance.get("base_revision"),
        "expected_editor_workflow_version": instance.get("editor_workflow_version"),
    }
    for record_id in expected_ids:
        binding = binding_by_id[record_id]
        for key, value in expected_values.items():
            if binding.get(key) != value:
                index = next(
                    index
                    for index, item in enumerate(bindings)
                    if item.get("record_id") == record_id
                )
                return "unsafe_binding", f"record_bindings.{index}.{key}"
    return None


def _backlink(source_record_id: object, target_record_id: object, connection_id: object, effect: str) -> dict:
    return {
        "source_record_id": source_record_id,
        "target_record_id": target_record_id,
        "connection_id": connection_id,
        "effect": effect,
    }


def _expected_backlinks(card: dict) -> list[dict]:
    kind = card.get("kind")
    connection = card.get("connection")
    if kind in {"connection_added", "connection_removed"} and isinstance(connection, dict):
        return [
            _backlink(
                card.get("subject_record_id"),
                connection.get("target_record_id"),
                connection.get("connection_id"),
                "added" if kind == "connection_added" else "removed",
            )
        ]
    if kind == "connection_updated" and isinstance(connection, dict):
        before = connection.get("before", {})
        after = connection.get("after", {})
        return [
            _backlink(
                card.get("subject_record_id"),
                before.get("target_record_id"),
                before.get("connection_id"),
                "removed",
            ),
            _backlink(
                card.get("subject_record_id"),
                after.get("target_record_id"),
                after.get("connection_id"),
                "added",
            ),
        ]
    if kind == "reference_resolution":
        before = card.get("before", {})
        after = card.get("after", {})
        if after.get("action") == "redirect":
            return [
                _backlink(
                    before.get("source_record_id", card.get("subject_record_id")),
                    after.get("replacement_target_record_id"),
                    before.get("connection_id"),
                    "updated",
                )
            ]
        if after.get("action") == "remove_reference":
            return [
                _backlink(
                    before.get("source_record_id", card.get("subject_record_id")),
                    before.get("target_record_id"),
                    before.get("connection_id"),
                    "removed",
                )
            ]
    return []


def _backlink_binding_failure(diff: dict) -> tuple[str, str] | None:
    for index, card in enumerate(diff.get("cards", [])):
        if card.get("derived_backlinks") != _expected_backlinks(card):
            return "unsafe_binding", f"diff.cards.{index}.derived_backlinks"
    return None


def _core_change_content_digest(card: dict, record_card: dict | None) -> str | None:
    if card.get("kind") in {"record_created", "record_updated", "record_removed"}:
        record = card.get("after") or card.get("before")
        return record.get("content_digest") if isinstance(record, dict) else None
    if card.get("kind") in {
        "connection_added",
        "connection_updated",
        "connection_removed",
    }:
        if record_card is not None and record_card.get("kind") != "record_removed":
            record = record_card.get("after") or record_card.get("before")
            if isinstance(record, dict):
                return record.get("content_digest")
        return canonical_digest(card.get("connection"))
    if card.get("kind") == "reference_resolution":
        return canonical_digest(card.get("after"))
    return None


def _core_identity_failure(instance: dict) -> tuple[str, str] | None:
    core = instance.get("core_proposal", {})
    proposal = core.get("proposal") if isinstance(core, dict) else None
    if not isinstance(proposal, dict):
        return None
    for core_key, outer_key in (
        ("proposal_id", "proposal_id"),
        ("proposal_version", "proposal_version"),
        ("campaign_id", "campaign_id"),
        ("base_revision", "base_revision"),
        ("source_revision", "source_revision"),
        ("expected_campaign_head", "expected_campaign_head"),
        ("expected_editor_workflow_version", "editor_workflow_version"),
        ("status", "proposal_status"),
    ):
        if outer_key not in instance:
            continue
        outer_value = instance[outer_key]
        if isinstance(outer_value, dict):
            outer_value = outer_value.get("revision_id")
        if proposal.get(core_key) != outer_value:
            return "unsafe_binding", f"core_proposal.proposal.{core_key}"

    if "correction_of" in instance:
        correction_of = instance.get("correction_of")
        correction_of_version = (
            correction_of.get("proposal_version")
            if isinstance(correction_of, dict)
            else None
        )
        if proposal.get("correction_of_version") != correction_of_version:
            return "unsafe_binding", "core_proposal.proposal.correction_of_version"

    validation = core.get("validation") if isinstance(core, dict) else None
    outer_validation = instance.get("validation")
    if isinstance(validation, dict) and isinstance(outer_validation, dict):
        for key in ("status", "validation_digest", "error_count"):
            if validation.get(key) != outer_validation.get(key):
                return "unsafe_binding", f"core_proposal.validation.{key}"
    return None


def _core_approval_binding_failure(instance: dict) -> tuple[str, str] | None:
    core = instance.get("core_proposal")
    if not isinstance(core, dict):
        return None
    proposal = core.get("proposal")
    validation = core.get("validation")
    if not isinstance(proposal, dict) or not isinstance(validation, dict):
        return None

    approval_binding = core.get("approval_binding")
    approval_state = proposal.get("status") in {"approving", "approved"}
    if approval_state != isinstance(approval_binding, dict):
        return "proposal_approval_conflict", "core_proposal.approval_binding"
    if not isinstance(approval_binding, dict):
        return None

    for key in (
        "proposal_id",
        "proposal_version",
        "diff_digest",
        "base_revision",
        "source_revision",
        "expected_campaign_head",
        "expected_editor_workflow_version",
        "authority_change_ids",
        "visibility_change_ids",
    ):
        if approval_binding.get(key) != proposal.get(key):
            return "proposal_approval_conflict", f"core_proposal.approval_binding.{key}"
    for key, proposal_key in (
        ("validation_status", "status"),
        ("validation_digest", "validation_digest"),
    ):
        if approval_binding.get(key) != validation.get(proposal_key):
            return "proposal_approval_conflict", f"core_proposal.approval_binding.{key}"
    if approval_binding.get("warden_confirmed") is not True:
        return "proposal_approval_conflict", "core_proposal.approval_binding.warden_confirmed"
    return None


def _core_change_failure(instance: dict, diff: dict) -> tuple[str, str] | None:
    core = instance.get("core_proposal", {})
    proposal = core.get("proposal", {}) if isinstance(core, dict) else {}
    changes = proposal.get("changes") if isinstance(proposal, dict) else None
    if not isinstance(changes, list):
        return None

    record_cards = {
        card.get("subject_record_id"): card
        for card in diff.get("cards", [])
        if card.get("kind") in {"record_created", "record_updated", "record_removed"}
    }
    source_changes = {
        source.get("subject_record_id"): source
        for source in diff.get("source_changes", [])
    }
    expected = []
    for card in diff.get("cards", []):
        record_card = record_cards.get(card.get("subject_record_id"))
        if card.get("kind") in {"record_created", "record_updated", "record_removed"}:
            before = card.get("before")
            after = card.get("after")
            from_authority = before.get("authority") if isinstance(before, dict) else "absent"
            to_authority = after.get("authority") if isinstance(after, dict) else "absent"
            change_type = {
                "record_created": "add",
                "record_removed": "remove",
                "record_updated": "update",
            }[card["kind"]]
        else:
            source = source_changes.get(card.get("subject_record_id"), {})
            before_source = source.get("before_source")
            after_source = source.get("after_source")
            before_metadata = frontmatter(before_source) if before_source else {}
            after_metadata = frontmatter(after_source) if after_source else {}
            if record_card is not None:
                record_before = record_card.get("before")
                record_after = record_card.get("after")
                if record_card.get("kind") == "record_removed":
                    from_authority = record_before.get("authority")
                    to_authority = from_authority
                else:
                    from_authority = (
                        record_before.get("authority")
                        if isinstance(record_before, dict)
                        else record_after.get("authority")
                    )
                    to_authority = (
                        record_after.get("authority")
                        if isinstance(record_after, dict)
                        else record_before.get("authority")
                    )
            else:
                from_authority = _authority_for_status(before_metadata.get("status"))
                to_authority = _authority_for_status(after_metadata.get("status"))
            change_type = "update"
        expected.append(
            {
                "change_id": card.get("change_id"),
                "subject_id": card.get("subject_record_id"),
                "change_type": change_type,
                "from_authority": from_authority,
                "to_authority": to_authority,
                "content_digest": _core_change_content_digest(card, record_card),
            }
        )

    if len(changes) != len(expected):
        return "unsafe_binding", "core_proposal.proposal.changes"
    for index, (actual, expected_change) in enumerate(zip(changes, expected)):
        for key, value in expected_change.items():
            if actual.get(key) != value:
                return "unsafe_binding", f"core_proposal.proposal.changes.{index}.{key}"
    return None


def _connection_delta_failure(diff: dict) -> tuple[str, str] | None:
    expected_connections = {}
    for record_card in diff.get("cards", []):
        if record_card.get("kind") not in {
            "record_created",
            "record_updated",
            "record_removed",
        }:
            continue
        before = record_card.get("before")
        after = record_card.get("after")
        before_connections = {
            connection["connection_id"]: connection
            for connection in (before or {}).get("connections", [])
        }
        after_connections = {
            connection["connection_id"]: connection
            for connection in (after or {}).get("connections", [])
        }
        for connection_id in before_connections.keys() - after_connections.keys():
            expected_connections[
                (record_card["subject_record_id"], "connection_removed", connection_id)
            ] = before_connections[connection_id]
        for connection_id in after_connections.keys() - before_connections.keys():
            expected_connections[
                (record_card["subject_record_id"], "connection_added", connection_id)
            ] = after_connections[connection_id]
        for connection_id in before_connections.keys() & after_connections.keys():
            if before_connections[connection_id] != after_connections[connection_id]:
                expected_connections[
                    (record_card["subject_record_id"], "connection_updated", connection_id)
                ] = {
                    "before": before_connections[connection_id],
                    "after": after_connections[connection_id],
                }

    actual_connections = {}
    for card in diff.get("cards", []):
        if card.get("kind") not in {
            "connection_added",
            "connection_updated",
            "connection_removed",
        }:
            continue
        connection = card.get("connection", {})
        if card["kind"] == "connection_updated":
            connection_id = connection.get("before", {}).get("connection_id")
        else:
            connection_id = connection.get("connection_id")
        actual_connections[
            (card["subject_record_id"], card["kind"], connection_id)
        ] = connection
    connection_card_count = sum(
        card.get("kind") in {
            "connection_added",
            "connection_updated",
            "connection_removed",
        }
        for card in diff.get("cards", [])
    )
    if (
        len(actual_connections) != connection_card_count
        or set(expected_connections) != set(actual_connections)
    ):
        return "mutation_consistency", "diff.cards.connection_delta"
    if any(
        expected_connections[key] != actual_connections[key]
        for key in expected_connections
    ):
        return "mutation_consistency", "diff.cards.connection"
    return None


def _loaded_proposal_action_failure(instance: dict, context: dict) -> tuple[str, str] | None:
    loaded = context.get("loaded_proposal")
    if not isinstance(loaded, dict):
        return None
    if isinstance(loaded.get("payload"), dict):
        loaded = loaded["payload"]

    if loaded.get("contract_name") == "editor_proposal_view":
        proposal_validation_failure = _proposal_validation_gate_failure(loaded)
        if proposal_validation_failure is not None:
            return proposal_validation_failure

    approval_binding_failure = _core_approval_binding_failure(loaded)
    if approval_binding_failure is not None:
        return approval_binding_failure
    removal_binding_failure = _removal_impact_binding_failure(loaded)
    if removal_binding_failure is not None:
        return removal_binding_failure
    impact_digest_failure = _removal_impact_digest_failure(
        loaded,
        context.get("removal_impact"),
    )
    if impact_digest_failure is not None:
        return impact_digest_failure
    impact_digest_failure = _removal_impact_digest_failure(
        instance,
        context.get("removal_impact"),
    )
    if impact_digest_failure is not None:
        return impact_digest_failure

    if isinstance(loaded.get("diff"), dict):
        resolution_failure = _reference_resolution_binding_failure(loaded["diff"])
        if resolution_failure is not None:
            return resolution_failure
        impact_reference_failure = _removal_impact_reference_binding_failure(
            loaded["diff"],
            context.get("removal_impact"),
        )
        if impact_reference_failure is not None:
            return impact_reference_failure
        reference_policy_failure = _removal_reference_policy_failure(
            loaded,
            loaded["diff"],
        )
        if reference_policy_failure is not None:
            return reference_policy_failure
        if "cards" in loaded["diff"]:
            if loaded.get("contract_name") == "editor_proposal_view":
                card_subjects = {
                    card.get("subject_record_id")
                    for card in loaded["diff"].get("cards", [])
                }
                source_subjects = [
                    source.get("subject_record_id")
                    for source in loaded["diff"].get("source_changes", [])
                ]
                if (
                    len(source_subjects) != len(set(source_subjects))
                    or set(source_subjects) != card_subjects
                ):
                    return "mutation_consistency", "diff.source_changes"
                if loaded["diff"].get("affected_record_count") != len(card_subjects):
                    return "mutation_consistency", "diff.affected_record_count"
                record_card_subjects = [
                    card.get("subject_record_id")
                    for card in loaded["diff"].get("cards", [])
                    if card.get("kind")
                    in {"record_created", "record_updated", "record_removed"}
                ]
                if len(record_card_subjects) != len(set(record_card_subjects)):
                    return "proposal_validation_failure", "diff.cards.record_mutation"
            for failure in (
                _record_property_change_failure(loaded["diff"]),
                _record_subject_failure(loaded["diff"]),
                _record_authority_failure(loaded["diff"]),
                _record_binding_digest_failure(loaded, context),
                _record_binding_failure(loaded, loaded["diff"]),
                _resolution_set_failure(loaded, loaded["diff"]),
                _removal_redirect_failure(loaded["diff"], context),
                None if source_snapshots_match(
                    loaded["diff"],
                    adapter_definition=context.get("adapter_definition"),
                ) else ("mutation_consistency", "diff.source_changes"),
                _connection_delta_failure(loaded["diff"]),
                _backlink_binding_failure(loaded["diff"]),
                _core_change_failure(loaded, loaded["diff"]),
            ):
                if failure is not None:
                    return failure
        adapter_definition = context.get("adapter_definition")
        if adapter_definition is not None:
            failure = _adapter_diff_vocabulary_failure(
                loaded["diff"],
                adapter_definition,
            )
            if failure is not None:
                return "proposal_validation_failure", failure
        record_type_failure = _record_type_transition_failure(loaded["diff"])
        if record_type_failure is not None:
            return record_type_failure
        if loaded.get("contract_name") == "editor_proposal_view":
            transition_failure = _transition_completeness_failure(loaded, context)
            if transition_failure is not None:
                return transition_failure

    if instance.get("operation_request", {}).get("operation") == "editor_proposal_approve":
        validation = loaded.get("validation")
        if isinstance(validation, dict):
            if validation.get("status") != "passed":
                return "proposal_validation_failure", "validation.status"
            if validation.get("error_count") != 0:
                return "proposal_validation_failure", "validation.error_count"
            if any(
                finding.get("severity") in {"error", "warning"}
                for finding in validation.get("findings", [])
                if isinstance(finding, dict)
            ):
                return "proposal_validation_failure", "validation.findings"

    expected = {}
    direct_action = "operation_request" in loaded or loaded.get("contract_name") in {
        "editor_proposal_approval_request",
        "editor_proposal_rejection_request",
    }
    direct_keys = (
        "proposal",
        "proposal_status",
        "mutation_kind",
        "source_revision",
        "base_revision",
        "expected_campaign_head",
        "expected_editor_workflow_version",
        "proposal_payload_digest",
        "diff_digest",
        "validation_status",
        "validation_digest",
        "record_bindings",
        "impact_digest",
        "impact_binding",
        "resolutions",
        "authority_outcome",
        "visibility_outcome",
    )
    if direct_action:
        direct_keys += (
            "diff",
            "affected_record_count",
            "confirmed_change_ids",
            "confirmed_authority_change_ids",
            "confirmed_visibility_change_ids",
        )
    for key in direct_keys:
        if key in loaded:
            expected[key] = loaded[key]

    if "proposal" not in expected and {
        "proposal_id",
        "proposal_version",
    }.issubset(loaded):
        expected["proposal"] = {
            "proposal_id": loaded["proposal_id"],
            "proposal_version": loaded["proposal_version"],
        }

    core = loaded.get("core_proposal", {})
    core_proposal = core.get("proposal", {}) if isinstance(core, dict) else {}
    if isinstance(core_proposal, dict):
        if "proposal_status" not in expected and "status" in core_proposal:
            expected["proposal_status"] = core_proposal["status"]
        if "proposal" not in expected and {
            "proposal_id",
            "proposal_version",
        }.issubset(core_proposal):
            expected["proposal"] = {
                "proposal_id": core_proposal["proposal_id"],
                "proposal_version": core_proposal["proposal_version"],
            }

    view_fields = {
        "editor_workflow_version": "expected_editor_workflow_version",
        "validation": "validation_status",
    }
    for loaded_key, action_key in view_fields.items():
        value = loaded.get(loaded_key)
        if loaded_key == "validation" and isinstance(value, dict):
            expected[action_key] = value.get("status")
            expected["validation_digest"] = value.get("validation_digest")
        elif action_key not in expected and loaded_key in loaded:
            expected[action_key] = value
    if isinstance(loaded.get("diff"), dict):
        loaded_diff = loaded["diff"]
        if "diff_digest" not in expected:
            expected["diff_digest"] = loaded_diff.get("diff_digest")
        if "affected_record_count" not in expected:
            expected["affected_record_count"] = loaded_diff.get("affected_record_count")
        if not direct_action and "diff" not in expected:
            expected["diff"] = {
                "diff_digest": loaded_diff.get("diff_digest"),
                "confirmed_change_ids": [
                    card.get("change_id") for card in loaded_diff.get("cards", [])
                ],
                "confirmed_authority_change_ids": [
                    change.get("change_id")
                    for change in loaded.get("authority_outcome", [])
                ],
                "confirmed_visibility_change_ids": [
                    change.get("change_id")
                    for change in loaded.get("visibility_outcome", [])
                ],
            }

    action_proposal = instance.get("proposal", {})
    expected_proposal = expected.get("proposal")
    if isinstance(action_proposal, dict) and isinstance(expected_proposal, dict):
        if action_proposal.get("proposal_id") != expected_proposal.get("proposal_id"):
            return "proposal_approval_conflict", "proposal.proposal_id"
        if action_proposal.get("proposal_version") != expected_proposal.get("proposal_version"):
            return "proposal_approval_conflict", "proposal.proposal_version"
    for key, expected_value in expected.items():
        if key == "proposal":
            continue
        if key in instance and instance[key] != expected_value:
            return "proposal_approval_conflict", key
    return None


def _record_member_id_failure(record: dict, path: str) -> str | None:
    for collection, member_id in (
        ("fields", "field_id"),
        ("sections", "section_id"),
        ("connections", "connection_id"),
    ):
        seen = set()
        for index, member in enumerate(record.get(collection, [])):
            value = member.get(member_id)
            if value in seen:
                return f"{path}.{collection}.{index}.{member_id}"
            seen.add(value)
    return None


def _authority_for_status(status: object) -> str:
    if isinstance(status, dict) and status.get("classification") == "known":
        status = status.get("value")
    if not isinstance(status, str):
        return "preparation"
    return {"canon": "canon", "revealed": "revealed"}.get(status, "preparation")


def _adapter_definition_failure(adapter_definition: object, path: str) -> str | None:
    if not isinstance(adapter_definition, dict):
        return None
    if set(adapter_definition.get("record_types", [])) != set(
        adapter_definition.get("record_definitions", {})
    ):
        return f"{path}.record_types"
    return None


def _adapter_definition_binding_failure(
    instance: dict,
    context: dict,
) -> tuple[str, str] | None:
    returned = instance.get("adapter_definition")
    bound = context.get("adapter_definition")
    if isinstance(returned, dict) and isinstance(bound, dict) and returned != bound:
        return "unsafe_binding", "adapter_definition"
    return None


def _proposal_validation_gate_failure(instance: dict) -> tuple[str, str] | None:
    if instance.get("contract_name") != "editor_proposal_view":
        return None
    core = instance.get("core_proposal")
    proposal = core.get("proposal") if isinstance(core, dict) else None
    if not isinstance(proposal, dict):
        return None

    expected_kind = {
        "create": "record_created",
        "edit": "record_updated",
        "remove": "record_removed",
    }.get(instance.get("mutation_kind"))
    if expected_kind is not None:
        primary_cards = [
            card
            for card in instance.get("diff", {}).get("cards", [])
            if card.get("kind")
            in {"record_created", "record_updated", "record_removed"}
        ]
        if len(primary_cards) != 1 or primary_cards[0].get("kind") != expected_kind:
            return "proposal_validation_failure", "diff.cards.record_mutation"

    proposal_status = proposal.get("status")
    publication = instance.get("publication")
    if isinstance(publication, dict):
        publication_status = publication.get("status")
        published_revision = publication.get("published_revision")
        if proposal_status in {"draft", "needs_review", "rejected", "conflict"}:
            if publication_status != "not_published":
                return "proposal_approval_conflict", "publication.status"
            if published_revision is not None:
                return "proposal_approval_conflict", "publication.published_revision"
        elif proposal_status == "approving":
            if publication_status not in {"not_published", "quarantined"}:
                return "proposal_approval_conflict", "publication.status"
            if published_revision is not None:
                return "proposal_approval_conflict", "publication.published_revision"
        elif proposal_status == "approved":
            if publication_status != "published":
                return "proposal_approval_conflict", "publication.status"
            if published_revision is None:
                return "proposal_approval_conflict", "publication.published_revision"
            base_revision = instance.get("base_revision")
            if not isinstance(base_revision, dict) or not isinstance(published_revision, dict):
                return "proposal_approval_conflict", "publication.published_revision"
            if published_revision.get("revision_id") == base_revision.get("revision_id"):
                return "proposal_approval_conflict", "publication.published_revision"
            if (
                not isinstance(base_revision.get("ordinal"), int)
                or not isinstance(published_revision.get("ordinal"), int)
                or published_revision["ordinal"] <= base_revision["ordinal"]
            ):
                return "proposal_approval_conflict", "publication.published_revision"

    if proposal_status not in {"needs_review", "approving", "approved"}:
        return None
    validation = instance.get("validation", {})
    if not isinstance(validation, dict) or validation.get("status") != "passed":
        return "proposal_validation_failure", "validation.status"
    if validation.get("error_count") != 0:
        return "proposal_validation_failure", "validation.error_count"
    if proposal_status in {"approving", "approved"} and any(
        finding.get("severity") in {"error", "warning"}
        for finding in validation.get("findings", [])
        if isinstance(finding, dict)
    ):
        return "proposal_validation_failure", "validation.findings"
    core_validation = core.get("validation") if isinstance(core, dict) else None
    if not isinstance(core_validation, dict) or core_validation.get("status") != "passed":
        return "proposal_validation_failure", "core_proposal.validation.status"
    if core_validation.get("error_count") != 0:
        return "proposal_validation_failure", "core_proposal.validation.error_count"
    return None


def _accepted_request(fixture: dict) -> dict | None:
    context = fixture.get("semantic_context", {})
    fallback = None
    for source in (
        context.get("accepted_request"),
        context.get("accepted_operation_request"),
        fixture.get("accepted_request"),
    ):
        if not isinstance(source, dict):
            continue
        if isinstance(source.get("payload"), dict):
            source = source["payload"]
        if "proposal" in source:
            return source
        fallback = fallback or source
    return fallback


def _stored_publication_revision(fixture: dict) -> tuple[bool, object]:
    context = fixture.get("semantic_context", {})
    receipt = context.get("stored_receipt") or fixture.get("stored_receipt")
    if not isinstance(receipt, dict):
        return False, None
    candidates = [receipt]
    for key in ("result", "result_payload", "response", "publication"):
        value = receipt.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            if isinstance(value.get("payload"), dict):
                candidates.append(value["payload"])
    for candidate in candidates:
        if "published_revision" in candidate:
            return True, candidate["published_revision"]
    return False, None


def _correction_result_binding_failure(
    instance: dict,
    accepted: dict,
) -> tuple[str, str] | None:
    accepted_mutation = accepted.get("mutation_kind")
    if accepted_mutation not in {"create", "edit", "remove"}:
        return None
    request_binding = accepted.get("binding")
    if not isinstance(request_binding, dict):
        return "unsafe_binding", "binding"
    if instance.get("mutation_kind") != accepted_mutation:
        return "unsafe_binding", "mutation_kind"
    for response_key, request_key in (
        ("campaign_id", "campaign_id"),
        ("source_revision", "base_revision"),
        ("base_revision", "base_revision"),
        ("expected_campaign_head", "base_revision"),
    ):
        expected = request_binding.get(request_key)
        if expected is not None and instance.get(response_key) != expected:
            return "unsafe_binding", response_key

    candidate = accepted.get("candidate")
    expected_record_id = request_binding.get("record_id")
    if expected_record_id is None and isinstance(candidate, dict):
        expected_record_id = candidate.get("record_id")
    record_cards = [
        (index, card)
        for index, card in enumerate(instance.get("diff", {}).get("cards", []))
        if card.get("kind")
        == {
            "create": "record_created",
            "edit": "record_updated",
            "remove": "record_removed",
        }[accepted_mutation]
    ]
    if len(record_cards) != 1:
        return "unsafe_binding", "diff.cards"
    card_index, record_card = record_cards[0]
    if _proposal_subject_record_id(instance) != expected_record_id:
        return "unsafe_binding", "diff.cards.subject_record_id"
    if accepted_mutation in {"create", "edit"}:
        if not isinstance(candidate, dict) or record_card.get("after") != candidate:
            return "unsafe_binding", f"diff.cards.{card_index}.after"
    if accepted_mutation == "edit":
        before = record_card.get("before")
        if (
            not isinstance(before, dict)
            or before.get("content_digest") != request_binding.get("record_digest")
        ):
            return "unsafe_binding", f"diff.cards.{card_index}.before.content_digest"
    elif accepted_mutation == "remove":
        before = record_card.get("before")
        if not isinstance(before, dict):
            return "unsafe_binding", f"diff.cards.{card_index}.before"
        for response_key, request_key in (
            ("record_id", "record_id"),
            ("content_digest", "record_digest"),
        ):
            if before.get(response_key) != request_binding.get(request_key):
                return "unsafe_binding", f"diff.cards.{card_index}.before.{response_key}"

    response_bindings = [
        (index, binding)
        for index, binding in enumerate(instance.get("record_bindings", []))
        if binding.get("record_id") == expected_record_id
    ]
    if len(response_bindings) != 1:
        return "unsafe_binding", "record_bindings"
    binding_index, response_binding = response_bindings[0]
    for key in (
        "campaign_id",
        "base_revision",
        "record_id",
        "record_digest",
        "expected_editor_workflow_version",
    ):
        expected = (
            instance.get("editor_workflow_version")
            if key == "expected_editor_workflow_version"
            else request_binding.get(key)
        )
        if key in request_binding and response_binding.get(key) != expected:
            return "unsafe_binding", f"record_bindings.{binding_index}.{key}"

    if accepted_mutation == "remove":
        expected_impact_binding = accepted.get("impact_binding")
        if isinstance(expected_impact_binding, dict):
            actual_impact_binding = instance.get("impact_binding")
            if not isinstance(actual_impact_binding, dict):
                return "unsafe_binding", "impact_binding"
            if actual_impact_binding != expected_impact_binding:
                return "unsafe_binding", "impact_binding"
            if instance.get("impact_digest") != expected_impact_binding.get("impact_digest"):
                return "unsafe_binding", "impact_digest"
        if "resolutions" in accepted and instance.get("resolutions") != accepted.get("resolutions"):
            return "unsafe_binding", "resolutions"
    return None


def _result_binding_failure(instance: dict, fixture: dict) -> tuple[str, str] | None:
    if instance.get("contract_name") not in {
        "editor_proposal_view",
        "editor_proposal_approval_result",
        "editor_proposal_rejection_result",
    }:
        return None

    accepted = _accepted_request(fixture)
    accepted_operation = accepted.get("operation_request", {}) if isinstance(accepted, dict) else {}
    if not isinstance(accepted_operation, dict):
        accepted_operation = {}
    expected_result = {
        "editor_proposal_approve": (
            "editor_proposal_approval_result",
            "published",
        ),
        "editor_proposal_reject": (
            "editor_proposal_rejection_result",
            "rejected",
        ),
    }.get(accepted_operation.get("operation"))
    if expected_result is not None:
        expected_contract, expected_outcome = expected_result
        if instance.get("contract_name") != expected_contract:
            return "unsafe_binding", "contract_name"
        if instance.get("outcome") != expected_outcome:
            return "unsafe_binding", "outcome"
    operation_to_mutation = {
        "editor_record_create": "create",
        "editor_record_edit": "edit",
        "editor_record_remove": "remove",
    }
    accepted_mutation = operation_to_mutation.get(accepted_operation.get("operation"))
    if (
        instance.get("contract_name") == "editor_proposal_view"
        and accepted_mutation is not None
        and isinstance(accepted, dict)
    ):
        request_binding = accepted.get("binding")
        if not isinstance(request_binding, dict):
            return "unsafe_binding", "binding"
        if instance.get("mutation_kind") != accepted_mutation:
            return "unsafe_binding", "mutation_kind"
        for response_key, request_key in (
            ("campaign_id", "campaign_id"),
            ("source_revision", "base_revision"),
            ("base_revision", "base_revision"),
            ("expected_campaign_head", "base_revision"),
        ):
            expected = request_binding.get(request_key)
            if expected is not None and instance.get(response_key) != expected:
                return "unsafe_binding", response_key

        expected_record_id = request_binding.get("record_id")
        candidate = accepted.get("candidate")
        if expected_record_id is None and isinstance(candidate, dict):
            expected_record_id = candidate.get("record_id")
        if _proposal_subject_record_id(instance) != expected_record_id:
            return "unsafe_binding", "diff.cards.subject_record_id"

        expected_kind = {
            "create": "record_created",
            "edit": "record_updated",
            "remove": "record_removed",
        }[accepted_mutation]
        record_cards = [
            (index, card)
            for index, card in enumerate(instance.get("diff", {}).get("cards", []))
            if card.get("kind") == expected_kind
        ]
        if len(record_cards) != 1:
            return "unsafe_binding", "diff.cards"
        card_index, record_card = record_cards[0]
        if accepted_mutation in {"create", "edit"}:
            if not isinstance(candidate, dict) or record_card.get("after") != candidate:
                return "unsafe_binding", f"diff.cards.{card_index}.after"
            if accepted_mutation == "edit":
                expected_record_digest = request_binding.get("record_digest")
                before = record_card.get("before")
                if (
                    expected_record_digest is not None
                    and (
                        not isinstance(before, dict)
                        or before.get("content_digest") != expected_record_digest
                    )
                ):
                    return "unsafe_binding", f"diff.cards.{card_index}.before.content_digest"
                response_bindings = [
                    (index, binding)
                    for index, binding in enumerate(instance.get("record_bindings", []))
                    if binding.get("record_id") == request_binding.get("record_id")
                ]
                if len(response_bindings) != 1:
                    return "unsafe_binding", "record_bindings"
                binding_index, response_binding = response_bindings[0]
                for key in ("campaign_id", "base_revision", "record_digest"):
                    if response_binding.get(key) != request_binding.get(key):
                        return "unsafe_binding", f"record_bindings.{binding_index}.{key}"
        else:
            before = record_card.get("before")
            if not isinstance(before, dict):
                return "unsafe_binding", f"diff.cards.{card_index}.before"
            for response_key, request_key in (
                ("record_id", "record_id"),
                ("content_digest", "record_digest"),
            ):
                if before.get(response_key) != request_binding.get(request_key):
                    return "unsafe_binding", f"diff.cards.{card_index}.before.{response_key}"
            expected_impact_binding = accepted.get("impact_binding")
            actual_impact_binding = instance.get("impact_binding")
            if isinstance(expected_impact_binding, dict):
                if not isinstance(actual_impact_binding, dict):
                    return "unsafe_binding", "impact_binding"
                if actual_impact_binding.get("binding") != expected_impact_binding.get("binding"):
                    return "unsafe_binding", "impact_binding.binding"
                if actual_impact_binding.get("impact_digest") != expected_impact_binding.get("impact_digest"):
                    return "unsafe_binding", "impact_binding.impact_digest"
                if instance.get("impact_digest") != expected_impact_binding.get("impact_digest"):
                    return "unsafe_binding", "impact_digest"
            if "resolutions" in accepted and instance.get("resolutions") != accepted.get("resolutions"):
                return "unsafe_binding", "resolutions"

    if (
        instance.get("contract_name") == "editor_proposal_view"
        and isinstance(accepted, dict)
        and accepted.get("operation_request", {}).get("operation")
        == "editor_proposal_correct"
    ):
        prior = accepted.get("prior_proposal")
        if isinstance(prior, dict):
            actual_proposal = instance.get("proposal_id")
            if actual_proposal != prior.get("proposal_id"):
                return "unsafe_binding", "proposal_id"
            if instance.get("proposal_version") <= prior.get("proposal_version"):
                return "unsafe_binding", "proposal_version"
            if instance.get("correction_of") != prior:
                return "unsafe_binding", "correction_of"
        correction_binding_failure = _correction_result_binding_failure(instance, accepted)
        if correction_binding_failure is not None:
            return correction_binding_failure
        return None
    expected_proposal = accepted.get("proposal") if isinstance(accepted, dict) else None
    actual_proposal = instance.get("proposal")
    if isinstance(expected_proposal, dict):
        for key in ("proposal_id", "proposal_version"):
            if not isinstance(actual_proposal, dict) or actual_proposal.get(key) != expected_proposal.get(key):
                return "unsafe_binding", f"proposal.{key}"

    if instance.get("contract_name") == "editor_proposal_approval_result":
        accepted_base_revision = accepted.get("base_revision") if isinstance(accepted, dict) else None
        published_revision = instance.get("published_revision")
        if isinstance(accepted_base_revision, dict):
            if not isinstance(published_revision, dict):
                return "unsafe_binding", "published_revision"
            if published_revision.get("revision_id") == accepted_base_revision.get("revision_id"):
                return "unsafe_binding", "published_revision"
            if (
                not isinstance(accepted_base_revision.get("ordinal"), int)
                or not isinstance(published_revision.get("ordinal"), int)
                or published_revision["ordinal"] <= accepted_base_revision["ordinal"]
            ):
                return "unsafe_binding", "published_revision"
        receipt_has_revision, expected_revision = _stored_publication_revision(fixture)
        if receipt_has_revision and instance.get("published_revision") != expected_revision:
            if isinstance(expected_revision, dict) and isinstance(instance.get("published_revision"), dict):
                for key in ("revision_id", "ordinal", "tree_digest", "immutable"):
                    if instance["published_revision"].get(key) != expected_revision.get(key):
                        return "unsafe_binding", f"published_revision.{key}"
            return "unsafe_binding", "published_revision"
    return None


def _result_workflow_version_failure(
    instance: dict,
    fixture: dict,
) -> tuple[str, str] | None:
    if instance.get("contract_name") not in {
        "editor_proposal_view",
        "editor_proposal_approval_result",
        "editor_proposal_rejection_result",
    }:
        return None

    context = fixture.get("semantic_context", {})
    expected = context.get("expected_result_editor_workflow_version")
    if expected is None:
        expected = context.get("result_editor_workflow_version")
    if expected is None:
        source = _accepted_request(fixture)
        if isinstance(source, dict):
            request = source.get("operation_request")
            if not isinstance(request, dict):
                request = source
            if "expected_editor_workflow_version" in request:
                expected = request["expected_editor_workflow_version"] + 1
    if expected is None:
        receipt = context.get("stored_receipt") or fixture.get("stored_receipt")
        if isinstance(receipt, dict):
            expected = receipt.get("result_editor_workflow_version")
            if expected is None:
                expected = receipt.get("editor_workflow_version")
            if expected is None:
                expected = receipt.get("workflow_version")
    if expected is None and "current_editor_workflow_version" in context:
        expected = context["current_editor_workflow_version"]
    if expected is not None and instance.get("editor_workflow_version") != expected:
        return "unsafe_binding", "editor_workflow_version"
    return None


def _result_replay_failure(instance: dict, fixture: dict) -> tuple[str, str] | None:
    if instance.get("contract_name") not in {
        "editor_proposal_view",
        "editor_proposal_approval_result",
        "editor_proposal_rejection_result",
    }:
        return None

    context = fixture.get("semantic_context", {})
    stored_result = context.get("stored_result")
    if stored_result is None:
        stored_result = fixture.get("stored_result")
    receipt = context.get("stored_receipt") or fixture.get("stored_receipt")
    if stored_result is None and isinstance(receipt, dict):
        for key in ("result", "result_payload", "response"):
            if key in receipt:
                stored_result = receipt[key]
                break
    if isinstance(stored_result, dict) and isinstance(stored_result.get("payload"), dict):
        stored_result = stored_result["payload"]
    if isinstance(stored_result, dict) and stored_result != instance:
        return "replay_mismatch", "result"
    return None


def _diff_record_member_id_failure(diff: dict) -> str | None:
    for index, card in enumerate(diff.get("cards", [])):
        path = f"diff.cards.{index}"
        for side in ("before", "after"):
            value = card.get(side)
            if isinstance(value, dict) and "record_id" in value:
                failure = _record_member_id_failure(value, f"{path}.{side}")
                if failure is not None:
                    return failure
    return None


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
    member_id_failure = _record_member_id_failure(record, path)
    if member_id_failure is not None:
        return member_id_failure
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


def _source_status_matches(structured_status: object, metadata: dict[str, str], *, after: bool) -> bool:
    if after and (
        not isinstance(structured_status, str)
        or structured_status not in VALID_STATUSES
    ):
        return False
    if isinstance(structured_status, str):
        return metadata.get("status") == structured_status
    if not isinstance(structured_status, dict):
        return False
    classification = structured_status.get("classification")
    if classification == "missing":
        return "status" not in metadata
    if classification in {"known", "unknown"}:
        return metadata.get("status") == structured_status.get("value")
    return False


def _frontmatter_has_duplicate_keys(source_text: object) -> bool:
    if not isinstance(source_text, str) or not source_text.startswith("---\n"):
        return False
    end = source_text.find("\n---", 4)
    if end < 0:
        return False
    keys = set()
    for line in source_text[4:end].splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key = line.split(":", 1)[0].strip()
        if key in keys:
            return True
        keys.add(key)
    return False


def source_snapshots_match(diff: dict, *, adapter_definition: dict | None = None) -> bool:
    required_metadata = {
        "id",
        "type",
        "ownership",
        "visibility",
        "warden_only",
    }
    for source in diff.get("source_changes", []):
        subject = source.get("subject_record_id")
        record_kinds = {
            card.get("kind")
            for card in diff.get("cards", [])
            if card.get("subject_record_id") == subject
            and card.get("kind") in {"record_created", "record_updated", "record_removed"}
        }
        if len(record_kinds) > 1:
            return False
        expected_change_type = {
            "record_created": "create",
            "record_removed": "delete",
        }.get(next(iter(record_kinds), "record_updated"), "update")
        if source.get("change_type") != expected_change_type:
            return False
        before_source = source.get("before_source")
        after_source = source.get("after_source")
        if expected_change_type == "create" and (before_source is not None or after_source is None):
            return False
        if expected_change_type == "update" and (before_source is None or after_source is None):
            return False
        if expected_change_type == "delete" and (before_source is None or after_source is not None):
            return False
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
            if _frontmatter_has_duplicate_keys(source_text):
                return False
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
                if "status" in metadata:
                    expected_metadata_keys.add("status")
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
                        structured["visibility"]["audience"] != metadata["visibility"],
                        str(structured["visibility"]["warden_only"]).lower()
                        != metadata["warden_only"],
                    )
                ):
                    return False
                if not _source_status_matches(
                    structured["status"],
                    metadata,
                    after=side == "after",
                ):
                    return False
                for field in structured["fields"]:
                    expected_value = "" if field["value"] is None else str(field["value"])
                    if expected_value != metadata.get(field["field_id"]):
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
    result_replay_failure = _result_replay_failure(instance, fixture)
    if result_replay_failure is not None:
        return result_replay_failure
    result_binding_failure = _result_binding_failure(instance, fixture)
    if result_binding_failure is not None:
        return result_binding_failure
    result_workflow_failure = _result_workflow_version_failure(instance, fixture)
    if result_workflow_failure is not None:
        return result_workflow_failure
    operation = instance.get("operation_request", {})
    binding = instance.get("binding", {})
    candidate = instance.get("candidate")

    bound_revision = binding.get("base_revision")
    if not isinstance(bound_revision, dict):
        bound_revision = instance.get("base_revision", {})
    bound_revision_id = (
        bound_revision.get("revision_id") if isinstance(bound_revision, dict) else None
    )
    if (
        "expected_revision" in operation
        and bound_revision_id is not None
        and operation.get("expected_revision") != bound_revision_id
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

    context = fixture.get("semantic_context", {})
    if instance.get("contract_name") in {"editor_record_view", "editor_creation_context"}:
        viewed_revision = instance.get("viewed_revision")
        head_revision = instance.get("head_revision")
        if (
            instance.get("contract_name") == "editor_creation_context"
            and viewed_revision != head_revision
        ):
            return "unsafe_binding", "viewed_revision"
        if instance.get("contract_name") != "editor_creation_context":
            historical = viewed_revision != head_revision
            if instance.get("historical") != historical:
                return "unsafe_binding", "historical"
            if instance.get("editable") != (not historical):
                return "unsafe_binding", "editable"
    for adapter_definition, path in (
        (instance.get("adapter_definition"), "adapter_definition"),
        (context.get("adapter_definition"), "semantic_context.adapter_definition"),
    ):
        failure = _adapter_definition_failure(adapter_definition, path)
        if failure is not None:
            return "unsafe_binding", failure
    adapter_binding_failure = _adapter_definition_binding_failure(instance, context)
    if adapter_binding_failure is not None:
        return adapter_binding_failure
    proposal_validation_failure = _proposal_validation_gate_failure(instance)
    if proposal_validation_failure is not None:
        return proposal_validation_failure
    approval_binding_failure = _core_approval_binding_failure(instance)
    if approval_binding_failure is not None:
        return approval_binding_failure
    if instance.get("contract_name") == "editor_record_view":
        record = instance.get("record")
        if isinstance(record, dict):
            if record.get("authority") != _authority_for_status(record.get("status")):
                return "invalid_authority_transition", "record.authority"
            failure = _adapter_record_vocabulary_failure(
                record,
                instance.get("adapter_definition", {}),
                "record",
            )
            if failure is not None:
                return "proposal_validation_failure", failure
    if operation.get("operation") in {
        "editor_proposal_approve",
        "editor_proposal_reject",
    }:
        failure = _loaded_proposal_action_failure(instance, context)
        if failure is not None:
            return failure

    receipt = fixture.get("stored_receipt")
    if receipt and receipt.get("idempotency_key") == operation.get("idempotency_key"):
        digest_failure = _operation_payload_digest_failure(instance)
        if digest_failure is not None:
            return digest_failure
        if receipt.get("payload_digest") != operation.get("payload_digest"):
            return "replay_mismatch", "operation_request.payload_digest"

    base_revision = bound_revision if isinstance(bound_revision, dict) else {}
    expected_head = instance.get("expected_campaign_head")
    expected_head_id = (
        expected_head.get("revision_id")
        if isinstance(expected_head, dict)
        else expected_head
    )
    if expected_head is not None and expected_head != base_revision:
        return "unsafe_binding", "expected_campaign_head"
    if instance.get("contract_name") in {"editor_record_view", "editor_creation_context"}:
        head_revision = instance.get("head_revision")
        current_head_binding = (
            head_revision.get("revision_id")
            if isinstance(head_revision, dict)
            else head_revision
        )
    else:
        current_head_binding = expected_head_id or base_revision.get("revision_id")
    if context.get("current_head_revision") != current_head_binding:
        if "current_head_revision" in context:
            return "stale_revision", "binding.base_revision"
    if instance.get("contract_name") == "editor_record_view":
        record = instance.get("record")
        current_record_binding = record.get("content_digest") if isinstance(record, dict) else None
        record_digest_path = "record.content_digest"
    elif instance.get("contract_name") == "editor_creation_context":
        current_record_binding = None
        record_digest_path = "binding.record_digest"
    else:
        current_record_binding = binding.get("record_digest")
        record_digest_path = "binding.record_digest"
    if (
        current_record_binding is not None
        and context.get("current_record_digest") != current_record_binding
    ):
        if "current_record_digest" in context:
            return "stale_record_digest", record_digest_path
    if instance.get("contract_name") in {"editor_record_view", "editor_creation_context"}:
        expected_workflow_version = instance.get("editor_workflow_version")
        workflow_path = "editor_workflow_version"
    else:
        expected_workflow_version = binding.get(
            "expected_editor_workflow_version",
            instance.get("expected_editor_workflow_version"),
        )
        workflow_path = (
            "binding.expected_editor_workflow_version"
            if "expected_editor_workflow_version" in binding
            else "expected_editor_workflow_version"
        )
    if context.get("current_editor_workflow_version") != expected_workflow_version:
        if "current_editor_workflow_version" in context:
            return "workflow_conflict", workflow_path

    if (
        operation.get("operation") in {"editor_record_edit", "editor_proposal_correct"}
        and isinstance(candidate, dict)
        and context.get("current_record_type") is not None
        and candidate.get("record_type") != context["current_record_type"]
    ):
        return "invalid_record_type", "candidate.record_type"

    if isinstance(candidate, dict):
        member_id_failure = _record_member_id_failure(candidate, "candidate")
        if member_id_failure is not None:
            return "proposal_validation_failure", member_id_failure
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
        expected_authority = _authority_for_status(candidate.get("status"))
        if candidate.get("authority") != expected_authority:
            return "invalid_authority_transition", "candidate.authority"

        available_ids = set(context.get("available_record_ids", []))
        for index, connection in enumerate(candidate.get("connections", [])):
            if "available_record_ids" in context and connection.get("target_record_id") not in available_ids:
                return "invalid_connections", f"candidate.connections.{index}.target_record_id"

    prior_proposal = context.get("prior_proposal")
    prior_identity_failure = _prior_proposal_identity_failure(instance, context)
    if prior_identity_failure is not None:
        return prior_identity_failure
    prior_record_id = _proposal_subject_record_id(prior_proposal)
    if prior_proposal and (
        instance.get("mutation_kind") != prior_proposal.get("mutation_kind")
        or (
            prior_record_id is not None
            and binding.get("record_id") != prior_record_id
        )
    ):
        return "invalid_correction", "prior_proposal"

    removal_binding_failure = _removal_impact_binding_failure(instance)
    if removal_binding_failure is not None:
        return removal_binding_failure
    if operation.get("operation") == "editor_proposal_correct" and instance.get("mutation_kind") == "remove":
        impact_binding = instance.get("impact_binding")
        if not isinstance(impact_binding, dict):
            return "proposal_validation_failure", "impact_binding"
        if instance.get("impact_digest") != impact_binding.get("impact_digest"):
            return "proposal_validation_failure", "impact_digest"
        current_impact_digest = context.get("current_removal_impact_digest")
        if current_impact_digest is not None and instance.get("impact_digest") != current_impact_digest:
            return "proposal_validation_failure", "impact_digest"

    impact = fixture.get("impact", {})
    if instance.get("contract_name") == "editor_removal_impact":
        record = instance.get("record", {})
        member_id_failure = _record_member_id_failure(record, "record")
        if member_id_failure is not None:
            return "proposal_validation_failure", member_id_failure
        if record.get("ownership") != "campaign":
            return "proposal_validation_failure", "record.ownership"
        if record.get("authority") != _authority_for_status(record.get("status")):
            return "proposal_validation_failure", "record.authority"
        adapter_definition = context.get("adapter_definition")
        if adapter_definition is not None:
            failure = _adapter_record_vocabulary_failure(
                record,
                adapter_definition,
                "record",
            )
            if failure is not None:
                return "proposal_validation_failure", failure
        binding = instance.get("binding", {})
        if binding.get("record_id") != record.get("record_id"):
            return "proposal_validation_failure", "binding.record_id"
        if binding.get("record_digest") != record.get("content_digest"):
            return "proposal_validation_failure", "binding.record_digest"
        reference_policy_failure = _removal_reference_policy_failure(instance)
        if reference_policy_failure is not None:
            return reference_policy_failure
    logical_id_failure = _logical_id_failure(instance, instance.get("diff"))
    if logical_id_failure is not None:
        return "proposal_validation_failure", logical_id_failure
    required_reference_ids = set()
    required_reference_id = impact.get("required_reference_id")
    if required_reference_id and not impact.get("permitted_unresolved"):
        required_reference_ids.add(required_reference_id)
    for reference in impact.get("incoming_references", []):
        if (
            isinstance(reference, dict)
            and reference.get("resolution_required") is True
            and reference.get("permitted_unresolved") is not True
        ):
            required_reference_ids.add(reference.get("reference_id"))
    resolution_ids = [item.get("reference_id") for item in instance.get("resolutions", [])]
    if len(resolution_ids) != len(set(resolution_ids)):
        return "proposal_validation_failure", "resolutions"
    resolutions = set(resolution_ids)
    if "resolutions" in instance and required_reference_ids - resolutions:
        return "incomplete_removal_resolution", "resolutions"
    if "impact" in fixture:
        resolution_set_failure = _removal_resolution_set_failure(instance, impact)
        if resolution_set_failure is not None:
            return resolution_set_failure

    redirect_failure = _removal_request_redirect_failure(instance, impact, context)
    if redirect_failure is not None:
        return redirect_failure

    diff = instance.get("diff")
    reference_policy_failure = _removal_reference_policy_failure(instance, diff)
    if reference_policy_failure is not None:
        return reference_policy_failure
    if (
        operation.get("operation") in {"editor_proposal_approve", "editor_proposal_reject"}
        and instance.get("diff_digest") is not None
        and operation.get("intent_digest") != instance.get("diff_digest")
    ):
        return "proposal_approval_conflict", "operation_request.intent_digest"
    resolution_policy_failure = _resolution_policy_failure(instance, impact, diff)
    if resolution_policy_failure is not None:
        return resolution_policy_failure
    if (
        isinstance(impact, dict)
        and "impact_digest" in impact
        and (
            operation.get("operation") == "editor_record_remove"
            or (
                operation.get("operation") == "editor_proposal_correct"
                and instance.get("mutation_kind") == "remove"
            )
        )
    ):
        authoritative_impact_digest = projection_digest(
            impact,
            DIGEST_PROJECTIONS["impact_digest"],
        )
        if instance.get("impact_digest") != authoritative_impact_digest:
            return "proposal_validation_failure", "impact_digest"
        impact_binding = instance.get("impact_binding")
        if (
            isinstance(impact_binding, dict)
            and impact_binding.get("impact_digest") != authoritative_impact_digest
        ):
            return "proposal_validation_failure", "impact_binding.impact_digest"
    if isinstance(diff, dict):
        resolution_failure = _reference_resolution_binding_failure(diff)
        if resolution_failure is not None:
            return resolution_failure
        property_change_failure = _record_property_change_failure(diff)
        if property_change_failure is not None:
            return property_change_failure
        subject_failure = _record_subject_failure(diff)
        if subject_failure is not None:
            return subject_failure
        authority_failure = _record_authority_failure(diff)
        if authority_failure is not None:
            return authority_failure
        if "cards" in diff:
            if "record_bindings" in instance:
                binding_failure = _record_binding_failure(instance, diff)
                if binding_failure is not None:
                    return binding_failure
            resolution_set_failure = _resolution_set_failure(instance, diff)
            if resolution_set_failure is not None:
                return resolution_set_failure
            redirect_failure = _removal_redirect_failure(diff, context)
            if redirect_failure is not None:
                return redirect_failure
        member_id_failure = _diff_record_member_id_failure(diff)
        if member_id_failure is not None:
            return "proposal_validation_failure", member_id_failure
        adapter_definition = context.get("adapter_definition")
        if adapter_definition is not None:
            failure = _adapter_diff_vocabulary_failure(diff, adapter_definition)
            if failure is not None:
                return "proposal_validation_failure", failure
        record_type_failure = _record_type_transition_failure(diff)
        if record_type_failure is not None:
            return record_type_failure
        if operation.get("operation") in {"editor_proposal_approve", "editor_proposal_reject"}:
            if operation.get("operation") == "editor_proposal_approve":
                for key in (
                    "diff_digest",
                    "confirmed_change_ids",
                    "confirmed_authority_change_ids",
                    "confirmed_visibility_change_ids",
                ):
                    if diff.get(key) != instance.get(key):
                        return "proposal_approval_conflict", f"diff.{key}"
        if instance.get("contract_name") == "editor_proposal_view":
            card_subjects = {card.get("subject_record_id") for card in diff.get("cards", [])}
            source_subjects = [source.get("subject_record_id") for source in diff.get("source_changes", [])]
            if len(source_subjects) != len(set(source_subjects)) or set(source_subjects) != card_subjects:
                return "mutation_consistency", "diff.source_changes"
            if diff.get("affected_record_count") != len(card_subjects):
                return "mutation_consistency", "diff.affected_record_count"
            record_card_subjects = [
                card.get("subject_record_id")
                for card in diff.get("cards", [])
                if card.get("kind") in {"record_created", "record_updated", "record_removed"}
            ]
            if len(record_card_subjects) != len(set(record_card_subjects)):
                return "proposal_validation_failure", "diff.cards.record_mutation"
            if not source_snapshots_match(
                diff,
                adapter_definition=context.get("adapter_definition"),
            ):
                return "mutation_consistency", "diff.source_changes"
        connection_failure = _connection_delta_failure(diff)
        if connection_failure is not None:
            return connection_failure

        transition_failure = _transition_completeness_failure(instance, context)
        if transition_failure is not None:
            return transition_failure

        backlink_failure = _backlink_binding_failure(diff)
        if backlink_failure is not None:
            return backlink_failure
        core_change_failure = _core_change_failure(instance, diff)
        if core_change_failure is not None:
            return core_change_failure

    correction_reference_failure = _correction_reference_failure(instance)
    if correction_reference_failure is not None:
        return correction_reference_failure

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

    if operation.get("operation") in {
        "editor_proposal_approve",
        "editor_proposal_reject",
    }:
        loaded = context.get("loaded_proposal")
        if isinstance(loaded, dict) and isinstance(loaded.get("payload"), dict):
            loaded = loaded["payload"]
        if isinstance(loaded, dict):
            digest_failure = _declared_digest_failure(loaded, context)
            if digest_failure is not None:
                return digest_failure
            if isinstance(loaded.get("diff"), dict):
                unresolved_count_failure = _unresolved_reference_count_failure(loaded["diff"])
                if unresolved_count_failure is not None:
                    return unresolved_count_failure

    digest_failure = _operation_payload_digest_failure(instance)
    if digest_failure is not None:
        return digest_failure
    digest_failure = _declared_digest_failure(instance, context)
    if digest_failure is not None:
        return digest_failure
    if isinstance(diff, dict):
        unresolved_count_failure = _unresolved_reference_count_failure(diff)
        if unresolved_count_failure is not None:
            return unresolved_count_failure
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
        self.assertEqual("unsafe_binding", self.invariants["error_category_mapping"]["unsafe_binding"])
        rules = {rule["id"]: rule for rule in self.invariants["rules"]}
        for rule_id in ("editor_record_member_ids", "editor_status_authority", "editor_campaign_ownership"):
            with self.subTest(rule=rule_id):
                self.assertIn("editor_removal_impact", rules[rule_id]["applies_to"])
        self.assertIn("editor_proposal_view", rules["editor_workflow_lifecycle"]["applies_to"])
        self.assertIn("editor_proposal_rejection_result", rules["editor_workflow_lifecycle"]["applies_to"])
        self.assertTrue(self.invariants["digest_projections"]["proposal_payload_digest"]["omit_if_absent"])

    def test_every_editor_example_is_schema_valid(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        for example in self.examples:
            with self.subTest(example=example["name"]):
                self.assertEqual([], list(self.validator.iter_errors(example["payload"])))
                adapter_definition = example["payload"].get("adapter_definition")
                self.assertIsNone(
                    _adapter_definition_failure(adapter_definition, "adapter_definition")
                )

    def test_adapter_definition_types_have_matching_definitions(self) -> None:
        context = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "creation_context_empty_revision")
        )
        context["adapter_definition"]["record_types"].append("vehicle")
        self.assertEqual(
            ("unsafe_binding", "adapter_definition.record_types"),
            evaluate_semantic_failure({"instance": context}),
        )

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
                "before_present": True,
                "after_present": True,
            })),
        )
        self.assertEqual(
            [],
            list(property_validator.iter_errors({
                "property": "status",
                "before": {"classification": "unknown", "value": "legacy-state"},
                "after": "review",
                "before_present": True,
                "after_present": True,
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
            ("record_type", "vehicle", "candidate.record_type"),
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
        proposal["diff"]["cards"][0]["before"]["record_type"] = "vehicle"
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
        invalid_visibility = deepcopy(edit)
        invalid_visibility["candidate"]["visibility"] = {
            "audience": "players",
            "warden_only": True,
        }
        restricted_context = deepcopy(context)
        restricted_context["adapter_definition"]["record_definitions"]["location"]["required_values"] = {
            "visibility": "warden",
            "warden_only": "true",
        }
        self.assertEqual(
            ("proposal_validation_failure", "candidate.visibility"),
            evaluate_semantic_failure({"instance": invalid_visibility, "semantic_context": restricted_context}),
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

    def test_record_member_ids_are_unique_in_candidates_and_cards(self) -> None:
        edit = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        for collection, member_id in (
            ("fields", "field_id"),
            ("sections", "section_id"),
            ("connections", "connection_id"),
        ):
            invalid = deepcopy(edit)
            invalid["candidate"][collection].append(deepcopy(invalid["candidate"][collection][0]))
            with self.subTest(candidate=collection):
                self.assertEqual(
                    (
                        "proposal_validation_failure",
                        f"candidate.{collection}.{len(edit['candidate'][collection])}.{member_id}",
                    ),
                    evaluate_semantic_failure({"instance": invalid}),
                )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        for collection, member_id in (
            ("fields", "field_id"),
            ("sections", "section_id"),
            ("connections", "connection_id"),
        ):
            invalid = deepcopy(proposal)
            members = invalid["diff"]["cards"][0]["before"][collection]
            members.append(deepcopy(members[0]))
            with self.subTest(card=collection):
                self.assertEqual(
                    (
                        "proposal_validation_failure",
                        f"diff.cards.0.before.{collection}.{len(members) - 1}.{member_id}",
                    ),
                    evaluate_semantic_failure({"instance": invalid}),
                )

    def test_logical_ids_are_unique_across_bindings_cards_outcomes_and_references(self) -> None:
        proposal = next(
            item["payload"] for item in self.examples if item["name"] == "editor_proposal_view"
        )
        duplicate_cases = []

        invalid = deepcopy(proposal)
        invalid["diff"]["cards"][1]["change_id"] = invalid["diff"]["cards"][0]["change_id"]
        duplicate_cases.append((invalid, "diff.cards.1.change_id"))

        for collection in ("authority_changes", "visibility_changes"):
            invalid = deepcopy(proposal)
            copied = deepcopy(invalid["diff"][collection][0])
            copied["record_id"] = "record-other"
            invalid["diff"][collection].append(copied)
            duplicate_cases.append((invalid, f"diff.{collection}.1.change_id"))

        for collection in ("authority_outcome", "visibility_outcome"):
            invalid = deepcopy(proposal)
            copied = deepcopy(invalid[collection][0])
            copied["record_id"] = "record-other"
            invalid[collection].append(copied)
            duplicate_cases.append((invalid, f"{collection}.1.change_id"))

        invalid = deepcopy(proposal)
        invalid["core_proposal"]["proposal"]["changes"][1]["change_id"] = (
            invalid["core_proposal"]["proposal"]["changes"][0]["change_id"]
        )
        invalid["core_proposal"]["proposal"]["changes"][1]["content_digest"] = "f" * 64
        duplicate_cases.append((invalid, "core_proposal.proposal.changes.1.change_id"))

        invalid = deepcopy(proposal)
        copied = deepcopy(invalid["record_bindings"][0])
        copied["record_digest"] = "f" * 64
        invalid["record_bindings"].append(copied)
        duplicate_cases.append((invalid, "record_bindings.1.record_id"))

        impact = next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        invalid = deepcopy(impact)
        copied = deepcopy(invalid["incoming_references"][0])
        copied["connection_id"] = "connection_two"
        invalid["incoming_references"].append(copied)
        duplicate_cases.append((invalid, "incoming_references.1.reference_id"))

        for invalid, path in duplicate_cases:
            with self.subTest(path=path):
                self.assertEqual(
                    ("proposal_validation_failure", path),
                    evaluate_semantic_failure({"instance": invalid}),
                )

    def test_action_requests_bind_to_the_loaded_proposal(self) -> None:
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        for name in ("approval_request", "rejection_request"):
            action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            with self.subTest(action=name):
                self.assertIsNone(
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"loaded_proposal": loaded},
                    })
                )
                mismatched = deepcopy(loaded)
                mismatched["proposal_version"] += 1
                self.assertEqual(
                    ("proposal_approval_conflict", "proposal.proposal_version"),
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"loaded_proposal": mismatched},
                    }),
                )

        action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == "approval_request"))
        mismatched = deepcopy(action)
        mismatched["base_revision"] = {
            **mismatched["base_revision"],
            "revision_id": "revision_13",
        }
        self.assertEqual(
            ("proposal_approval_conflict", "base_revision"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": mismatched},
            }),
        )

    def test_rejection_intent_binds_without_an_embedded_diff(self) -> None:
        rejection = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "rejection_request")
        )
        rejection["operation_request"]["intent_digest"] = "f" * 64
        self.assertEqual(
            ("proposal_approval_conflict", "operation_request.intent_digest"),
            evaluate_semantic_failure({"instance": rejection}),
        )

    def test_loaded_approval_rejects_staged_warnings(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        loaded["validation"]["findings"].append(
            {
                "finding_id": "finding-warning",
                "code": "needs_review",
                "severity": "warning",
                "location": "record",
                "message": "Review this staged warning before approval.",
                "retryable": False,
            }
        )
        self.assertEqual(
            ("proposal_validation_failure", "validation.findings"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

        loaded["validation"]["findings"] = []
        loaded["validation"]["error_count"] = 1
        self.assertEqual(
            ("proposal_validation_failure", "validation.error_count"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )
        loaded["validation"]["error_count"] = 0
        loaded["validation"]["findings"].append({"severity": "error"})
        self.assertEqual(
            ("proposal_validation_failure", "validation.findings"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_loaded_approval_rejects_missing_connection_cards(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        loaded["diff"]["cards"] = [
            card for card in loaded["diff"]["cards"] if card["kind"] != "connection_added"
        ]
        loaded["core_proposal"]["proposal"]["changes"] = loaded["core_proposal"]["proposal"][
            "changes"
        ][:1]
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["diff_digest"] = loaded["diff"]["diff_digest"]
        action["operation_request"]["intent_digest"] = loaded["diff"]["diff_digest"]
        action["confirmed_change_ids"] = ["change_edit_record"]
        action["diff"]["confirmed_change_ids"] = ["change_edit_record"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("mutation_consistency", "diff.cards.connection_delta"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_loaded_approval_revalidates_source_snapshots(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        source = loaded["diff"]["source_changes"][0]
        source["after_source"] = source["after_source"].replace(
            "# Synthetic Station Renamed", "# Wrong Station"
        )
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["diff_digest"] = loaded["diff"]["diff_digest"]
        action["operation_request"]["intent_digest"] = loaded["diff"]["diff_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("mutation_consistency", "diff.source_changes"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_loaded_approval_requires_the_complete_source_change_set(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        loaded["diff"]["source_changes"] = []
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["diff_digest"] = loaded["diff"]["diff_digest"]
        action["operation_request"]["intent_digest"] = loaded["diff"]["diff_digest"]
        action["diff"]["diff_digest"] = loaded["diff"]["diff_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("mutation_consistency", "diff.source_changes"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_loaded_approval_revalidates_publication_state(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        loaded["publication"]["status"] = "published"
        loaded["publication"]["published_revision"] = {
            "revision_id": "revision_13",
            "ordinal": 13,
            "tree_digest": "d" * 64,
            "immutable": True,
        }
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("proposal_approval_conflict", "publication.status"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_loaded_approval_revalidates_adapter_vocabulary(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        adapter_definition = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")[
                "adapter_definition"
            ]
        )
        card = loaded["diff"]["cards"][1]
        card["connection"]["relationship"] = "unsupported"
        record_card = loaded["diff"]["cards"][0]
        record_connection = next(
            connection
            for connection in record_card["after"]["connections"]
            if connection["connection_id"] == card["connection"]["connection_id"]
        )
        record_connection["relationship"] = "unsupported"
        record_card["after"]["content_digest"] = record_content_digest(
            record_card["after"]
        )
        for change in loaded["core_proposal"]["proposal"]["changes"]:
            change["content_digest"] = record_card["after"]["content_digest"]
        source = next(
            source
            for source in loaded["diff"]["source_changes"]
            if source["subject_record_id"] == card["subject_record_id"]
        )
        source["after_source"] = source["after_source"].replace(
            "- `signals`", "- `unsupported`"
        )
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["diff_digest"] = loaded["diff"]["diff_digest"]
        action["operation_request"]["intent_digest"] = loaded["diff"]["diff_digest"]
        action["diff"]["diff_digest"] = loaded["diff"]["diff_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.0.after.connections.1.relationship"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {
                    "loaded_proposal": loaded,
                    "adapter_definition": adapter_definition,
                },
            }),
        )

    def test_loaded_approval_revalidates_transition_projections(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        loaded["diff"]["authority_changes"] = []
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["diff_digest"] = loaded["diff"]["diff_digest"]
        action["operation_request"]["intent_digest"] = loaded["diff"]["diff_digest"]
        action["diff"]["diff_digest"] = loaded["diff"]["diff_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("proposal_validation_failure", "diff.authority_changes"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_loaded_removal_approval_binds_resolution_cards_to_impact(self) -> None:
        action = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_approval_with_outgoing_connections"
            )
        )
        loaded = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        impact = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_impact_with_outgoing_connections"
            )
        )
        card = next(
            card
            for card in loaded["diff"]["cards"]
            if card["kind"] == "reference_resolution"
        )
        card["before"]["context"] = "Changed after the impact was loaded."
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["diff_digest"] = loaded["diff"]["diff_digest"]
        action["operation_request"]["intent_digest"] = loaded["diff"]["diff_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("unsafe_binding", "diff.cards.2.before"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {
                    "loaded_proposal": loaded,
                    "removal_impact": impact,
                },
            }),
        )

    def test_loaded_actions_recompute_declared_proposal_digests(self) -> None:
        loaded = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        loaded["diff"]["summary"] = "Changed after the proposal was loaded."
        for name in ("approval_request", "rejection_request"):
            action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            with self.subTest(action=name):
                self.assertEqual(
                    ("idempotency_digest_conflict", "proposal_payload_digest"),
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"loaded_proposal": loaded},
                    }),
                )

    def test_operation_payload_digest_is_recomputed_before_first_submission(self) -> None:
        for name in (
            "edit_record_with_connections_request",
            "approval_request",
            "rejection_request",
        ):
            invalid = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            invalid["operation_request"]["payload_digest"] = "0" * 64
            with self.subTest(operation=name):
                self.assertEqual(
                    ("replay_mismatch", "operation_request.payload_digest"),
                    evaluate_semantic_failure({"instance": invalid}),
                )

    def test_non_operation_digests_are_recomputed_by_semantic_evaluation(self) -> None:
        record_view = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")
        )
        record_view["record"]["displayed_name"] = "Changed without a new digest"
        self.assertEqual(
            ("idempotency_digest_conflict", "record.content_digest"),
            evaluate_semantic_failure({"instance": record_view}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["incoming_references"][0]["context"] = "Changed without a new digest"
        self.assertEqual(
            ("idempotency_digest_conflict", "impact_digest"),
            evaluate_semantic_failure({"instance": impact}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["unresolved_reference_count"] = 1
        self.assertEqual(
            ("idempotency_digest_conflict", "diff.diff_digest"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["validation"]["validation_digest"] = "f" * 64
        self.assertEqual(
            ("idempotency_digest_conflict", "validation.validation_digest"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["core_proposal"]["proposal"]["diff_digest"] = "f" * 64
        self.assertEqual(
            ("unsafe_binding", "core_proposal.proposal.diff_digest"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["core_proposal"]["validation"]["validation_digest"] = "f" * 64
        self.assertEqual(
            ("unsafe_binding", "core_proposal.validation.validation_digest"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["proposal_version"] = 2
        self.assertEqual(
            ("idempotency_digest_conflict", "proposal_payload_digest"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["record_bindings"][0]["record_digest"] = "0" * 64
        self.assertEqual(
            ("idempotency_digest_conflict", "record_bindings.0.record_digest"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        removal["record_bindings"][1]["record_digest"] = "f" * 64
        removal["proposal_payload_digest"] = projection_digest(
            removal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("idempotency_digest_conflict", "record_bindings.1.record_digest"),
            evaluate_semantic_failure({"instance": removal}),
        )

        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        unrelated = "- `signals` → [[record-other|Other]] (`current`) — The station signals elsewhere."
        source = next(
            source
            for source in removal["diff"]["source_changes"]
            if source["subject_record_id"] == "record-station"
        )
        for side in ("before_source", "after_source"):
            source[side] = source[side].replace(
                "## Connections\n",
                f"## Connections\n\n{unrelated}\n",
            )
        removal["record_bindings"][1]["record_digest"] = "f" * 64
        removal["proposal_payload_digest"] = projection_digest(
            removal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("idempotency_digest_conflict", "record_bindings.1.record_digest"),
            evaluate_semantic_failure({"instance": removal}),
        )

    def test_resolution_only_binding_digest_preserves_unchanged_connections(self) -> None:
        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        unrelated = "- `signals` → [[record-other|Other]] (`current`) — The station signals elsewhere."
        source = next(
            source
            for source in removal["diff"]["source_changes"]
            if source["subject_record_id"] == "record-station"
        )
        for side in ("before_source", "after_source"):
            source[side] = source[side].replace(
                "## Connections\n",
                f"## Connections\n\n{unrelated}\n",
            )
        authoritative_before = {
            "record_id": "record-station",
            "record_type": "location",
            "displayed_name": "Synthetic Station",
            "ownership": "campaign",
            "status": "review",
            "authority": "preparation",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [],
            "sections": [{"section_id": "summary", "body": "The station handles salvage contracts."}],
            "connections": [
                {
                    "connection_id": "connection_unrelated",
                    "target_record_id": "record-other",
                    "relationship": "signals",
                    "state": "current",
                    "context": "The station signals elsewhere.",
                },
                {
                    "connection_id": "connection_one",
                    "target_record_id": "record-company",
                    "relationship": "works-for",
                    "state": "current",
                    "context": "The station handles salvage contracts.",
                },
            ],
        }
        authoritative_before["content_digest"] = record_content_digest(authoritative_before)
        removal["record_bindings"][1]["record_digest"] = authoritative_before["content_digest"]
        removal["diff"]["diff_digest"] = projection_digest(
            removal["diff"], DIGEST_PROJECTIONS["diff_digest"]
        )
        removal["core_proposal"]["proposal"]["diff_digest"] = removal["diff"]["diff_digest"]
        removal["proposal_payload_digest"] = projection_digest(
            removal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertIsNone(
            evaluate_semantic_failure({
                "instance": removal,
                "semantic_context": {
                    "authoritative_before_records": {
                        "record-station": authoritative_before,
                    },
                },
            })
        )

    def test_action_workflow_conflicts_use_the_top_level_fallback(self) -> None:
        for name in ("approval_request", "rejection_request"):
            action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            with self.subTest(action=name):
                self.assertIsNone(
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"current_editor_workflow_version": 8},
                    })
                )
                self.assertEqual(
                    ("workflow_conflict", "expected_editor_workflow_version"),
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"current_editor_workflow_version": 7},
                    }),
                )

    def test_action_results_bind_the_completed_workflow_version(self) -> None:
        for action_name, result_name in (
            ("approval_request", "approval_success_response"),
            ("rejection_request", "rejection_response"),
        ):
            action = next(item["payload"] for item in self.examples if item["name"] == action_name)
            result = deepcopy(next(item["payload"] for item in self.examples if item["name"] == result_name))
            with self.subTest(result=result_name):
                self.assertIsNone(
                    evaluate_semantic_failure({
                        "instance": result,
                        "semantic_context": {"accepted_request": action},
                    })
                )
                result["editor_workflow_version"] += 1
                self.assertEqual(
                    ("unsafe_binding", "editor_workflow_version"),
                    evaluate_semantic_failure({
                        "instance": result,
                        "semantic_context": {"accepted_request": action},
                    }),
                )

    def test_action_results_bind_the_accepted_proposal_and_publication(self) -> None:
        action = next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        result = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "approval_success_response"
        )
        receipt = {"published_revision": deepcopy(result["published_revision"])}

        invalid = deepcopy(result)
        invalid["proposal"]["proposal_id"] = "proposal-other"
        self.assertEqual(
            ("unsafe_binding", "proposal.proposal_id"),
            evaluate_semantic_failure({
                "instance": invalid,
                "semantic_context": {
                    "accepted_request": action,
                    "stored_receipt": receipt,
                },
            }),
        )

        invalid = deepcopy(result)
        invalid["published_revision"]["revision_id"] = "revision-other"
        self.assertEqual(
            ("unsafe_binding", "published_revision.revision_id"),
            evaluate_semantic_failure({
                "instance": invalid,
                "semantic_context": {
                    "accepted_request": action,
                    "stored_receipt": receipt,
                },
            }),
        )

        rejection = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "rejection_response"
        )
        rejection_request = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "rejection_request"
        )
        self.assertEqual(
            ("unsafe_binding", "contract_name"),
            evaluate_semantic_failure({
                "instance": result,
                "semantic_context": {"accepted_request": rejection_request},
            }),
        )
        self.assertEqual(
            ("unsafe_binding", "contract_name"),
            evaluate_semantic_failure({
                "instance": rejection,
                "semantic_context": {"accepted_request": action},
            }),
        )

        invalid = deepcopy(result)
        invalid["outcome"] = "rejected"
        self.assertEqual(
            ("unsafe_binding", "outcome"),
            evaluate_semantic_failure({
                "instance": invalid,
                "semantic_context": {"accepted_request": action},
            }),
        )
        invalid = deepcopy(rejection)
        invalid["outcome"] = "published"
        self.assertEqual(
            ("unsafe_binding", "outcome"),
            evaluate_semantic_failure({
                "instance": invalid,
                "semantic_context": {"accepted_request": rejection_request},
            }),
        )

    def test_proposal_views_bind_the_returned_workflow_version(self) -> None:
        accepted_request = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        self.assertIsNone(
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"accepted_request": accepted_request},
            })
        )
        proposal["editor_workflow_version"] += 1
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("unsafe_binding", "editor_workflow_version"),
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"accepted_request": accepted_request},
            }),
        )

    def test_proposal_views_bind_accepted_create_edit_and_remove_requests(self) -> None:
        edit_request = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        edit_proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        edit_proposal["mutation_kind"] = "create"
        self.assertEqual(
            ("unsafe_binding", "mutation_kind"),
            evaluate_semantic_failure({
                "instance": edit_proposal,
                "semantic_context": {"accepted_request": edit_request},
            }),
        )

        edit_proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        edit_proposal["diff"]["cards"][0]["after"]["displayed_name"] = "Different record"
        self.assertEqual(
            ("unsafe_binding", "diff.cards.0.after"),
            evaluate_semantic_failure({
                "instance": edit_proposal,
                "semantic_context": {"accepted_request": edit_request},
            }),
        )

        create_request = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "create_record_request")
        )
        self.assertEqual(
            ("unsafe_binding", "mutation_kind"),
            evaluate_semantic_failure({
                "instance": deepcopy(edit_proposal),
                "semantic_context": {"accepted_request": create_request},
            }),
        )

        remove_request = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
        )
        remove_proposal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        remove_request["binding"] = deepcopy(remove_proposal["impact_binding"]["binding"])
        remove_request["impact_binding"] = deepcopy(remove_proposal["impact_binding"])
        remove_request["impact_digest"] = remove_proposal["impact_digest"]
        _refresh_operation_payload_digest(remove_request)
        remove_proposal["impact_binding"]["binding"]["record_id"] = "record-other"
        self.assertEqual(
            ("unsafe_binding", "impact_binding.binding"),
            evaluate_semantic_failure({
                "instance": remove_proposal,
                "semantic_context": {"accepted_request": remove_request},
            }),
        )

    def test_corrected_proposal_views_bind_the_accepted_correction(self) -> None:
        accepted_request = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        self.assertEqual(
            ("unsafe_binding", "proposal_version"),
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"accepted_request": accepted_request},
            }),
        )

    def test_replayed_action_results_bind_the_stored_result(self) -> None:
        result = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_success_response")
        )
        stored_result = deepcopy(result)
        result["proposal"]["proposal_id"] = "proposal-other"
        self.assertEqual(
            ("replay_mismatch", "result"),
            evaluate_semantic_failure({
                "instance": result,
                "semantic_context": {"stored_receipt": {"result": stored_result}},
            }),
        )

        result = deepcopy(stored_result)
        result["published_revision"]["revision_id"] = "revision-other"
        self.assertEqual(
            ("replay_mismatch", "result"),
            evaluate_semantic_failure({
                "instance": result,
                "stored_result": stored_result,
            }),
        )

    def test_replayed_proposal_views_bind_the_stored_result(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        stored_proposal = deepcopy(proposal)
        proposal["diff"]["summary"] = "Different replay"
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("replay_mismatch", "result"),
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"stored_receipt": {"result": stored_proposal}},
            }),
        )

    def test_removal_reference_policy_applies_to_proposals_and_loaded_proposals(self) -> None:
        proposal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        proposal["diff"]["cards"][2]["before"]["permitted_unresolved"] = True
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.2.before.permitted_unresolved"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        approval = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_approval_with_outgoing_connections"
            )
        )
        loaded = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        loaded["diff"]["cards"][2]["before"]["resolution_required"] = False
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.2.before.resolution_required"),
            evaluate_semantic_failure({
                "instance": approval,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_source_snapshots_reject_duplicate_frontmatter_keys(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        source = proposal["diff"]["source_changes"][0]
        source["before_source"] = source["before_source"].replace(
            "status: review\n",
            "status: draft\nstatus: review\n",
            1,
        )
        source["after_source"] = source["after_source"].replace(
            "status: canon\n",
            "status: draft\nstatus: canon\n",
            1,
        )
        self.assertEqual(
            ("mutation_consistency", "diff.source_changes"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_loaded_approval_revalidates_removal_impact_binding(self) -> None:
        action = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_approval_with_outgoing_connections"
            )
        )
        loaded = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        loaded["impact_binding"]["binding"]["record_id"] = "record-station"
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["impact_binding"]["binding"]["record_id"] = "record-station"
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("proposal_validation_failure", "impact_binding.binding.record_id"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_approved_proposals_require_a_later_published_revision(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        core_proposal = proposal["core_proposal"]["proposal"]
        core_proposal["status"] = "approved"
        proposal["publication"] = {
            "status": "published",
            "published_revision": deepcopy(
                next(item["payload"] for item in self.examples if item["name"] == "approval_success_response")[
                    "published_revision"
                ]
            ),
        }
        proposal["core_proposal"]["approval_binding"] = {
            "proposal_id": core_proposal["proposal_id"],
            "proposal_version": core_proposal["proposal_version"],
            "diff_digest": core_proposal["diff_digest"],
            "base_revision": core_proposal["base_revision"],
            "source_revision": core_proposal["source_revision"],
            "expected_campaign_head": core_proposal["expected_campaign_head"],
            "expected_editor_workflow_version": core_proposal[
                "expected_editor_workflow_version"
            ],
            "validation_status": proposal["core_proposal"]["validation"]["status"],
            "validation_digest": proposal["core_proposal"]["validation"]["validation_digest"],
            "authority_change_ids": core_proposal["authority_change_ids"],
            "visibility_change_ids": core_proposal["visibility_change_ids"],
            "warden_confirmed": True,
        }
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertIsNone(evaluate_semantic_failure({"instance": proposal}))

        proposal["publication"]["published_revision"] = {
            **proposal["base_revision"],
            "immutable": True,
        }
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_approval_conflict", "publication.published_revision"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_proposal_cards_reject_record_type_migrations(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        card = proposal["diff"]["cards"][0]
        card["before"]["record_type"] = "npc"
        card["after"]["record_type"] = "faction"
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.0.after.record_type"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_proposal_unresolved_reference_count_matches_resolution_actions(self) -> None:
        proposal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        proposal["diff"]["unresolved_reference_count"] = 1
        proposal["diff"]["diff_digest"] = projection_digest(
            proposal["diff"],
            DIGEST_PROJECTIONS["diff_digest"],
        )
        proposal["core_proposal"]["proposal"]["diff_digest"] = proposal["diff"]["diff_digest"]
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("mutation_consistency", "diff.unresolved_reference_count"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_loaded_approval_binds_all_removal_impact_digests(self) -> None:
        action = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_approval_with_outgoing_connections"
            )
        )
        loaded = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        impact = next(
            item["payload"]
            for item in self.examples
            if item["name"] == "removal_impact_with_outgoing_connections"
        )
        loaded["diff"]["impact_digest"] = "f" * 64
        loaded["impact_digest"] = "f" * 64
        loaded["impact_binding"]["impact_digest"] = "f" * 64
        loaded["diff"]["diff_digest"] = projection_digest(
            loaded["diff"],
            DIGEST_PROJECTIONS["diff_digest"],
        )
        loaded["core_proposal"]["proposal"]["diff_digest"] = loaded["diff"]["diff_digest"]
        loaded["proposal_payload_digest"] = projection_digest(
            loaded,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        action["proposal_payload_digest"] = loaded["proposal_payload_digest"]
        action["impact_digest"] = loaded["impact_digest"]
        action["impact_binding"]["impact_digest"] = loaded["impact_binding"]["impact_digest"]
        _refresh_operation_payload_digest(action)
        self.assertEqual(
            ("proposal_validation_failure", "impact_digest"),
            evaluate_semantic_failure({
                "instance": action,
                "semantic_context": {
                    "loaded_proposal": loaded,
                    "removal_impact": impact,
                },
            }),
        )

    def test_approval_results_bind_published_revision_to_accepted_base(self) -> None:
        action = next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        result = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_success_response")
        )
        result["published_revision"] = {
            **action["base_revision"],
            "immutable": True,
        }
        self.assertEqual(
            ("unsafe_binding", "published_revision"),
            evaluate_semantic_failure({
                "instance": result,
                "semantic_context": {"accepted_request": action},
            }),
        )

    def test_edit_proposals_bind_before_digest_to_accepted_request(self) -> None:
        action = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "edit_record_with_connections_request"
            )
        )
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["cards"][0]["before"]["content_digest"] = "f" * 64
        self.assertEqual(
            ("unsafe_binding", "diff.cards.0.before.content_digest"),
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"accepted_request": action},
            }),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["record_bindings"][0]["record_digest"] = "f" * 64
        self.assertEqual(
            ("unsafe_binding", "record_bindings.0.record_digest"),
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"accepted_request": action},
            }),
        )

    def test_corrected_proposals_bind_the_submitted_candidate(self) -> None:
        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["proposal_version"] = 2
        proposal["correction_of"] = deepcopy(action["prior_proposal"])
        self.assertEqual(
            ("unsafe_binding", "diff.cards.0.after"),
            evaluate_semantic_failure({
                "instance": proposal,
                "semantic_context": {"accepted_request": action},
            }),
        )

        valid = deepcopy(proposal)
        valid["editor_workflow_version"] = 9
        valid["proposal_version"] = 2
        valid["correction_of"] = deepcopy(action["prior_proposal"])
        valid["diff"]["cards"][0]["after"] = deepcopy(action["candidate"])
        valid["record_bindings"][0]["expected_editor_workflow_version"] = 9
        self.assertIsNone(_correction_result_binding_failure(valid, action))

    def test_corrected_create_proposals_bind_the_submitted_candidate(self) -> None:
        create = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "create_record_request")
        )
        create["operation_request"]["operation"] = "editor_proposal_correct"
        create["operation_request"]["expected_editor_workflow_version"] = 8
        create["prior_proposal"] = {"proposal_id": "proposal_create", "proposal_version": 1}
        create["binding"]["expected_editor_workflow_version"] = 8
        create["mutation_kind"] = "create"

        response = {
            "contract_name": "editor_proposal_view",
            "mutation_kind": "create",
            "campaign_id": create["binding"]["campaign_id"],
            "source_revision": deepcopy(create["binding"]["base_revision"]),
            "base_revision": deepcopy(create["binding"]["base_revision"]),
            "expected_campaign_head": deepcopy(create["binding"]["base_revision"]),
            "editor_workflow_version": 9,
            "proposal_id": "proposal_create",
            "proposal_version": 2,
            "correction_of": deepcopy(create["prior_proposal"]),
            "diff": {
                "cards": [{
                    "kind": "record_created",
                    "subject_record_id": "record-new",
                    "after": deepcopy(create["candidate"]),
                }],
            },
            "record_bindings": [{
                "campaign_id": create["binding"]["campaign_id"],
                "base_revision": deepcopy(create["binding"]["base_revision"]),
                "record_id": "record-new",
                "record_digest": None,
                "expected_editor_workflow_version": 9,
            }],
        }
        self.assertIsNone(_correction_result_binding_failure(response, create))

    def test_record_view_history_flags_bind_to_revision_objects(self) -> None:
        historical = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "historical_record_view")
        )
        historical["historical"] = False
        historical["editable"] = True
        self.assertEqual(
            ("unsafe_binding", "historical"),
            evaluate_semantic_failure({"instance": historical}),
        )

    def test_record_views_bind_current_head_digest_and_workflow(self) -> None:
        record_view = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")
        )
        context = {
            "current_head_revision": record_view["head_revision"]["revision_id"],
            "current_record_digest": record_view["record"]["content_digest"],
            "current_editor_workflow_version": record_view["editor_workflow_version"],
        }
        self.assertIsNone(
            evaluate_semantic_failure({"instance": record_view, "semantic_context": context})
        )

        context["current_head_revision"] = "revision_other"
        self.assertEqual(
            ("stale_revision", "binding.base_revision"),
            evaluate_semantic_failure({"instance": record_view, "semantic_context": context}),
        )
        context["current_head_revision"] = record_view["head_revision"]["revision_id"]
        context["current_record_digest"] = "f" * 64
        self.assertEqual(
            ("stale_record_digest", "record.content_digest"),
            evaluate_semantic_failure({"instance": record_view, "semantic_context": context}),
        )
        context["current_record_digest"] = record_view["record"]["content_digest"]
        context["current_editor_workflow_version"] = record_view["editor_workflow_version"] + 1
        self.assertEqual(
            ("workflow_conflict", "editor_workflow_version"),
            evaluate_semantic_failure({"instance": record_view, "semantic_context": context}),
        )

    def test_record_views_bind_authority_and_adapter_vocabulary(self) -> None:
        record_view = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")
        )
        record_view["record"]["status"] = {
            "classification": "known",
            "value": "canon",
        }
        record_view["record"]["authority"] = "canon"
        record_view["record"]["content_digest"] = record_content_digest(record_view["record"])
        self.assertIsNone(evaluate_semantic_failure({"instance": record_view}))

        record_view["record"]["authority"] = "preparation"
        record_view["record"]["content_digest"] = record_content_digest(record_view["record"])
        self.assertEqual(
            ("invalid_authority_transition", "record.authority"),
            evaluate_semantic_failure({"instance": record_view}),
        )

        record_view = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")
        )
        record_view["record"]["status"] = "canon"
        record_view["record"]["authority"] = "preparation"
        record_view["record"]["content_digest"] = record_content_digest(record_view["record"])
        self.assertEqual(
            ("invalid_authority_transition", "record.authority"),
            evaluate_semantic_failure({"instance": record_view}),
        )

        record_view = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")
        )
        record_view["record"]["record_type"] = "vehicle"
        record_view["record"]["content_digest"] = record_content_digest(record_view["record"])
        self.assertEqual(
            ("proposal_validation_failure", "record.record_type"),
            evaluate_semantic_failure({"instance": record_view}),
        )

        bound_adapter = deepcopy(record_view["adapter_definition"])
        changed_adapter = deepcopy(record_view)
        changed_adapter["adapter_definition"]["record_types"].remove("faction")
        del changed_adapter["adapter_definition"]["record_definitions"]["faction"]
        self.assertEqual(
            ("unsafe_binding", "adapter_definition"),
            evaluate_semantic_failure({
                "instance": changed_adapter,
                "semantic_context": {"adapter_definition": bound_adapter},
            }),
        )

        creation = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "creation_context_empty_revision"
            )
        )
        bound_adapter = deepcopy(creation["adapter_definition"])
        creation["adapter_definition"]["record_types"].remove("faction")
        del creation["adapter_definition"]["record_definitions"]["faction"]
        self.assertEqual(
            ("unsafe_binding", "adapter_definition"),
            evaluate_semantic_failure({
                "instance": creation,
                "semantic_context": {"adapter_definition": bound_adapter},
            }),
        )

    def test_record_mutation_cards_are_unique_per_subject(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        duplicate = deepcopy(proposal["diff"]["cards"][0])
        duplicate["change_id"] = "change_edit_record_again"
        proposal["diff"]["cards"].append(duplicate)
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.record_mutation"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        extra = deepcopy(proposal["diff"]["cards"][0])
        extra["subject_record_id"] = "record-other"
        proposal["diff"]["cards"].append(extra)
        self.assertEqual(
            ("proposal_validation_failure", "diff.cards.record_mutation"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_record_mutation_cards_derive_property_changes_and_authority(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["cards"][0]["property_changes"] = []
        self.assertEqual(
            ("mutation_consistency", "diff.cards.0.property_changes"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        card = proposal["diff"]["cards"][0]
        card["after"]["fields"].append({"field_id": "optional", "value": None})
        self.assertEqual(
            {
                "property": "fields.optional",
                "before": None,
                "after": None,
                "before_present": False,
                "after_present": True,
            },
            next(
                change
                for change in _record_property_changes(card["before"], card["after"])
                if change["property"] == "fields.optional"
            ),
        )
        self.assertEqual(
            ("mutation_consistency", "diff.cards.0.property_changes"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        before = deepcopy(card["after"])
        after = deepcopy(card["after"])
        after["fields"] = [
            field for field in after["fields"] if field["field_id"] != "optional"
        ]
        self.assertEqual(
            {
                "property": "fields.optional",
                "before": None,
                "after": None,
                "before_present": True,
                "after_present": False,
            },
            _record_property_changes(before, after)[-1],
        )

    def test_reference_resolution_cards_bind_source_subject(self) -> None:
        proposal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        card_index = next(
            index
            for index, card in enumerate(proposal["diff"]["cards"])
            if card["kind"] == "reference_resolution"
        )
        proposal["diff"]["cards"][card_index]["before"]["source_record_id"] = "record-other"
        self.assertEqual(
            (
                "unsafe_binding",
                f"diff.cards.{card_index}.before.source_record_id",
            ),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_approved_proposal_views_require_passed_validation(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["core_proposal"]["proposal"]["status"] = "approved"
        proposal["publication"] = {
            "status": "published",
            "published_revision": deepcopy(
                next(item["payload"] for item in self.examples if item["name"] == "approval_success_response")[
                    "published_revision"
                ]
            ),
        }
        proposal["validation"]["status"] = "failed"
        proposal["validation"]["error_count"] = 1
        proposal["validation"]["validation_digest"] = projection_digest(
            proposal["validation"],
            DIGEST_PROJECTIONS["validation_digest"],
        )
        proposal["core_proposal"]["validation"]["validation_digest"] = proposal["validation"][
            "validation_digest"
        ]
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_validation_failure", "validation.status"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_needs_review_proposal_views_require_passed_validation(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["validation"]["status"] = "failed"
        proposal["validation"]["error_count"] = 1
        proposal["validation"]["findings"] = [{"severity": "error"}]
        proposal["core_proposal"]["validation"]["status"] = "failed"
        proposal["core_proposal"]["validation"]["error_count"] = 1
        proposal["validation"]["validation_digest"] = projection_digest(
            proposal["validation"],
            DIGEST_PROJECTIONS["validation_digest"],
        )
        proposal["core_proposal"]["validation"]["validation_digest"] = proposal["validation"][
            "validation_digest"
        ]
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_validation_failure", "validation.status"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_approving_proposal_views_require_error_free_validation(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        core_proposal = proposal["core_proposal"]["proposal"]
        core_proposal["status"] = proposal["proposal_status"] = "approving"
        proposal["publication"] = {"status": "quarantined", "published_revision": None}
        proposal["validation"]["error_count"] = 1
        proposal["validation"]["validation_digest"] = projection_digest(
            proposal["validation"], DIGEST_PROJECTIONS["validation_digest"]
        )
        proposal["core_proposal"]["validation"]["error_count"] = 1
        proposal["core_proposal"]["validation"]["validation_digest"] = proposal["validation"][
            "validation_digest"
        ]
        proposal["core_proposal"]["approval_binding"] = {
            "proposal_id": core_proposal["proposal_id"],
            "proposal_version": core_proposal["proposal_version"],
            "diff_digest": core_proposal["diff_digest"],
            "base_revision": core_proposal["base_revision"],
            "source_revision": core_proposal["source_revision"],
            "expected_campaign_head": core_proposal["expected_campaign_head"],
            "expected_editor_workflow_version": core_proposal[
                "expected_editor_workflow_version"
            ],
            "validation_status": proposal["core_proposal"]["validation"]["status"],
            "validation_digest": proposal["core_proposal"]["validation"]["validation_digest"],
            "authority_change_ids": core_proposal["authority_change_ids"],
            "visibility_change_ids": core_proposal["visibility_change_ids"],
            "warden_confirmed": True,
        }
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_validation_failure", "validation.error_count"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_needs_review_proposal_views_retain_warning_findings(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["validation"]["findings"].append({"severity": "warning"})
        proposal["validation"]["validation_digest"] = projection_digest(
            proposal["validation"], DIGEST_PROJECTIONS["validation_digest"]
        )
        proposal["core_proposal"]["validation"]["validation_digest"] = proposal["validation"][
            "validation_digest"
        ]
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertIsNone(evaluate_semantic_failure({"instance": proposal}))

    def test_proposal_publication_matches_core_status(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["publication"] = {
            "status": "published",
            "published_revision": deepcopy(
                next(item["payload"] for item in self.examples if item["name"] == "approval_success_response")[
                    "published_revision"
                ]
            ),
        }
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_approval_conflict", "publication.status"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_embedded_proposals_bind_approval_state_and_identity(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        core = proposal["core_proposal"]
        core_proposal = core["proposal"]
        core_proposal["status"] = "approved"
        core["approval_binding"] = {
            "proposal_id": core_proposal["proposal_id"],
            "proposal_version": core_proposal["proposal_version"],
            "diff_digest": core_proposal["diff_digest"],
            "base_revision": core_proposal["base_revision"],
            "source_revision": core_proposal["source_revision"],
            "expected_campaign_head": core_proposal["expected_campaign_head"],
            "expected_editor_workflow_version": core_proposal[
                "expected_editor_workflow_version"
            ],
            "validation_status": core["validation"]["status"],
            "validation_digest": core["validation"]["validation_digest"],
            "authority_change_ids": core_proposal["authority_change_ids"],
            "visibility_change_ids": core_proposal["visibility_change_ids"],
            "warden_confirmed": True,
        }
        proposal["publication"] = {
            "status": "published",
            "published_revision": deepcopy(
                next(item["payload"] for item in self.examples if item["name"] == "approval_success_response")[
                    "published_revision"
                ]
            ),
        }
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertIsNone(evaluate_semantic_failure({"instance": proposal}))

        invalid = deepcopy(proposal)
        invalid["core_proposal"]["approval_binding"]["proposal_id"] = "proposal-other"
        invalid["proposal_payload_digest"] = projection_digest(
            invalid,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            (
                "proposal_approval_conflict",
                "core_proposal.approval_binding.proposal_id",
            ),
            evaluate_semantic_failure({"instance": invalid}),
        )

        invalid = deepcopy(proposal)
        invalid["core_proposal"]["proposal"]["status"] = "needs_review"
        invalid["publication"] = {"status": "not_published", "published_revision": None}
        invalid["proposal_payload_digest"] = projection_digest(
            invalid,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_approval_conflict", "core_proposal.approval_binding"),
            evaluate_semantic_failure({"instance": invalid}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        card = proposal["diff"]["cards"][0]
        card["after"]["status"] = "review"
        card["property_changes"] = _record_property_changes(card["before"], card["after"])
        self.assertEqual(
            ("invalid_authority_transition", "diff.cards.0.after.authority"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_authority_and_visibility_transitions_are_complete(self) -> None:
        proposal = next(
            item["payload"] for item in self.examples if item["name"] == "editor_proposal_view"
        )
        cases = (
            ("diff.authority_changes", "diff", "authority_changes"),
            ("diff.visibility_changes", "diff", "visibility_changes"),
            ("authority_outcome", "instance", "authority_outcome"),
            ("visibility_outcome", "instance", "visibility_outcome"),
        )
        for expected_path, container, field in cases:
            invalid = deepcopy(proposal)
            if container == "diff":
                invalid[container][field] = []
            else:
                invalid[field] = []
            with self.subTest(path=expected_path):
                self.assertEqual(
                    ("proposal_validation_failure", expected_path),
                    evaluate_semantic_failure({"instance": invalid}),
                )

        invalid = deepcopy(proposal)
        invalid["core_proposal"]["proposal"]["authority_change_ids"] = []
        self.assertEqual(
            (
                "proposal_validation_failure",
                "core_proposal.proposal.authority_change_ids",
            ),
            evaluate_semantic_failure({"instance": invalid}),
        )

        action = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        )
        invalid = deepcopy(action)
        invalid["authority_outcome"] = []
        invalid["confirmed_authority_change_ids"] = []
        invalid["diff"]["confirmed_authority_change_ids"] = []
        loaded = deepcopy(proposal)
        loaded["authority_outcome"] = []
        self.assertEqual(
            ("proposal_validation_failure", "authority_outcome"),
            evaluate_semantic_failure({
                "instance": invalid,
                "semantic_context": {"loaded_proposal": loaded},
            }),
        )

    def test_ordinary_connection_cards_cover_every_record_delta(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["cards"] = [
            card
            for card in proposal["diff"]["cards"]
            if card["kind"] != "connection_added"
        ]
        self.assertEqual(
            ("mutation_consistency", "diff.cards.connection_delta"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_proposal_bindings_cover_resolutions_backlinks_records_and_core_changes(self) -> None:
        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        removal["resolutions"][0]["replacement_target_record_id"] = "record-company"
        self.assertEqual(
            ("unsafe_binding", "resolutions"),
            evaluate_semantic_failure({"instance": removal}),
        )

        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        removal["impact_binding"]["binding"]["record_id"] = "record-station"
        removal["impact_binding"]["binding"]["record_digest"] = removal["record_bindings"][1][
            "record_digest"
        ]
        removal["proposal_payload_digest"] = projection_digest(
            removal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("proposal_validation_failure", "impact_binding.binding.record_id"),
            evaluate_semantic_failure({"instance": removal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["cards"][1]["derived_backlinks"] = []
        self.assertEqual(
            ("unsafe_binding", "diff.cards.1.derived_backlinks"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["cards"][0]["before"]["record_id"] = "record-other"
        proposal["diff"]["cards"][0]["after"]["record_id"] = "record-other"
        self.assertEqual(
            ("unsafe_binding", "diff.cards.0.before.record_id"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["record_bindings"][0]["campaign_id"] = "campaign_other"
        self.assertEqual(
            ("unsafe_binding", "record_bindings.0.campaign_id"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["core_proposal"]["proposal"]["changes"][0]["subject_id"] = "record-other"
        self.assertEqual(
            ("unsafe_binding", "core_proposal.proposal.changes.0.subject_id"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["core_proposal"]["proposal"]["proposal_id"] = "proposal-other"
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("unsafe_binding", "core_proposal.proposal.proposal_id"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        removal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        removal["resolutions"][0]["replacement_target_record_id"] = "record-company"
        removal["diff"]["cards"][2]["after"]["replacement_target_record_id"] = "record-company"
        removal["diff"]["cards"][2]["resolution"] = deepcopy(removal["diff"]["cards"][2]["after"])
        self.assertEqual(
            (
                "proposal_validation_failure",
                "diff.cards.2.after.replacement_target_record_id",
            ),
            evaluate_semantic_failure({"instance": removal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["expected_campaign_head"]["revision_id"] = "revision_11"
        self.assertEqual(
            ("unsafe_binding", "expected_campaign_head"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["diff"]["affected_record_count"] = 2
        self.assertEqual(
            ("mutation_consistency", "diff.affected_record_count"),
            evaluate_semantic_failure({"instance": proposal}),
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

        for name in ("approval_request", "rejection_request"):
            action = deepcopy(next(item["payload"] for item in self.examples if item["name"] == name))
            action["operation_request"]["expected_revision"] = "revision_11"
            with self.subTest(action=f"{name}-revision"):
                self.assertEqual(
                    ("unsafe_binding", "operation_request.expected_revision"),
                    evaluate_semantic_failure({"instance": action}),
                )

            action["operation_request"]["expected_revision"] = "revision_12"
            with self.subTest(action=f"{name}-current-head"):
                self.assertIsNone(
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"current_head_revision": "revision_12"},
                    })
                )
            with self.subTest(action=f"{name}-stale-head"):
                self.assertEqual(
                    ("stale_revision", "binding.base_revision"),
                    evaluate_semantic_failure({
                        "instance": action,
                        "semantic_context": {"current_head_revision": "revision_11"},
                    }),
                )

        replay = deepcopy(edit)
        replay["operation_request"]["expected_revision"] = "revision_12"
        replay["candidate"]["displayed_name"] = "Changed after receipt"
        self.assertEqual(
            ("replay_mismatch", "operation_request.payload_digest"),
            evaluate_semantic_failure({
                "instance": replay,
                "stored_receipt": {
                    "idempotency_key": replay["operation_request"]["idempotency_key"],
                    "payload_digest": replay["operation_request"]["payload_digest"],
                },
            }),
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

        for name in ("approval_request", "rejection_request"):
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
        invalid_type = deepcopy(proposal)
        invalid_type["diff"]["source_changes"][0]["change_type"] = "create"
        invalid_type["diff"]["source_changes"][0]["before_source"] = None
        self.assertFalse(source_snapshots_match(invalid_type["diff"]))

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

        duplicate_resolution = deepcopy(resolution)
        duplicate_resolution["resolutions"].append(
            deepcopy(duplicate_resolution["resolutions"][0])
        )
        self.assertEqual(
            ("proposal_validation_failure", "resolutions"),
            evaluate_semantic_failure({"instance": duplicate_resolution}),
        )

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
        _refresh_operation_payload_digest(correction)
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

    def test_removal_requests_bind_impact_and_resolution_permissions(self) -> None:
        removal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
        )
        removal["impact_binding"]["binding"]["record_id"] = "record-station"
        _refresh_operation_payload_digest(removal)
        self.assertEqual(
            ("proposal_validation_failure", "impact_binding.binding"),
            evaluate_semantic_failure({"instance": removal}),
        )

        removal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
        )
        removal["resolutions"][0]["action"] = "accept_unresolved"
        _refresh_operation_payload_digest(removal)
        self.assertEqual(
            ("incomplete_removal_resolution", "resolutions.0.action"),
            evaluate_semantic_failure({
                "instance": removal,
                "impact": {
                    "required_reference_id": "reference_station_company",
                    "permitted_unresolved": False,
                },
            }),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        removal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
        )
        removal["resolutions"] = []
        _refresh_operation_payload_digest(removal)
        self.assertEqual(
            ("incomplete_removal_resolution", "resolutions"),
            evaluate_semantic_failure({"instance": removal, "impact": impact}),
        )

        removal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
        )
        removal["resolutions"].append({
            "reference_id": "reference-extra",
            "action": "remove_reference",
            "replacement_target_record_id": None,
        })
        _refresh_operation_payload_digest(removal)
        self.assertEqual(
            ("unsafe_binding", "resolutions"),
            evaluate_semantic_failure({"instance": removal, "impact": impact}),
        )

        for target in ("record-company", "record-missing"):
            removal = deepcopy(
                next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
            )
            removal["resolutions"][0]["replacement_target_record_id"] = target
            _refresh_operation_payload_digest(removal)
            with self.subTest(target=target):
                self.assertEqual(
                    (
                        "proposal_validation_failure",
                        "resolutions.0.replacement_target_record_id",
                    ),
                    evaluate_semantic_failure({
                        "instance": removal,
                        "impact": impact,
                        "semantic_context": {
                            "available_record_ids": ["record-station", "record-ship"]
                        },
                    }),
                )

        removal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "remove_record_request")
        )
        _refresh_operation_payload_digest(removal)
        self.assertIsNone(evaluate_semantic_failure({
            "instance": removal,
            "impact": impact,
            "semantic_context": {
                "available_record_ids": ["record-station", "record-ship"]
            },
        }))

        empty_impact = {"incoming_references": []}
        self.assertEqual(
            ("unsafe_binding", "resolutions"),
            evaluate_semantic_failure({"instance": removal, "impact": empty_impact}),
        )

        removal["impact_digest"] = "f" * 64
        removal["impact_binding"]["impact_digest"] = "f" * 64
        _refresh_operation_payload_digest(removal)
        self.assertEqual(
            ("proposal_validation_failure", "impact_digest"),
            evaluate_semantic_failure({"instance": removal, "impact": impact}),
        )

    def test_corrections_bind_prior_proposal_identity(self) -> None:
        correction = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        context = {
            "prior_proposal": {
                "proposal_id": "proposal_editor",
                "proposal_version": 1,
                "mutation_kind": "edit",
                "record_id": "record-station",
            }
        }
        correction["prior_proposal"]["proposal_version"] = 2
        _refresh_operation_payload_digest(correction)
        self.assertEqual(
            ("invalid_correction", "prior_proposal"),
            evaluate_semantic_failure({"instance": correction, "semantic_context": context}),
        )

        correction = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        correction["prior_proposal"]["proposal_id"] = "proposal_other"
        correction["operation_request"]["subject_id"] = "proposal_other"
        _refresh_operation_payload_digest(correction)
        self.assertEqual(
            ("invalid_correction", "prior_proposal"),
            evaluate_semantic_failure({"instance": correction, "semantic_context": context}),
        )

    def test_corrections_accept_a_loaded_proposal_as_prior_scope(self) -> None:
        correction = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        prior_proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        self.assertIsNone(
            evaluate_semantic_failure({
                "instance": correction,
                "semantic_context": {"prior_proposal": prior_proposal},
            })
        )

    def test_removal_corrections_bind_to_the_removed_prior_record(self) -> None:
        correction = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "correction_request")
        )
        prior_proposal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        correction["prior_proposal"] = {
            "proposal_id": prior_proposal["proposal_id"],
            "proposal_version": prior_proposal["proposal_version"],
        }
        correction["operation_request"]["subject_id"] = prior_proposal["proposal_id"]
        correction["mutation_kind"] = "remove"
        correction["candidate"] = None
        correction["binding"]["record_id"] = "record-station"
        _refresh_operation_payload_digest(correction)
        self.assertEqual(
            ("invalid_correction", "prior_proposal"),
            evaluate_semantic_failure({
                "instance": correction,
                "semantic_context": {"prior_proposal": prior_proposal},
            }),
        )

    def test_corrected_proposals_advance_the_referenced_version(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["correction_of"] = {
            "proposal_id": proposal["proposal_id"],
            "proposal_version": proposal["proposal_version"],
        }
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("unsafe_binding", "proposal_version"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal["correction_of"]["proposal_id"] = "proposal_other"
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("unsafe_binding", "correction_of.proposal_id"),
            evaluate_semantic_failure({"instance": proposal}),
        )

        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        proposal["proposal_version"] = 2
        proposal["core_proposal"]["proposal"]["proposal_version"] = 2
        proposal["correction_of"] = {
            "proposal_id": proposal["proposal_id"],
            "proposal_version": 1,
        }
        proposal["core_proposal"]["proposal"]["correction_of_version"] = 99
        proposal["proposal_payload_digest"] = projection_digest(
            proposal,
            DIGEST_PROJECTIONS["proposal_payload_digest"],
        )
        self.assertEqual(
            ("unsafe_binding", "core_proposal.proposal.correction_of_version"),
            evaluate_semantic_failure({"instance": proposal}),
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
        impact["record"]["content_digest"] = record_content_digest(impact["record"])
        impact["binding"]["record_digest"] = impact["record"]["content_digest"]
        impact["impact_digest"] = projection_digest(
            impact,
            DIGEST_PROJECTIONS["impact_digest"],
        )
        self.assertIsNone(evaluate_semantic_failure({"instance": impact}))

        impact["outgoing_connections"] = []
        self.assertEqual(
            ("mutation_consistency", "outgoing_connections"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact["outgoing_connections"] = [deepcopy(connection), deepcopy(connection)]
        self.assertEqual(
            ("mutation_consistency", "outgoing_connections"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact["record"]["connections"] = [deepcopy(connection), deepcopy(connection)]
        self.assertEqual(
            ("proposal_validation_failure", "record.connections.1.connection_id"),
            evaluate_semantic_failure({"instance": impact}),
        )

    def test_removal_impact_record_uses_record_invariants(self) -> None:
        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["record"]["sections"].append(deepcopy(impact["record"]["sections"][0]))
        self.assertEqual(
            ("proposal_validation_failure", "record.sections.1.section_id"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["record"]["authority"] = "preparation"
        self.assertEqual(
            ("proposal_validation_failure", "record.authority"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["incoming_references"][0]["permitted_unresolved"] = True
        self.assertEqual(
            (
                "proposal_validation_failure",
                "incoming_references.0.permitted_unresolved",
            ),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["incoming_references"][0]["target_record_id"] = "record-station"
        self.assertEqual(
            (
                "proposal_validation_failure",
                "incoming_references.0.target_record_id",
            ),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["binding"]["record_id"] = "record-station"
        self.assertEqual(
            ("proposal_validation_failure", "binding.record_id"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        impact["binding"]["record_digest"] = "f" * 64
        self.assertEqual(
            ("proposal_validation_failure", "binding.record_digest"),
            evaluate_semantic_failure({"instance": impact}),
        )

        impact = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "removal_impact")
        )
        adapter_definition = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "head_record_view")[
                "adapter_definition"
            ]
        )
        impact["record"]["record_type"] = "vehicle"
        impact["record"]["content_digest"] = record_content_digest(impact["record"])
        impact["binding"]["record_digest"] = impact["record"]["content_digest"]
        impact["impact_digest"] = projection_digest(
            impact,
            DIGEST_PROJECTIONS["impact_digest"],
        )
        self.assertEqual(
            ("proposal_validation_failure", "record.record_type"),
            evaluate_semantic_failure({
                "instance": impact,
                "semantic_context": {"adapter_definition": adapter_definition},
            }),
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
        self.assertEqual(
            canonical_digest({"nested": {"body": "a\r\nb", "items": ["c\rd"]}}),
            canonical_digest({"nested": {"body": "a\nb", "items": ["c\nd"]}}),
        )

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
        self.assertIsNone(
            evaluate_semantic_failure({
                "instance": context,
                "semantic_context": {
                    "current_head_revision": "revision_empty",
                    "current_editor_workflow_version": 1,
                },
            })
        )
        self.assertEqual(
            ("workflow_conflict", "editor_workflow_version"),
            evaluate_semantic_failure({
                "instance": context,
                "semantic_context": {
                    "current_head_revision": "revision_empty",
                    "current_editor_workflow_version": 2,
                },
            }),
        )
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

        invalid = deepcopy(context)
        invalid["head_revision"] = {
            **invalid["head_revision"],
            "revision_id": "revision_2",
            "ordinal": 2,
        }
        self.assertEqual(
            ("unsafe_binding", "viewed_revision"),
            evaluate_semantic_failure({"instance": invalid}),
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

    def test_reference_resolution_cards_bind_resolution_to_after(self) -> None:
        proposal = deepcopy(
            next(
                item["payload"]
                for item in self.examples
                if item["name"] == "removal_proposal_with_outgoing_connections"
            )
        )
        card = next(
            card
            for card in proposal["diff"]["cards"]
            if card["kind"] == "reference_resolution"
        )

        card["resolution"] = None
        self.assertTrue(list(self.validator.iter_errors(proposal)))

        card["resolution"] = deepcopy(card["after"])
        card["resolution"]["action"] = "remove_reference"
        card["resolution"]["replacement_target_record_id"] = None
        self.assertEqual(
            ("unsafe_binding", "diff.cards.2.resolution"),
            evaluate_semantic_failure({"instance": proposal}),
        )

    def test_source_snapshots_normalize_null_field_values(self) -> None:
        proposal = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "editor_proposal_view")
        )
        card = proposal["diff"]["cards"][0]
        for side in ("before", "after"):
            next(field for field in card[side]["fields"] if field["field_id"] == "date")["value"] = None
        for source in proposal["diff"]["source_changes"]:
            for side in ("before_source", "after_source"):
                source[side] = source[side].replace("date: 2187-04-03", "date:")
        self.assertTrue(source_snapshots_match(proposal["diff"]))

    def test_source_snapshots_match_raw_before_statuses(self) -> None:
        proposal = next(
            item["payload"] for item in self.examples if item["name"] == "editor_proposal_view"
        )
        for status, source_status in (
            ({"classification": "unknown", "value": "legacy-state"}, "legacy-state"),
            ({"classification": "missing", "value": None}, None),
        ):
            candidate = deepcopy(proposal)
            card = candidate["diff"]["cards"][0]
            source = candidate["diff"]["source_changes"][0]
            card["before"]["status"] = status
            card["before"]["authority"] = "preparation"
            if source_status is None:
                source["before_source"] = source["before_source"].replace(
                    "status: review\n", ""
                )
            else:
                source["before_source"] = source["before_source"].replace(
                    "status: review", f"status: {source_status}"
                )
            with self.subTest(status=status):
                self.assertTrue(source_snapshots_match(candidate["diff"]))

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
                    "change_type": "update",
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

    def test_approval_diff_binding_matches_top_level_duplicates(self) -> None:
        approval = next(item["payload"] for item in self.examples if item["name"] == "approval_request")
        for key in (
            "diff_digest",
            "confirmed_change_ids",
            "confirmed_authority_change_ids",
            "confirmed_visibility_change_ids",
        ):
            invalid = deepcopy(approval)
            invalid["diff"][key] = "different" if key == "diff_digest" else []
            with self.subTest(key=key):
                self.assertEqual(
                    ("proposal_approval_conflict", f"diff.{key}"),
                    evaluate_semantic_failure({"instance": invalid}),
                )


if __name__ == "__main__":
    unittest.main()
