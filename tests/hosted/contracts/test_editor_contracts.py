from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import unittest

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[3]
HTTP_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http"
EDITOR_ROOT = HTTP_ROOT / "editor" / "v1"


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_normalize_text(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _normalize_text(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, list):
        return [_normalize_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_text(item) for key, item in value.items()}
    return value


def content_digest(document: dict) -> str:
    return canonical_digest(
        _normalize_text({key: value for key, value in document.items() if key != "content_digest"})
    )


def validation_digest(validation: dict) -> str:
    return canonical_digest(
        {key: validation[key] for key in ("status", "error_count", "findings")}
    )


class EditorSemanticError(ValueError):
    def __init__(self, category: str, path: str):
        self.category = category
        self.path = path
        super().__init__(f"{category}: {path}")


def _operation_digest(payload: dict) -> str:
    excluded = {"contract_name", "contract_version", "operation_request", "request_id", "idempotency_key", "payload_digest"}
    return canonical_digest({key: value for key, value in payload.items() if key not in excluded})


def _require_equal(left: object, right: object, category: str, path: str) -> None:
    if left != right:
        raise EditorSemanticError(category, path)


def _require_unique_ids(
    items: list[dict], identifier: str, path: str, *, category: str = "proposal_validation_failure"
) -> None:
    values = [item[identifier] for item in items]
    if len(values) != len(set(values)):
        raise EditorSemanticError(category, path)


def _expected_authority(status: str) -> str:
    return {"canon": "canon", "revealed": "revealed"}.get(status, "preparation")


def _validate_record_document(document: dict, *, path: str = "record") -> None:
    if content_digest(document) != document["content_digest"]:
        raise EditorSemanticError("idempotency_digest_conflict", f"{path}.content_digest")
    if document["authority"] != _expected_authority(document["status"]):
        raise EditorSemanticError("invalid_authority_transition", f"{path}.authority")
    for collection, identifier in (("fields", "field_id"), ("sections", "section_id"), ("connections", "connection_id")):
        _require_unique_ids(document[collection], identifier, f"{path}.{collection}.{identifier}")


def _expected_property_changes(before: dict, after: dict) -> list[dict]:
    changes: list[dict] = []
    for property_name in ("displayed_name", "status", "authority", "visibility"):
        if before[property_name] != after[property_name]:
            changes.append({"property": property_name, "before": before[property_name], "after": after[property_name]})
    for collection, identifier in (("fields", "field_id"), ("sections", "section_id")):
        old = {item[identifier]: item for item in before[collection]}
        new = {item[identifier]: item for item in after[collection]}
        for item_id in sorted(set(old) | set(new)):
            old_value = old.get(item_id, {}).get("value" if collection == "fields" else "body")
            new_value = new.get(item_id, {}).get("value" if collection == "fields" else "body")
            if old_value != new_value:
                changes.append({
                    "property": f"{collection}.{item_id}",
                    "before": old_value,
                    "after": new_value,
                })
    return changes


def _resolved_schema_fragment(fragment: object, root: dict) -> object:
    if isinstance(fragment, dict):
        if "$ref" in fragment:
            reference = fragment["$ref"]
            value = root
            for token in reference[2:].split("/"):
                value = value[token.replace("~1", "/").replace("~0", "~")]
            return _resolved_schema_fragment(value, root)
        return {
            key: _resolved_schema_fragment(value, root)
            for key, value in fragment.items()
            if key not in {"$schema", "$id", "title", "description", "x-invariants"}
        }
    if isinstance(fragment, list):
        return [_resolved_schema_fragment(item, root) for item in fragment]
    return fragment


def validate_card_semantics(card: dict, *, existing_record_ids: set[str] | None = None) -> None:
    for key in ("before", "after"):
        if isinstance(card.get(key), dict) and "content_digest" in card[key]:
            _validate_record_document(card[key], path=f"card.{card['change_id']}.{key}")
    expected_shapes = {
        "record_created": (None, "record", None, None),
        "record_updated": ("record", "record", None, None),
        "record_removed": ("record", None, None, None),
        "connection_added": (None, None, "connection", None),
        "connection_updated": (None, None, "connection_change", None),
        "connection_removed": (None, None, "connection", None),
        "reference_resolution": ("reference", "resolution", None, "resolution"),
    }
    before_kind, after_kind, connection_kind, resolution_kind = expected_shapes[card["kind"]]
    if before_kind == "record" and card["before"]["record_id"] != card["subject_record_id"]:
        raise EditorSemanticError("unsafe_binding", "card.before.record_id")
    if after_kind == "record" and card["after"]["record_id"] != card["subject_record_id"]:
        raise EditorSemanticError("unsafe_binding", "card.after.record_id")
    if before_kind == "reference" and card["before"]["source_record_id"] != card["subject_record_id"]:
        raise EditorSemanticError("unsafe_binding", "card.before.source_record_id")
    if after_kind == "resolution" and card["after"]["reference_id"] != card["before"]["reference_id"]:
        raise EditorSemanticError("unsafe_binding", "card.after.reference_id")
    if connection_kind == "connection_change":
        _require_equal(card["connection"]["before"]["connection_id"], card["connection"]["after"]["connection_id"], "unsafe_binding", "connection.connection_id")
        if card["connection"]["before"] == card["connection"]["after"]:
            raise EditorSemanticError("proposal_validation_failure", "connection no-op")
    if card["kind"] == "reference_resolution":
        _require_equal(card["resolution"], card["after"], "unsafe_binding", "reference resolution")
        resolution = card["resolution"]
        if resolution["action"] == "accept_unresolved" and not card["before"]["permitted_unresolved"]:
            raise EditorSemanticError("proposal_validation_failure", "resolution.permitted_unresolved")
        if resolution["action"] == "redirect":
            target = resolution["replacement_target_record_id"]
            if target in {card["before"]["target_record_id"], card["before"]["source_record_id"]}:
                raise EditorSemanticError("proposal_validation_failure", "resolution.redirect")
            if existing_record_ids is not None and target not in existing_record_ids:
                raise EditorSemanticError("proposal_validation_failure", "resolution.redirect")
        expected_backlinks = []
        if resolution["action"] == "redirect":
            expected_backlinks = [{
                "source_record_id": card["before"]["source_record_id"],
                "target_record_id": resolution["replacement_target_record_id"],
                "connection_id": card["before"]["connection_id"],
                "effect": "updated",
            }]
        else:
            expected_backlinks = [{
                "source_record_id": card["before"]["source_record_id"],
                "target_record_id": card["before"]["target_record_id"],
                "connection_id": card["before"]["connection_id"],
                "effect": "removed",
            }]
        _require_equal(card["derived_backlinks"], expected_backlinks, "unsafe_binding", "reference resolution backlinks")
    _require_unique_ids(card["property_changes"], "property", f"card.{card['change_id']}.property_changes.property")


def _validate_resolutions(
    resolutions: list[dict],
    references: dict[str, dict],
    *,
    existing_record_ids: set[str] | None = None,
    forbidden_target_ids: set[str] | None = None,
) -> None:
    _require_unique_ids(resolutions, "reference_id", "resolutions", category="incomplete_removal_resolution")
    resolution_ids = [item["reference_id"] for item in resolutions]
    if (
        len(resolution_ids) != len(set(resolution_ids))
        or set(resolution_ids) != set(references)
        or len(resolution_ids) != len(references)
    ):
        raise EditorSemanticError("incomplete_removal_resolution", "resolutions")
    for resolution in resolutions:
        reference = references[resolution["reference_id"]]
        if resolution["action"] == "accept_unresolved" and not reference["permitted_unresolved"]:
            raise EditorSemanticError("proposal_validation_failure", "resolutions.action")
        if resolution["action"] == "redirect":
            target = resolution["replacement_target_record_id"]
            if target in {reference.get("source_record_id"), reference.get("target_record_id")} | (forbidden_target_ids or set()):
                raise EditorSemanticError("proposal_validation_failure", "resolutions.replacement_target_record_id")
            if existing_record_ids is not None and target not in existing_record_ids:
                raise EditorSemanticError("invalid_connections", "resolutions.replacement_target_record_id")


def _validate_connection_cards(cards: list[dict]) -> None:
    _require_unique_ids(cards, "change_id", "diff.cards.change_id")
    for card in cards:
        for key in ("before", "after"):
            if isinstance(card.get(key), dict) and "content_digest" in card[key]:
                _validate_record_document(card[key], path=f"card.{card['change_id']}.{key}")
    record_cards_by_subject: dict[str, list[dict]] = {}
    for card in cards:
        if card["kind"] in {"record_created", "record_updated", "record_removed"}:
            record_cards_by_subject.setdefault(card["subject_record_id"], []).append(card)
    record_cards = {
        subject_id: subject_cards[0]
        for subject_id, subject_cards in record_cards_by_subject.items()
        if len(subject_cards) == 1
    }
    if len(record_cards) != len(record_cards_by_subject):
        raise EditorSemanticError("unsafe_binding", "connection card has multiple resulting records")
    expected_cards: dict[tuple[str, str, str], tuple[dict, list[dict]]] = {}
    for subject_id, record_card in record_cards.items():
        before = record_card.get("before") if isinstance(record_card.get("before"), dict) else {}
        after = record_card.get("after") if isinstance(record_card.get("after"), dict) else {}
        before_connections = {item["connection_id"]: item for item in before.get("connections", [])}
        after_connections = {item["connection_id"]: item for item in after.get("connections", [])}
        for connection_id in sorted(set(before_connections) | set(after_connections)):
            old = before_connections.get(connection_id)
            new = after_connections.get(connection_id)
            if old is None:
                kind, connection = "connection_added", new
                effects = [{"source_record_id": subject_id, "target_record_id": new["target_record_id"], "connection_id": connection_id, "effect": "added"}]
            elif new is None:
                kind, connection = "connection_removed", old
                effects = [{"source_record_id": subject_id, "target_record_id": old["target_record_id"], "connection_id": connection_id, "effect": "removed"}]
            elif old != new:
                kind, connection = "connection_updated", {"before": old, "after": new}
                if old["target_record_id"] == new["target_record_id"]:
                    effects = [{"source_record_id": subject_id, "target_record_id": new["target_record_id"], "connection_id": connection_id, "effect": "updated"}]
                else:
                    effects = [
                        {"source_record_id": subject_id, "target_record_id": old["target_record_id"], "connection_id": connection_id, "effect": "removed"},
                        {"source_record_id": subject_id, "target_record_id": new["target_record_id"], "connection_id": connection_id, "effect": "added"},
                    ]
            else:
                continue
            expected_cards[(subject_id, kind, connection_id)] = (connection, effects)

    actual_cards: dict[tuple[str, str, str], dict] = {}
    for card in cards:
        if card["kind"] not in {"connection_added", "connection_updated", "connection_removed"}:
            continue
        connection = card["connection"]
        connection_id = connection["connection_id"] if card["kind"] != "connection_updated" else connection["before"]["connection_id"]
        key = (card["subject_record_id"], card["kind"], connection_id)
        if key in actual_cards:
            raise EditorSemanticError("proposal_validation_failure", "diff.cards.change_id")
        actual_cards[key] = card
    if set(actual_cards) != set(expected_cards):
        raise EditorSemanticError("unsafe_binding", "connection cards do not match record before/after delta")
    for key, card in actual_cards.items():
        expected_connection, expected_backlinks = expected_cards[key]
        _require_equal(card["connection"], expected_connection, "unsafe_binding", f"connection card {card['change_id']} connection")
        _require_equal(card["derived_backlinks"], expected_backlinks, "unsafe_binding", f"connection card {card['change_id']} backlinks")
    for card in cards:
        if card["kind"] == "record_updated":
            if card["before"] == card["after"]:
                raise EditorSemanticError("proposal_validation_failure", f"record card {card['change_id']} is a no-op")
            _require_equal(card["property_changes"], _expected_property_changes(card["before"], card["after"]), "proposal_validation_failure", f"record card {card['change_id']} property_changes")


def validate_editor_semantics(
    payload: dict,
    *,
    proposal: dict | None = None,
    current_head: dict | None = None,
    current_workflow_version: int | None = None,
    record_digest_at_base: str | None = None,
    stored_receipt: dict | None = None,
    required_reference_ids: set[str] | None = None,
    existing_record_ids: set[str] | None = None,
    impact: dict | None = None,
) -> None:
    name = payload.get("contract_name")
    if name == "error_response":
        if set(payload) != {"contract_name", "contract_version", "error"} or payload["contract_version"] != 2:
            raise EditorSemanticError("unsafe_binding", "error")
        return
    if name == "editor_record_view":
        if payload["historical"] != (payload["viewed_revision"] != payload["head_revision"]):
            raise EditorSemanticError("unsafe_binding", "historical")
        if payload["editable"] != (not payload["historical"]):
            raise EditorSemanticError("unsafe_binding", "editable")
        _validate_record_document(payload["record"])
        return
    if name == "editor_removal_impact":
        binding = payload["binding"]
        if binding["record_id"] != payload["record"]["record_id"] or binding["record_digest"] != payload["record"]["content_digest"]:
            raise EditorSemanticError("unsafe_binding", "binding.record_id")
        if payload["impact_digest"] != canonical_digest({key: payload[key] for key in ("record", "outgoing_connections", "incoming_references")}):
            raise EditorSemanticError("idempotency_digest_conflict", "impact_digest")
        _validate_record_document(payload["record"])
        _require_unique_ids(payload["outgoing_connections"], "connection_id", "outgoing_connections.connection_id")
        _require_unique_ids(payload["incoming_references"], "reference_id", "incoming_references.reference_id")
        _require_equal(payload["outgoing_connections"], payload["record"]["connections"], "unsafe_binding", "outgoing_connections")
        return
    if name in {"editor_record_create_request", "editor_record_edit_request", "editor_record_remove_request", "editor_proposal_correction_request", "editor_proposal_rejection_request", "editor_proposal_approval_request"}:
        operation = payload["operation_request"]
        for key in ("request_id", "idempotency_key"):
            if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", operation[key]) is None:
                raise EditorSemanticError("unsafe_binding", f"operation_request.{key}")
        binding = payload.get("binding")
        if binding:
            _require_equal(
                operation["expected_revision"],
                binding["base_revision"]["revision_id"],
                "unsafe_binding",
                "operation_request.expected_revision",
            )
            _require_equal(
                operation["expected_editor_workflow_version"],
                binding["expected_editor_workflow_version"],
                "unsafe_binding",
                "workflow binding",
            )
            if current_head is not None and binding["base_revision"] != current_head:
                raise EditorSemanticError("stale_revision", "binding.base_revision")
            if current_workflow_version is not None and binding["expected_editor_workflow_version"] != current_workflow_version:
                raise EditorSemanticError("workflow_conflict", "expected_editor_workflow_version")
            if record_digest_at_base is not None and binding["record_digest"] != record_digest_at_base:
                raise EditorSemanticError("stale_record_digest", "binding.record_digest")
            if name in {"editor_record_create_request", "editor_record_edit_request", "editor_record_remove_request"}:
                _require_equal(operation.get("subject_id"), binding["record_id"], "unsafe_binding", "operation_request.subject_id")
            if name == "editor_proposal_correction_request":
                if payload["candidate"] is not None:
                    _require_equal(payload["candidate"]["record_id"], binding["record_id"], "invalid_correction", "prior_proposal")
                if proposal is not None:
                    _require_equal(operation.get("subject_id"), proposal["proposal_id"], "unsafe_binding", "operation_request.subject_id")
            if "candidate" in payload and payload["candidate"] is not None:
                _require_equal(payload["candidate"]["record_id"], binding["record_id"], "unsafe_binding", "candidate.record_id")
                if content_digest(payload["candidate"]) != payload["candidate"]["content_digest"]:
                    raise EditorSemanticError("idempotency_digest_conflict", "candidate.content_digest")
                _validate_record_document(payload["candidate"], path="candidate")
                expected_authority = _expected_authority(payload["candidate"]["status"])
                if payload["candidate"]["authority"] != expected_authority:
                    raise EditorSemanticError("invalid_authority_transition", "candidate.authority")
                if existing_record_ids is not None:
                    for index, connection in enumerate(payload["candidate"]["connections"]):
                        if connection["target_record_id"] not in existing_record_ids:
                            raise EditorSemanticError("invalid_connections", f"candidate.connections.{index}.target_record_id")
            if name == "editor_record_remove_request":
                if binding["record_digest"] is None:
                    raise EditorSemanticError("unsafe_binding", "binding.record_digest")
                if required_reference_ids is not None and {item["reference_id"] for item in payload["resolutions"]} != required_reference_ids:
                    raise EditorSemanticError("incomplete_removal_resolution", "resolutions")
                if impact is None and required_reference_ids is None:
                    raise EditorSemanticError("unsafe_binding", "impact lookup")
                if impact is not None:
                    _require_equal(payload["impact_digest"], impact["impact_digest"], "unsafe_binding", "impact_digest")
                    _require_equal(payload["binding"], impact["binding"], "unsafe_binding", "binding")
                    _require_equal(payload["impact_binding"], {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}, "unsafe_binding", "impact_binding")
                    references = {item["reference_id"]: item for item in impact["incoming_references"]}
                    _validate_resolutions(
                        payload["resolutions"],
                        references,
                        existing_record_ids=existing_record_ids,
                        forbidden_target_ids={binding["record_id"]},
                    )
                elif required_reference_ids is not None:
                    references = {reference_id: {} for reference_id in required_reference_ids}
                    _validate_resolutions(payload["resolutions"], references)
                else:
                    for resolution in payload["resolutions"]:
                        if resolution["action"] == "redirect" and resolution["replacement_target_record_id"] == binding["record_id"]:
                            raise EditorSemanticError("invalid_connections", "resolutions.replacement_target_record_id")
            if name == "editor_record_create_request" and binding["record_digest"] is not None:
                raise EditorSemanticError("unsafe_binding", "binding.record_digest")
            if name in {"editor_record_edit_request", "editor_record_remove_request"} and binding["record_digest"] is None:
                raise EditorSemanticError("unsafe_binding", "binding.record_digest")
        if stored_receipt is not None and stored_receipt["idempotency_key"] == operation["idempotency_key"]:
            if stored_receipt["payload_digest"] != operation["payload_digest"]:
                raise EditorSemanticError("replay_mismatch", "operation_request.payload_digest")
        if operation["payload_digest"] != _operation_digest(payload):
            raise EditorSemanticError("idempotency_digest_conflict", "operation_request.payload_digest")
        if name == "editor_proposal_correction_request" and proposal:
            if payload["prior_proposal"] != {"proposal_id": proposal["proposal_id"], "proposal_version": proposal["proposal_version"]}:
                raise EditorSemanticError("invalid_correction", "prior_proposal")
            if payload["mutation_kind"] != proposal["mutation_kind"]:
                raise EditorSemanticError("invalid_correction", "prior_proposal")
            _require_equal(binding["campaign_id"], proposal["campaign_id"], "unsafe_binding", "binding.campaign_id")
            _require_equal(binding["base_revision"], proposal["base_revision"], "unsafe_binding", "binding.base_revision")
            _require_equal(
                binding["expected_editor_workflow_version"],
                proposal["editor_workflow_version"],
                "workflow_conflict",
                "binding.expected_editor_workflow_version",
            )
            if payload["candidate"] is not None and payload["candidate"]["record_id"] != proposal["record_bindings"][0]["record_id"]:
                raise EditorSemanticError("invalid_correction", "prior_proposal")
            expected_digest = None if payload["mutation_kind"] == "create" else proposal["record_bindings"][0]["record_digest"]
            _require_equal(binding["record_digest"], expected_digest, "unsafe_binding", "binding.record_digest")
            if payload["mutation_kind"] == "remove":
                if impact is None:
                    raise EditorSemanticError("unsafe_binding", "impact lookup")
                _require_equal(payload["impact_digest"], impact["impact_digest"], "unsafe_binding", "impact_digest")
                _require_equal(payload["binding"], impact["binding"], "unsafe_binding", "binding")
                _require_equal(payload["impact_binding"], {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}, "unsafe_binding", "impact_binding")
                references = {item["reference_id"]: item for item in impact["incoming_references"]}
                _validate_resolutions(
                    payload["resolutions"],
                    references,
                    existing_record_ids=existing_record_ids,
                    forbidden_target_ids={binding["record_id"]},
                )
        if name in {"editor_proposal_rejection_request", "editor_proposal_approval_request"}:
            if proposal is None:
                raise EditorSemanticError("proposal_approval_conflict", "loaded proposal")
            _validate_action_binding(payload, proposal, current_head=current_head, current_workflow_version=current_workflow_version, impact=impact)
            if name == "editor_proposal_approval_request":
                if proposal["core_proposal"]["proposal"]["status"] != "needs_review" or proposal["publication"]["status"] != "not_published":
                    raise EditorSemanticError("proposal_approval_conflict", "proposal.status")
                if proposal["validation"]["status"] != "passed" or proposal["validation"]["error_count"] != 0:
                    raise EditorSemanticError("proposal_validation_failure", "validation.status")
                if payload["validation_status"] != "passed":
                    raise EditorSemanticError("proposal_validation_failure", "validation_status")
        return
    if name == "editor_proposal_view":
        _validate_proposal_equality(payload, impact=impact)


def _validate_proposal_equality(payload: dict, *, impact: dict | None = None) -> None:
    core = payload["core_proposal"]["proposal"]
    publication = payload["publication"]
    core_status = core["status"]
    publication_status = publication["status"]
    published_revision = publication["published_revision"]
    if core_status == "approved":
        if publication_status != "published" or published_revision is None:
            raise EditorSemanticError("proposal_approval_conflict", "publication")
    elif core_status == "approving":
        if publication_status not in {"not_published", "quarantined"} or published_revision is not None:
            raise EditorSemanticError("proposal_approval_conflict", "publication")
    elif core_status in {"draft", "needs_review", "rejected", "conflict"}:
        if publication_status != "not_published" or published_revision is not None:
            raise EditorSemanticError("proposal_approval_conflict", "publication")
    _require_unique_ids(core["changes"], "change_id", "core_proposal.proposal.changes.change_id")
    _require_unique_ids(payload["diff"]["cards"], "change_id", "diff.cards.change_id")
    _require_unique_ids(payload["record_bindings"], "record_id", "record_bindings.record_id")
    _require_unique_ids(payload["diff"]["authority_changes"], "change_id", "diff.authority_changes.change_id")
    _require_unique_ids(payload["diff"]["visibility_changes"], "change_id", "diff.visibility_changes.change_id")
    _require_unique_ids(payload["authority_outcome"], "change_id", "authority_outcome.change_id")
    _require_unique_ids(payload["visibility_outcome"], "change_id", "visibility_outcome.change_id")
    expected_core_values = {
        "expected_campaign_head": payload["base_revision"]["revision_id"],
        "expected_editor_workflow_version": payload["editor_workflow_version"],
        "authority_change_ids": [item["change_id"] for item in payload["diff"]["authority_changes"]],
        "visibility_change_ids": [item["change_id"] for item in payload["diff"]["visibility_changes"]],
    }
    for key, expected in expected_core_values.items():
        _require_equal(core[key], expected, "unsafe_binding", f"core_proposal.proposal.{key}")
    if payload["source_revision"]["revision_id"] != core["source_revision"] or payload["base_revision"]["revision_id"] != core["base_revision"] or payload["expected_campaign_head"]["revision_id"] != core["base_revision"]:
        raise EditorSemanticError("unsafe_binding", "proposal revision binding")
    if payload["base_revision"] != payload["expected_campaign_head"]:
        raise EditorSemanticError("unsafe_binding", "expected_campaign_head")
    if core["proposal_id"] != payload["proposal_id"] or core["proposal_version"] != payload["proposal_version"] or core["campaign_id"] != payload["campaign_id"]:
        raise EditorSemanticError("unsafe_binding", "proposal identity binding")
    if payload["diff"]["diff_digest"] != canonical_digest({key: payload["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")}):
        raise EditorSemanticError("idempotency_digest_conflict", "diff.diff_digest")
    if core["diff_digest"] != payload["diff"]["diff_digest"]:
        raise EditorSemanticError("unsafe_binding", "core_proposal.diff_digest")
    if payload["validation"]["status"] == "passed" and payload["validation"]["error_count"] != 0:
        raise EditorSemanticError("proposal_validation_failure", "validation.error_count")
    if payload["validation"]["validation_digest"] != validation_digest(payload["validation"]):
        raise EditorSemanticError("idempotency_digest_conflict", "validation.validation_digest")
    core_binding = payload["core_proposal"]["approval_binding"]
    if core_status in {"approving", "approved"} and core_binding is None:
        raise EditorSemanticError("proposal_approval_conflict", "core_proposal.approval_binding")
    if core_status not in {"approving", "approved"} and core_binding is not None:
        raise EditorSemanticError("proposal_approval_conflict", "core_proposal.approval_binding")
    if core_binding is not None:
        expected_binding = {
            "proposal_id": core["proposal_id"],
            "proposal_version": core["proposal_version"],
            "diff_digest": core["diff_digest"],
            "base_revision": core["base_revision"],
            "source_revision": core["source_revision"],
            "expected_campaign_head": core["expected_campaign_head"],
            "expected_editor_workflow_version": core["expected_editor_workflow_version"],
            "validation_status": payload["validation"]["status"],
            "validation_digest": payload["validation"]["validation_digest"],
            "authority_change_ids": core["authority_change_ids"],
            "visibility_change_ids": core["visibility_change_ids"],
            "warden_confirmed": True,
        }
        _require_equal(core_binding, expected_binding, "proposal_approval_conflict", "core_proposal.approval_binding")
    if core_status in {"needs_review", "approving", "approved"} and (payload["validation"]["status"], payload["validation"]["error_count"]) != ("passed", 0):
        raise EditorSemanticError("proposal_validation_failure", "validation.status")
    card_ids = {card["change_id"] for card in payload["diff"]["cards"]}
    change_ids = {change["change_id"] for change in core["changes"]}
    if card_ids != change_ids:
        raise EditorSemanticError("unsafe_binding", "proposal changes/diff cards")
    binding_ids = {binding["record_id"] for binding in payload["record_bindings"]}
    card_subject_ids = {card["subject_record_id"] for card in payload["diff"]["cards"]}
    if binding_ids != card_subject_ids:
        raise EditorSemanticError("unsafe_binding", "record_bindings.record_id")
    if len(payload["record_bindings"]) != len(binding_ids):
        raise EditorSemanticError("unsafe_binding", "record_bindings")
    for binding in payload["record_bindings"]:
        if binding["campaign_id"] != payload["campaign_id"] or binding["base_revision"] != payload["base_revision"] or binding["expected_editor_workflow_version"] != payload["editor_workflow_version"]:
            raise EditorSemanticError("unsafe_binding", "record binding")
    for card in payload["diff"]["cards"]:
        binding = next((item for item in payload["record_bindings"] if item["record_id"] == card["subject_record_id"]), None)
        if binding is None:
            raise EditorSemanticError("unsafe_binding", "record binding subject")
        if card["kind"] in {"record_updated", "record_removed"} and binding["record_digest"] != card["before"]["content_digest"]:
            raise EditorSemanticError("unsafe_binding", "record binding digest")
    _validate_connection_cards(payload["diff"]["cards"])
    actual_authority = {
        (card["change_id"], card["subject_record_id"], card["before"]["authority"], card["after"]["authority"])
        for card in payload["diff"]["cards"]
        if card["kind"] in {"record_created", "record_updated"}
        and card["before"] is not None and card["after"] is not None
        and card["before"]["authority"] != card["after"]["authority"]
    }
    declared_authority = {
        (item["change_id"], item["record_id"], item["from"], item["to"])
        for item in payload["diff"]["authority_changes"]
    }
    if actual_authority != declared_authority:
        raise EditorSemanticError("proposal_validation_failure", "diff.authority_changes")
    actual_visibility = {
        (
            card["change_id"],
            card["subject_record_id"],
            json.dumps(card["before"]["visibility"], sort_keys=True),
            json.dumps(card["after"]["visibility"], sort_keys=True),
        )
        for card in payload["diff"]["cards"]
        if card["kind"] in {"record_created", "record_updated"}
        and card["before"] is not None and card["after"] is not None
        and card["before"]["visibility"] != card["after"]["visibility"]
    }
    declared_visibility = {
        (
            item["change_id"],
            item["record_id"],
            json.dumps(item["before"], sort_keys=True),
            json.dumps(item["after"], sort_keys=True),
        )
        for item in payload["diff"]["visibility_changes"]
    }
    if actual_visibility != declared_visibility:
        raise EditorSemanticError("proposal_validation_failure", "diff.visibility_changes")
    if payload["authority_outcome"] != payload["diff"]["authority_changes"] or payload["visibility_outcome"] != payload["diff"]["visibility_changes"]:
        raise EditorSemanticError("unsafe_binding", "transition outcome")
    for change in payload["diff"]["authority_changes"]:
        card = next((card for card in payload["diff"]["cards"] if card["change_id"] == change["change_id"]), None)
        if card is None or card["before"] is None or card["after"] is None:
            raise EditorSemanticError("unsafe_binding", "authority change card")
        if change["record_id"] != card["subject_record_id"] or (change["from"], change["to"]) != (card["before"]["authority"], card["after"]["authority"]):
            raise EditorSemanticError("unsafe_binding", "authority change values")
    for change in payload["diff"]["visibility_changes"]:
        card = next((card for card in payload["diff"]["cards"] if card["change_id"] == change["change_id"]), None)
        if card is None or card["before"] is None or card["after"] is None:
            raise EditorSemanticError("unsafe_binding", "visibility change card")
        if change["record_id"] != card["subject_record_id"] or (change["before"], change["after"]) != (card["before"]["visibility"], card["after"]["visibility"]):
            raise EditorSemanticError("unsafe_binding", "visibility change values")
        expected_broadening = change["before"]["audience"] == "warden" and change["after"]["audience"] != "warden"
        if change["audience_broadens"] != expected_broadening:
            raise EditorSemanticError("unsafe_binding", "visibility change broadening")
    if payload["mutation_kind"] == "create":
        if any(binding["record_digest"] is not None for binding in payload["record_bindings"]):
            raise EditorSemanticError("unsafe_binding", "record_bindings.record_digest")
        if any(card["kind"] not in {"record_created", "connection_added"} for card in payload["diff"]["cards"]):
            raise EditorSemanticError("proposal_validation_failure", "diff.cards")
        if sum(card["kind"] == "record_created" for card in payload["diff"]["cards"]) != 1:
            raise EditorSemanticError("proposal_validation_failure", "diff.cards")
    elif payload["mutation_kind"] == "edit":
        if any(card["kind"] not in {"record_updated", "connection_added", "connection_updated", "connection_removed"} for card in payload["diff"]["cards"]):
            raise EditorSemanticError("proposal_validation_failure", "diff.cards")
        if any(binding["record_digest"] is None for binding in payload["record_bindings"]):
            raise EditorSemanticError("unsafe_binding", "record_bindings.record_digest")
    elif payload["mutation_kind"] == "remove":
        if any(card["kind"] not in {"record_removed", "reference_resolution", "connection_removed"} for card in payload["diff"]["cards"]):
            raise EditorSemanticError("proposal_validation_failure", "diff.cards")
        if any(binding["record_digest"] is None for binding in payload["record_bindings"]):
            raise EditorSemanticError("unsafe_binding", "record_bindings.record_digest")
        if payload["impact_digest"] is None or payload["diff"]["impact_digest"] != payload["impact_digest"]:
            raise EditorSemanticError("unsafe_binding", "impact_digest")
        if impact is None:
            raise EditorSemanticError("unsafe_binding", "impact lookup")
        _require_equal(payload["impact_binding"], {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}, "unsafe_binding", "impact_binding")
        _require_equal(payload["impact_digest"], impact["impact_digest"], "unsafe_binding", "impact_digest")
        references = {item["reference_id"]: item for item in impact["incoming_references"]}
        removed_card = next(card for card in payload["diff"]["cards"] if card["kind"] == "record_removed")
        _validate_resolutions(payload["resolutions"], references, forbidden_target_ids={removed_card["subject_record_id"]})
        resolution_cards = [card for card in payload["diff"]["cards"] if card["kind"] == "reference_resolution"]
        _require_unique_ids(resolution_cards, "change_id", "diff.cards.change_id")
        _require_unique_ids([card["before"] for card in resolution_cards], "reference_id", "diff.cards.before.reference_id")
        resolution_by_reference = {resolution["reference_id"]: resolution for resolution in payload["resolutions"]}
        card_by_reference = {card["before"]["reference_id"]: card for card in resolution_cards}
        if set(card_by_reference) != set(references) or set(resolution_by_reference) != set(references):
            raise EditorSemanticError("incomplete_removal_resolution", "diff.cards")
        for reference_id, reference in references.items():
            card = card_by_reference[reference_id]
            _require_equal(card["before"], reference, "unsafe_binding", f"diff.cards.{card['change_id']}.before")
            _require_equal(card["resolution"], resolution_by_reference[reference_id], "unsafe_binding", f"diff.cards.{card['change_id']}.resolution")
        _require_equal(removed_card["before"], impact["record"], "unsafe_binding", "record removal impact record")
        _require_equal(removed_card["before"]["connections"], impact["outgoing_connections"], "unsafe_binding", "record removal impact outgoing connections")
        expected_affected_ids = {removed_card["subject_record_id"]} | {
            reference["source_record_id"] for reference in references.values()
        }
        _require_equal(
            {binding["record_id"] for binding in payload["record_bindings"]},
            expected_affected_ids,
            "unsafe_binding",
            "record_bindings.record_id",
        )
        _require_equal(
            {card["subject_record_id"] for card in payload["diff"]["cards"]},
            expected_affected_ids,
            "unsafe_binding",
            "diff.cards.subject_record_id",
        )
        _require_equal(
            payload["diff"]["unresolved_reference_count"],
            sum(resolution["action"] == "accept_unresolved" for resolution in payload["resolutions"]),
            "proposal_validation_failure",
            "diff.unresolved_reference_count",
        )
        if sum(card["kind"] == "record_removed" for card in payload["diff"]["cards"]) != 1:
            raise EditorSemanticError("proposal_validation_failure", "diff.cards")
    if payload["proposal_payload_digest"] != canonical_digest({key: value for key, value in payload.items() if key != "proposal_payload_digest"}):
        raise EditorSemanticError("idempotency_digest_conflict", "proposal_payload_digest")
    core_validation = payload["core_proposal"]["validation"]
    if (core_validation["status"], core_validation["validation_digest"], core_validation["error_count"]) != (payload["validation"]["status"], payload["validation"]["validation_digest"], payload["validation"]["error_count"]):
        raise EditorSemanticError("unsafe_binding", "core_proposal.validation")
    for card in payload["diff"]["cards"]:
        matching = next(change for change in core["changes"] if change["change_id"] == card["change_id"])
        expected_change_type = {
            "record_created": "add",
            "record_removed": "remove",
            "record_updated": "update",
            "connection_added": "update",
            "connection_updated": "update",
            "connection_removed": "update",
            "reference_resolution": "update",
        }[card["kind"]]
        _require_equal(matching["change_type"], expected_change_type, "unsafe_binding", "core_proposal.changes.change_type")
        _require_equal(matching["subject_id"], card["subject_record_id"], "unsafe_binding", "core_proposal.changes.subject_id")
        document = card.get("after") if isinstance(card.get("after"), dict) and "content_digest" in card["after"] else card.get("before")
        if isinstance(document, dict) and "content_digest" in document:
            _require_equal(matching["content_digest"], document["content_digest"], "unsafe_binding", "core_proposal.changes.content_digest")
        validate_card_semantics(card, existing_record_ids={"record-company", "record-ship", "record-station", "record-new"})
    affected_record_ids = {card["subject_record_id"] for card in payload["diff"]["cards"]}
    _require_equal(payload["diff"]["affected_record_count"], len(affected_record_ids), "proposal_validation_failure", "diff.affected_record_count")
    if payload["mutation_kind"] != "remove":
        _require_equal(payload["impact_binding"], None, "unsafe_binding", "impact_binding")
        _require_equal(payload["resolutions"], [], "proposal_validation_failure", "resolutions")


def _validate_action_binding(payload: dict, proposal: dict, *, current_head: dict | None, current_workflow_version: int | None, impact: dict | None = None) -> None:
    _validate_proposal_equality(proposal, impact=impact)
    core = proposal["core_proposal"]["proposal"]
    for key in ("proposal_id", "proposal_version"):
        _require_equal(payload["proposal"][key], proposal[key], "proposal_approval_conflict", f"proposal.{key}")
    for key in ("source_revision", "base_revision", "expected_campaign_head", "proposal_payload_digest", "impact_digest", "impact_binding", "resolutions", "record_bindings", "authority_outcome", "visibility_outcome"):
        _require_equal(payload[key], proposal[key], "proposal_approval_conflict", f"{key}")
    _require_equal(payload["expected_editor_workflow_version"], proposal["editor_workflow_version"], "proposal_approval_conflict", "expected_editor_workflow_version")
    _require_equal(payload["diff_digest"], proposal["diff"]["diff_digest"], "proposal_approval_conflict", "diff_digest")
    if payload.get("diff") is not None:
        _require_equal(payload["diff"], proposal["diff"], "proposal_approval_conflict", "diff")
    _require_equal(payload["validation_status"], proposal["validation"]["status"], "proposal_approval_conflict", "validation_status")
    _require_equal(payload["validation_digest"], proposal["validation"]["validation_digest"], "proposal_approval_conflict", "validation_digest")
    _require_equal(payload["operation_request"]["subject_id"], proposal["proposal_id"], "proposal_approval_conflict", "operation_request.subject_id")
    _require_equal(payload["operation_request"]["intent_digest"], payload["diff_digest"], "proposal_approval_conflict", "operation_request.intent_digest")
    _require_equal(payload["operation_request"]["expected_revision"], payload["base_revision"]["revision_id"], "proposal_approval_conflict", "operation_request.expected_revision")
    _require_equal(payload["operation_request"]["expected_editor_workflow_version"], payload["expected_editor_workflow_version"], "unsafe_binding", "expected_editor_workflow_version")
    if current_head is not None and payload["expected_campaign_head"] != current_head:
        raise EditorSemanticError("stale_revision", "expected_campaign_head")
    if current_workflow_version is not None and payload["expected_editor_workflow_version"] != current_workflow_version:
        raise EditorSemanticError("workflow_conflict", "expected_editor_workflow_version")
    if payload["base_revision"] != payload["expected_campaign_head"]:
        raise EditorSemanticError("proposal_approval_conflict", "expected_campaign_head")
    if payload["proposal_status"] != core["status"]:
        raise EditorSemanticError("proposal_approval_conflict", "proposal_status")
    _require_equal(payload["mutation_kind"], proposal["mutation_kind"], "proposal_approval_conflict", "mutation_kind")
    if "affected_record_count" in payload:
        _require_equal(payload["affected_record_count"], len({card["subject_record_id"] for card in proposal["diff"]["cards"]}), "proposal_approval_conflict", "affected_record_count")
        _require_equal(payload["affected_record_count"], proposal["diff"]["affected_record_count"], "proposal_approval_conflict", "affected_record_count")
        _require_equal(payload["confirmed_change_ids"], [card["change_id"] for card in proposal["diff"]["cards"]], "proposal_approval_conflict", "confirmed_change_ids")
        _require_equal(payload["confirmed_authority_change_ids"], [change["change_id"] for change in proposal["diff"]["authority_changes"]], "proposal_approval_conflict", "confirmed_authority_change_ids")
        _require_equal(payload["confirmed_visibility_change_ids"], [change["change_id"] for change in proposal["diff"]["visibility_changes"]], "proposal_approval_conflict", "confirmed_visibility_change_ids")
    if payload.get("warden_confirmed") is not True:
        raise EditorSemanticError("proposal_approval_conflict", "warden_confirmed")


class EditorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = json.loads((EDITOR_ROOT / "index.json").read_text())
        cls.schema = json.loads((EDITOR_ROOT / "editor.schema.json").read_text())
        document = json.loads((EDITOR_ROOT / "examples.json").read_text())
        cls.examples = document["examples"]
        cls.card_examples = document["card_examples"]
        cls.lifecycle_examples = document["lifecycle_examples"]
        cls.by_name = {item["name"]: item["payload"] for item in cls.examples}

    def test_central_http_index_discovers_closed_editor_package(self):
        aggregate = json.loads((HTTP_ROOT / "index-v5.json").read_text())
        self.assertIn({"name":"hosted_record_editor_http_v1","index":"editor/v1/index.json"}, aggregate["packages"])
        self.assertEqual(
            ["v2/index.json", "atlas/v2/index.json", "live/v1/index.json", "editor/v1/index.json"],
            [item["index"] for item in aggregate["packages"]],
        )
        self.assertEqual(self.index["extends"]["proposal_contract"], "../../../schemas/v2/proposal.schema.json")
        self.assertEqual(self.index["extends"]["proposal_contract_version"], 2)
        legacy = json.loads((HTTP_ROOT / "index.json").read_text())
        self.assertEqual(4, legacy["contract_version"])
        self.assertNotIn("hosted_record_editor_http_v1", [item["name"] for item in legacy["packages"]])
        transport_schema = json.loads((HTTP_ROOT / "v2" / "http.schema.json").read_text())
        self.assertEqual(transport_schema["$defs"]["error"], self.schema["$defs"]["error"])
        self.assertEqual(transport_schema["$defs"]["error_response"], self.schema["$defs"]["error_response"])
        for item in aggregate["packages"]:
            package_index = json.loads((HTTP_ROOT / item["index"]).read_text())
            for key in ("schema", "routes", "examples", "semantic_invariants"):
                self.assertTrue((HTTP_ROOT / Path(item["index"]).parent / package_index[key]).is_file(), item["name"])
        for name in ("index.json", "editor.schema.json", "routes.json", "examples.json", "semantic-invariants.json"):
            self.assertTrue((EDITOR_ROOT / name).is_file())

    def test_registry_selection_is_coherent_and_legacy_registries_are_not_active(self):
        hosted = json.loads((ROOT / "docs/contracts/hosted/index-v2.json").read_text())
        http = json.loads((HTTP_ROOT / "index-v5.json").read_text())
        legacy_http = json.loads((HTTP_ROOT / "index.json").read_text())
        self.assertEqual("http/index-v5.json", hosted["http_registry"])
        self.assertTrue((ROOT / "docs/contracts/hosted" / hosted["http_registry"]).is_file())
        self.assertEqual("index.json", http["previous_registry"])
        self.assertEqual(
            {"hosted_http_v2", "hosted_atlas_http_v2", "hosted_live_http_v1", "hosted_record_editor_http_v1"},
            {item["name"] for item in http["packages"]},
        )
        self.assertNotIn("hosted_record_editor_http_v1", {item["name"] for item in legacy_http["packages"]})
        editor_entry = next(item for item in http["packages"] if item["name"] == "hosted_record_editor_http_v1")
        self.assertEqual("editor/v1/index.json", editor_entry["index"])
        editor = json.loads((HTTP_ROOT / editor_entry["index"]).read_text())
        self.assertEqual("../../../index-v2.json", editor["hosted_registry"])
        self.assertEqual("../../index-v5.json", editor["http_registry"])

    def test_current_versioned_registries_select_each_other_and_register_v2(self):
        hosted = json.loads((ROOT / "docs/contracts/hosted/index-v2.json").read_text())
        http = json.loads((HTTP_ROOT / "index-v5.json").read_text())
        editor = json.loads((EDITOR_ROOT / "index.json").read_text())
        self.assertEqual("http/index-v5.json", hosted["http_registry"])
        self.assertEqual("index.json", http["previous_registry"])
        self.assertEqual("../../../index-v2.json", editor["hosted_registry"])
        self.assertEqual("../../index-v5.json", editor["http_registry"])
        self.assertEqual(
            {"hosted_http_v2", "hosted_atlas_http_v2", "hosted_live_http_v1", "hosted_record_editor_http_v1"},
            {item["name"] for item in http["packages"]},
        )
        proposal = json.loads((ROOT / "docs/contracts/hosted/examples/v2/proposal.json").read_text())
        proposal_schema = json.loads((ROOT / "docs/contracts/hosted/schemas/v2/proposal.schema.json").read_text())
        self.assertEqual([], list(Draft202012Validator(proposal_schema).iter_errors(proposal)))

    def test_editor_core_proposal_matches_authoritative_v2_shape(self):
        editor = self.schema["$defs"]["core_proposal"]
        authoritative = json.loads((ROOT / "docs/contracts/hosted/schemas/v2/proposal.schema.json").read_text())
        self.assertEqual(
            _resolved_schema_fragment(editor, self.schema),
            _resolved_schema_fragment(
                {key: value for key, value in authoritative.items() if key != "$defs"},
                authoritative,
            ),
        )
        approval = authoritative["$defs"]["approval"]
        editor_approval = editor["properties"]["approval_binding"]["oneOf"][1]
        self.assertEqual(authoritative["required"], editor["required"])
        self.assertEqual(authoritative["properties"]["proposal"]["required"], editor["properties"]["proposal"]["required"])
        self.assertEqual(approval["required"], editor_approval["required"])
        self.assertEqual(set(approval["properties"]), set(editor_approval["properties"]))
        self.assertFalse(editor["additionalProperties"])
        self.assertFalse(editor_approval["additionalProperties"])

    def test_negative_categories_map_to_the_preserved_http_v2_error_envelope(self):
        routes = json.loads((EDITOR_ROOT / "routes.json").read_text())
        invariants = json.loads((EDITOR_ROOT / "semantic-invariants.json").read_text())
        error_schema = json.loads((HTTP_ROOT / "v2" / "http.schema.json").read_text())["$defs"]["error"]
        categories = set(error_schema["properties"]["category"]["enum"])
        mapping = routes["rules"]["error_category_mapping"]
        self.assertEqual(mapping, invariants["error_category_mapping"] | {"unsafe_binding":"unsafe_binding"})
        for path in (EDITOR_ROOT / "negative").glob("*.json"):
            fixture = json.loads(path.read_text())
            public_category = mapping.get(fixture["expected_category"], fixture["expected_category"])
            self.assertIn(public_category, categories, path.name)
            error = {"contract_name":"error_response","contract_version":2,"error":{"category":public_category,"code":"editor_rejected","stage":"validate","request_id":"request_editor","retryable":False}}
            self.assertEqual([], list(Draft202012Validator(self.schema).iter_errors(error)), path.name)

    def test_service_unavailable_is_only_a_503_editor_route_error(self):
        routes = json.loads((EDITOR_ROOT / "routes.json").read_text())
        for route in routes["routes"]:
            statuses = route["error_status"]
            with self.subTest(route=route["id"]):
                self.assertIn("service_unavailable", statuses.get("503", []))
                self.assertNotIn("service_unavailable", statuses.get("409", []))

    def test_authoritative_proposal_v2_accepts_hyphenated_ids_and_rejects_v1(self):
        path = ROOT / "docs/contracts/hosted/schemas/v2/proposal.schema.json"
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        registry = json.loads((ROOT / "docs/contracts/hosted/index-v2.json").read_text())
        self.assertEqual(
            {"name":"canon_proposal","version":2,"schema":"schemas/v2/proposal.schema.json","semantic_invariants":"schemas/v2/semantic-invariants.json","example":"examples/v2/proposal.json","negative_fixtures":["negative/v2/proposal-approval-binding.json","negative/v2/proposal-approval-validation.json","negative/v2/proposal-approval-state.json","negative/v2/proposal-duplicate-logical-id.json"],"authority_owner":"proposal_service"},
            registry["versioned_contracts"][0],
        )
        value = {"contract_name":"canon_proposal","contract_version":2,"draft":{"draft_id":"draft_alpha","authority":"draft","source_set_digest":"a"*64,"content_digest":"b"*64},"proposal":{"proposal_id":"proposal_alpha","proposal_version":1,"status":"draft","campaign_id":"campaign_alpha","base_revision":"revision_12","source_revision":"revision_12","expected_campaign_head":"revision_12","expected_editor_workflow_version":7,"diff_digest":"c"*64,"authority_change_ids":[],"visibility_change_ids":[],"changes":[{"change_id":"change_one","subject_id":"record-station","change_type":"update","from_authority":"preparation","to_authority":"preparation","content_digest":"d"*64}]},"validation":{"status":"passed","validation_digest":"e"*64,"error_count":0},"approval_binding":None}
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(value)))
        value["proposal"]["changes"][0]["subject_id"] = "entity-one"
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(value)))
        value["proposal"]["changes"][0]["subject_id"] = "x"
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(value)))
        value["contract_version"] = 1
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(value)))
        v1 = json.loads((ROOT / "docs/contracts/hosted/schemas/v1/proposal.schema.json").read_text())
        self.assertTrue(list(Draft202012Validator(v1).iter_errors({**value, "contract_version": 2})))

    def test_editor_domain_id_grammar_is_identical_to_atlas(self):
        atlas = json.loads((HTTP_ROOT / "atlas" / "v2" / "atlas.schema.json").read_text())
        self.assertEqual(self.schema["$defs"]["domain_id"], atlas["$defs"]["domain_id"])
        proposal = json.loads((ROOT / "docs/contracts/hosted/schemas/v2/proposal.schema.json").read_text())
        self.assertEqual(self.schema["$defs"]["domain_id"], proposal["$defs"]["domain_id"])
        validator = Draft202012Validator({"$ref": "#/$defs/domain_id", "$defs": self.schema["$defs"]})
        for value in ("x", "1", "record-station"):
            self.assertEqual([], list(validator.iter_errors(value)), value)
        for value in ("/tmp/record", "../record", "record_station"):
            self.assertTrue(list(validator.iter_errors(value)), value)

    def test_schema_declared_invariants_have_exact_semantic_registry_parity(self):
        invariants = json.loads((EDITOR_ROOT / "semantic-invariants.json").read_text())
        self.assertEqual(self.schema["x-invariants"], [rule["id"] for rule in invariants["rules"]])

    def test_all_positive_examples_are_schema_and_semantically_valid(self):
        Draft202012Validator.check_schema(self.schema)
        validator = Draft202012Validator(self.schema)
        for item in self.examples:
            with self.subTest(example=item["name"]):
                self.assertEqual([], list(validator.iter_errors(item["payload"])))
                if item["payload"].get("contract_name") in {"editor_proposal_approval_request", "editor_proposal_rejection_request"}:
                    loaded = self.by_name["removal_proposal_with_outgoing_connections"] if item["name"] == "removal_approval_with_outgoing_connections" else self.by_name["editor_proposal_view"]
                    if item["name"] == "draft_rejection_request":
                        loaded = deepcopy(loaded)
                        loaded["core_proposal"]["proposal"]["status"] = "draft"
                        loaded["proposal_payload_digest"] = canonical_digest(
                            {key: value for key, value in loaded.items() if key != "proposal_payload_digest"}
                        )
                    validate_editor_semantics(item["payload"], proposal=loaded, impact=self.by_name["removal_impact_with_outgoing_connections"] if item["name"] == "removal_approval_with_outgoing_connections" else None)
                elif item["payload"].get("contract_name") == "editor_proposal_approval_result":
                    self.assertEqual(item["payload"]["outcome"], "published")
                    self.assertTrue(item["payload"]["published_revision"]["immutable"])
                elif item["payload"].get("contract_name") == "editor_record_remove_request":
                    validate_editor_semantics(item["payload"], impact=self.by_name["removal_impact"])
                else:
                    validate_editor_semantics(item["payload"], impact=self.by_name["removal_impact_with_outgoing_connections"] if item["name"] == "removal_proposal_with_outgoing_connections" else None)
        card_validator = Draft202012Validator({"$ref":"#/$defs/change_card", "$defs":self.schema["$defs"]})
        for item in self.card_examples:
            with self.subTest(card=item["name"]):
                self.assertEqual([], list(card_validator.iter_errors(item["payload"])))
                validate_card_semantics(item["payload"], existing_record_ids={"record-company", "record-ship", "record-station", "record-new"})

    def test_correction_rejection_and_replay_response_fixtures_prove_workflow_lifecycle(self):
        lifecycle = {item["name"]: item for item in self.lifecycle_examples}
        self.assertEqual({"correction_response", "rejection_response", "approval_replay_response"}, set(lifecycle))
        self.assertEqual((8, 9), (lifecycle["correction_response"]["workflow_before"], lifecycle["correction_response"]["workflow_after"]))
        self.assertEqual((8, 9), (lifecycle["rejection_response"]["workflow_before"], lifecycle["rejection_response"]["workflow_after"]))
        replay = lifecycle["approval_replay_response"]
        self.assertEqual((8, 9), (replay["workflow_before"], replay["workflow_after"]))
        self.assertEqual(9, replay["replay_workflow_after"])
        self.assertTrue(replay["replay_is_byte_identical"])

        correction = deepcopy(self.by_name["editor_proposal_view"])
        correction["proposal_version"] = 2
        correction["correction_of"] = {"proposal_id": "proposal_editor", "proposal_version": 1}
        correction["editor_workflow_version"] = 9
        correction["core_proposal"]["proposal"]["proposal_version"] = 2
        correction["core_proposal"]["proposal"]["expected_editor_workflow_version"] = 9
        for binding in correction["record_bindings"]:
            binding["expected_editor_workflow_version"] = 9
        correction["proposal_payload_digest"] = canonical_digest({key: value for key, value in correction.items() if key != "proposal_payload_digest"})
        self.assertEqual([], list(Draft202012Validator(self.schema).iter_errors(correction)))
        validate_editor_semantics(correction)

        rejection = self.by_name["rejection_response"]
        approval = self.by_name["approval_success_response"]
        self.assertEqual(rejection["editor_workflow_version"], approval["editor_workflow_version"])
        self.assertEqual(rejection["proposal"], approval["proposal"])
        self.assertEqual(self.by_name["approval_replay_response"], approval)

    def test_record_member_ids_are_unique_by_identifier_not_whole_object(self):
        for collection, identifier in (("fields", "field_id"), ("sections", "section_id"), ("connections", "connection_id")):
            value = deepcopy(self.by_name["edit_record_with_connections_request"])
            duplicate = deepcopy(value["candidate"][collection][0])
            duplicate[identifier] = value["candidate"][collection][0][identifier]
            if collection == "fields":
                duplicate["value"] = "a different value"
            elif collection == "sections":
                duplicate["body"] = "A different body."
            else:
                duplicate["context"] = "A different context."
            value["candidate"][collection].append(duplicate)
            value["candidate"]["content_digest"] = content_digest(value["candidate"])
            value["operation_request"]["payload_digest"] = _operation_digest(value)
            with self.subTest(collection=collection), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(value)
            self.assertEqual("proposal_validation_failure", caught.exception.category)

    def test_publication_status_and_revision_are_bound_to_proposal_status(self):
        for status, publication in (("draft", {"status": "published", "published_revision": {"revision_id": "revision_13", "ordinal": 13, "tree_digest": "d" * 64}}), ("needs_review", {"status": "not_published", "published_revision": {"revision_id": "revision_13", "ordinal": 13, "tree_digest": "d" * 64}}), ("rejected", {"status": "published", "published_revision": {"revision_id": "revision_13", "ordinal": 13, "tree_digest": "d" * 64}})):
            value = deepcopy(self.by_name["editor_proposal_view"])
            value["core_proposal"]["proposal"]["status"] = status
            value["publication"] = publication
            value["proposal_payload_digest"] = canonical_digest({key: item for key, item in value.items() if key != "proposal_payload_digest"})
            with self.subTest(status=status):
                self.assertTrue(list(Draft202012Validator(self.schema).iter_errors(value)))
                with self.assertRaises(EditorSemanticError) as caught:
                    validate_editor_semantics(value)
                self.assertEqual("proposal_approval_conflict", caught.exception.category)

    def test_removal_proposal_and_approval_require_exact_impact_and_all_resolutions(self):
        impact = deepcopy(self.by_name["removal_impact"])
        removed = deepcopy(next(item["payload"] for item in self.card_examples if item["name"] == "record_removed_card"))
        resolved = deepcopy(next(item["payload"] for item in self.card_examples if item["name"] == "reference_resolution_card"))
        proposal = deepcopy(self.by_name["editor_proposal_view"])
        proposal["mutation_kind"] = "remove"
        removed_binding = deepcopy(impact["binding"])
        removed_binding["expected_editor_workflow_version"] = 8
        proposal["record_bindings"] = [removed_binding, {
            "campaign_id": "campaign_alpha",
            "base_revision": deepcopy(impact["binding"]["base_revision"]),
            "record_id": "record-station",
            "record_digest": "f" * 64,
            "expected_editor_workflow_version": 8,
        }]
        proposal["diff"]["cards"] = [removed, resolved]
        proposal["diff"]["affected_record_count"] = 2
        proposal["diff"]["authority_changes"] = []
        proposal["diff"]["visibility_changes"] = []
        proposal["authority_outcome"] = []
        proposal["visibility_outcome"] = []
        proposal["diff"]["impact_digest"] = impact["impact_digest"]
        proposal["impact_digest"] = impact["impact_digest"]
        proposal["impact_binding"] = {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}
        proposal["resolutions"] = deepcopy(self.by_name["remove_record_request"]["resolutions"])
        proposal["core_proposal"]["proposal"]["authority_change_ids"] = []
        proposal["core_proposal"]["proposal"]["visibility_change_ids"] = []
        proposal["core_proposal"]["proposal"]["changes"] = [
            {"change_id": removed["change_id"], "subject_id": "record-company", "change_type": "remove", "from_authority": "canon", "to_authority": "absent", "content_digest": removed["before"]["content_digest"]},
            {"change_id": resolved["change_id"], "subject_id": "record-station", "change_type": "update", "from_authority": "preparation", "to_authority": "preparation", "content_digest": "f" * 64},
        ]
        proposal["diff"]["diff_digest"] = canonical_digest({key: proposal["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")})
        proposal["core_proposal"]["proposal"]["diff_digest"] = proposal["diff"]["diff_digest"]
        proposal["proposal_payload_digest"] = canonical_digest({key: value for key, value in proposal.items() if key != "proposal_payload_digest"})
        validate_editor_semantics(proposal, impact=impact)

        approval = deepcopy(self.by_name["approval_request"])
        for key in ("source_revision", "base_revision", "expected_campaign_head", "proposal_payload_digest", "diff", "record_bindings", "impact_digest", "impact_binding", "resolutions", "authority_outcome", "visibility_outcome"):
            approval[key] = deepcopy(proposal[key])
        approval["diff_digest"] = proposal["diff"]["diff_digest"]
        approval["mutation_kind"] = "remove"
        approval["affected_record_count"] = 2
        approval["confirmed_change_ids"] = [removed["change_id"], resolved["change_id"]]
        approval["confirmed_authority_change_ids"] = []
        approval["confirmed_visibility_change_ids"] = []
        approval["operation_request"]["intent_digest"] = approval["diff_digest"]
        approval["operation_request"]["payload_digest"] = _operation_digest(approval)
        validate_editor_semantics(approval, proposal=proposal, impact=impact)

        invalid_proposal = deepcopy(proposal)
        invalid_proposal["diff"]["cards"][1]["derived_backlinks"] = []
        invalid_proposal["diff"]["diff_digest"] = canonical_digest({key: invalid_proposal["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")})
        invalid_proposal["core_proposal"]["proposal"]["diff_digest"] = invalid_proposal["diff"]["diff_digest"]
        invalid_proposal["proposal_payload_digest"] = canonical_digest({key: value for key, value in invalid_proposal.items() if key != "proposal_payload_digest"})
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(invalid_proposal, impact=impact)
        self.assertEqual("unsafe_binding", caught.exception.category)

        invalid_approval = deepcopy(approval)
        invalid_approval["diff"]["cards"][1]["derived_backlinks"] = []
        invalid_approval["operation_request"]["payload_digest"] = _operation_digest(invalid_approval)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(invalid_approval, proposal=proposal, impact=impact)
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

        approval["resolutions"] = []
        approval["operation_request"]["payload_digest"] = _operation_digest(approval)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(approval, proposal=proposal, impact=impact)
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

    def test_cards_bind_digests_connections_backlinks_count_and_approval_sets(self):
        value = deepcopy(self.by_name["editor_proposal_view"])
        value["diff"]["cards"][0]["after"]["sections"][0]["body"] = "A different mutation."
        value["proposal_payload_digest"] = canonical_digest({key: item for key, item in value.items() if key != "proposal_payload_digest"})
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(value)
        self.assertEqual("idempotency_digest_conflict", caught.exception.category)

        value = deepcopy(self.by_name["editor_proposal_view"])
        value["diff"]["cards"][1]["derived_backlinks"] = []
        value["diff"]["diff_digest"] = canonical_digest({key: value["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")})
        value["core_proposal"]["proposal"]["diff_digest"] = value["diff"]["diff_digest"]
        value["proposal_payload_digest"] = canonical_digest({key: item for key, item in value.items() if key != "proposal_payload_digest"})
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(value)
        self.assertEqual("unsafe_binding", caught.exception.category)

        value = deepcopy(self.by_name["approval_request"])
        value["affected_record_count"] = 2
        value["operation_request"]["payload_digest"] = _operation_digest(value)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(value, proposal=self.by_name["editor_proposal_view"])
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

        value = deepcopy(self.by_name["approval_request"])
        value["confirmed_change_ids"] = ["change_edit_record"]
        value["operation_request"]["payload_digest"] = _operation_digest(value)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(value, proposal=self.by_name["editor_proposal_view"])
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

    def test_canonical_digests_cover_every_positive_payload(self):
        for item in self.examples:
            payload = item["payload"]
            if "operation_request" in payload:
                self.assertEqual(_operation_digest(payload), payload["operation_request"]["payload_digest"], item["name"])
            for key in ("record", "candidate"):
                if payload.get(key) is not None:
                    self.assertEqual(content_digest(payload[key]), payload[key]["content_digest"], f"{item['name']} {key}")
            if payload.get("contract_name") == "editor_removal_impact":
                self.assertEqual(payload["impact_digest"], canonical_digest({key:payload[key] for key in ("record", "outgoing_connections", "incoming_references")}))
            if "diff" in payload:
                self.assertEqual(payload["diff"]["diff_digest"], canonical_digest({key:payload["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")}))
            if payload.get("contract_name") == "editor_proposal_view":
                self.assertEqual(payload["proposal_payload_digest"], canonical_digest({key:value for key,value in payload.items() if key != "proposal_payload_digest"}))

    def test_every_negative_fixture_is_schema_or_semantically_rejected(self):
        validator = Draft202012Validator(self.schema)
        head = {"revision_id":"revision_12", "ordinal":12, "tree_digest":"a"*64}
        for path in sorted((EDITOR_ROOT / "negative").glob("*.json")):
            fixture = json.loads(path.read_text())
            with self.subTest(fixture=path.name):
                errors = list(validator.iter_errors(fixture["instance"]))
                self.assertFalse(errors, [(list(error.absolute_path), error.message) for error in errors])
                kwargs = {"current_head": head}
                if path.name == "stale-record-digest.json": kwargs["record_digest_at_base"] = "c"*64
                if path.name == "workflow-conflict.json": kwargs["current_workflow_version"] = 7
                if path.name == "replay-mismatch.json": kwargs["stored_receipt"] = fixture["stored_receipt"]
                if path.name == "incomplete-removal-resolution.json": kwargs["required_reference_ids"] = {"reference_station_company"}
                if path.name == "invalid-correction.json": kwargs["proposal"] = self.by_name["editor_proposal_view"]
                if path.name == "incomplete-removal-correction.json":
                    remove_proposal = deepcopy(self.by_name["editor_proposal_view"])
                    remove_proposal["mutation_kind"] = "remove"
                    remove_proposal["record_bindings"] = [deepcopy(self.by_name["removal_impact"]["binding"])]
                    remove_proposal["record_bindings"][0]["expected_editor_workflow_version"] = 8
                    remove_proposal["impact_digest"] = self.by_name["removal_impact"]["impact_digest"]
                    kwargs["proposal"] = remove_proposal
                    impact = deepcopy(self.by_name["removal_impact"])
                    impact["binding"]["expected_editor_workflow_version"] = 8
                    kwargs["impact"] = impact
                if path.name == "invalid-connections.json": kwargs["existing_record_ids"] = {"record-station", "record-company", "record-ship"}
                if path.name.startswith("removal-outgoing-"):
                    kwargs["impact"] = self.by_name["removal_impact_with_outgoing_connections"]
                with self.assertRaises(EditorSemanticError) as caught:
                    validate_editor_semantics(fixture["instance"], **kwargs)
                self.assertEqual(fixture["expected_category"], caught.exception.category)
                self.assertEqual(fixture["expected_path"], caught.exception.path)

    def test_every_binding_and_digest_rule_fails_closed(self):
        cases = [("expected_revision", "unsafe_binding"), ("workflow", "unsafe_binding"), ("record_id", "unsafe_binding"), ("digest", "idempotency_digest_conflict")]
        for label, category in cases:
            value = deepcopy(self.by_name["edit_record_with_connections_request"])
            if label == "expected_revision": value["operation_request"]["expected_revision"] = "revision_other"
            if label == "workflow": value["binding"]["expected_editor_workflow_version"] = 8
            if label == "record_id": value["candidate"]["record_id"] = "record-other"
            if label == "digest": value["operation_request"]["payload_digest"] = "0" * 64
            with self.subTest(binding=label), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(value)
            self.assertEqual(category, caught.exception.category)

    def test_all_duplicated_revision_workflow_and_proposal_bindings_are_exact(self):
        cases = []
        proposal = deepcopy(self.by_name["editor_proposal_view"])
        proposal["expected_campaign_head"]["tree_digest"] = "f" * 64
        proposal["proposal_payload_digest"] = canonical_digest({key: value for key, value in proposal.items() if key != "proposal_payload_digest"})
        cases.append((proposal, "unsafe_binding"))
        proposal = deepcopy(self.by_name["editor_proposal_view"])
        proposal["record_bindings"][0]["base_revision"]["ordinal"] = 13
        cases.append((proposal, "unsafe_binding"))
        approval = deepcopy(self.by_name["approval_request"])
        approval["expected_editor_workflow_version"] = 9
        approval["operation_request"]["expected_editor_workflow_version"] = 9
        approval["operation_request"]["payload_digest"] = _operation_digest(approval)
        cases.append((approval, "proposal_approval_conflict"))
        for value, category in cases:
            with self.subTest(category=category), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(value, proposal=self.by_name["editor_proposal_view"] if value["contract_name"] == "editor_proposal_approval_request" else None)
            self.assertEqual(category, caught.exception.category)

    def test_redirect_and_resolution_rules_are_executable(self):
        card = deepcopy(next(item["payload"] for item in self.card_examples if item["name"] == "reference_resolution_card"))
        card["after"]["replacement_target_record_id"] = "record-company"
        card["resolution"]["replacement_target_record_id"] = "record-company"
        with self.assertRaises(EditorSemanticError):
            validate_card_semantics(card, existing_record_ids={"record-company", "record-ship"})
        self.assertFalse(self.by_name["historical_record_view"]["editable"])

    def test_approval_requires_loaded_needs_review_passed_exact_proposal_and_diff(self):
        proposal = self.by_name["editor_proposal_view"]
        approval = deepcopy(self.by_name["approval_request"])
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(approval)
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

        approval = deepcopy(self.by_name["approval_request"])
        approval["proposal_status"] = "draft"
        approval["operation_request"]["payload_digest"] = _operation_digest(approval)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(approval, proposal=proposal)
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

        approval = deepcopy(self.by_name["approval_request"])
        approval["diff"]["summary"] = "changed"
        approval["operation_request"]["payload_digest"] = _operation_digest(approval)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(approval, proposal=proposal)
        self.assertEqual("proposal_approval_conflict", caught.exception.category)

    def test_mutation_kind_requires_its_record_digest_and_payload_shape(self):
        create = deepcopy(self.by_name["create_record_request"])
        create["binding"]["record_digest"] = "a" * 64
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(create)
        self.assertEqual("unsafe_binding", caught.exception.category)

        edit = deepcopy(self.by_name["edit_record_with_connections_request"])
        edit["binding"]["record_digest"] = None
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(edit)
        self.assertEqual("unsafe_binding", caught.exception.category)

    def test_removal_binds_impact_and_rejects_unpermitted_unresolved(self):
        impact = deepcopy(self.by_name["removal_impact"])
        remove = deepcopy(self.by_name["remove_record_request"])
        remove["binding"] = deepcopy(impact["binding"])
        remove["impact_digest"] = impact["impact_digest"]
        validate_editor_semantics(remove, impact=impact, existing_record_ids={"record-company", "record-ship", "record-station"})
        remove["resolutions"][0] = {"reference_id":"reference_station_company", "action":"accept_unresolved", "replacement_target_record_id":None}
        remove["operation_request"]["payload_digest"] = _operation_digest(remove)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(remove, impact=impact, existing_record_ids={"record-company", "record-ship", "record-station"})
        self.assertEqual("proposal_validation_failure", caught.exception.category)

        remove = deepcopy(self.by_name["remove_record_request"])
        remove["operation_request"]["payload_digest"] = _operation_digest(remove)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(remove, existing_record_ids={"record-company", "record-ship", "record-station"})
        self.assertEqual("unsafe_binding", caught.exception.category)
        self.assertEqual("impact lookup", caught.exception.path)

    def test_removal_resolution_has_exactly_one_action_per_impact_reference(self):
        impact = deepcopy(self.by_name["removal_impact"])
        remove = deepcopy(self.by_name["remove_record_request"])
        remove["binding"] = deepcopy(impact["binding"])
        remove["impact_digest"] = impact["impact_digest"]
        remove["resolutions"].append(
            {
                "reference_id": "reference_station_company",
                "action": "redirect",
                "replacement_target_record_id": "record-ship",
            }
        )
        remove["operation_request"]["payload_digest"] = _operation_digest(remove)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(
                remove,
                impact=impact,
                existing_record_ids={"record-company", "record-ship", "record-station"},
            )
        self.assertEqual("incomplete_removal_resolution", caught.exception.category)
        self.assertEqual("resolutions", caught.exception.path)

    def test_approval_and_rejection_recheck_supplied_head_workflow_and_all_action_bindings(self):
        proposal = self.by_name["editor_proposal_view"]
        for name in ("approval_request", "rejection_request"):
            action = deepcopy(self.by_name[name])
            action["operation_request"]["payload_digest"] = _operation_digest(action)
            with self.subTest(action=name, binding="head"), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(action, proposal=proposal, current_head={"revision_id":"revision_13", "ordinal":13, "tree_digest":"d"*64})
            self.assertEqual("stale_revision", caught.exception.category)
            action = deepcopy(self.by_name[name])
            action["operation_request"]["payload_digest"] = _operation_digest(action)
            with self.subTest(action=name, binding="workflow"), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(action, proposal=proposal, current_workflow_version=9)
            self.assertEqual("workflow_conflict", caught.exception.category)
            action = deepcopy(self.by_name[name])
            action["authority_outcome"] = []
            action["operation_request"]["payload_digest"] = _operation_digest(action)
            with self.subTest(action=name, binding="authority"), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(action, proposal=proposal)
            self.assertEqual("proposal_approval_conflict", caught.exception.category)
        reject_route = next(item for item in json.loads((EDITOR_ROOT / "routes.json").read_text())["routes"] if item["id"] == "editor_proposal_reject")
        self.assertIn("stale_revision", reject_route["error_status"]["409"])

    def test_workflow_cas_sequence_consumes_each_accepted_step_once(self):
        for name in ("create_record_request", "edit_record_with_connections_request", "remove_record_request"):
            self.assertEqual(7, self.by_name[name]["operation_request"]["expected_editor_workflow_version"])
            self.assertEqual(7, self.by_name[name]["binding"]["expected_editor_workflow_version"])
        self.assertEqual(8, self.by_name["editor_proposal_view"]["editor_workflow_version"])
        self.assertEqual(8, self.by_name["correction_request"]["operation_request"]["expected_editor_workflow_version"])
        self.assertEqual(8, self.by_name["correction_request"]["binding"]["expected_editor_workflow_version"])
        action = self.by_name["approval_request"]
        self.assertEqual(8, action["expected_editor_workflow_version"])
        self.assertEqual(9, self.by_name["approval_success_response"]["editor_workflow_version"])
        self.assertEqual(8, self.by_name["rejection_request"]["expected_editor_workflow_version"])
        self.assertEqual(8, self.by_name["draft_rejection_request"]["expected_editor_workflow_version"])

    def test_removal_correction_reuses_exact_impact_and_complete_resolution(self):
        correction = deepcopy(self.by_name["correction_request"])
        impact = deepcopy(self.by_name["removal_impact"])
        impact["binding"]["expected_editor_workflow_version"] = 8
        remove_proposal = deepcopy(self.by_name["editor_proposal_view"])
        remove_proposal["mutation_kind"] = "remove"
        remove_proposal["record_bindings"] = [deepcopy(impact["binding"])]
        remove_proposal["record_bindings"][0]["expected_editor_workflow_version"] = 8
        remove_proposal["impact_digest"] = impact["impact_digest"]
        correction["operation_request"]["expected_revision"] = "revision_12"
        correction["binding"] = deepcopy(impact["binding"])
        correction["binding"]["expected_editor_workflow_version"] = 8
        correction["mutation_kind"] = "remove"
        correction["candidate"] = None
        correction["resolutions"] = [deepcopy(self.by_name["remove_record_request"]["resolutions"][0])]
        correction["impact_digest"] = impact["impact_digest"]
        correction["impact_binding"] = {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}
        correction["operation_request"]["payload_digest"] = _operation_digest(correction)
        validate_editor_semantics(correction, proposal=remove_proposal, impact=impact, existing_record_ids={"record-company", "record-ship", "record-station"})
        correction["resolutions"] = []
        correction["operation_request"]["payload_digest"] = _operation_digest(correction)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(correction, proposal=remove_proposal, impact=impact, existing_record_ids={"record-company", "record-ship", "record-station"})
        self.assertEqual("incomplete_removal_resolution", caught.exception.category)

    def test_mutation_card_kinds_and_record_bindings_fail_closed(self):
        proposal = deepcopy(self.by_name["editor_proposal_view"])
        proposal["mutation_kind"] = "create"
        proposal["record_bindings"][0]["record_digest"] = None
        proposal["proposal_payload_digest"] = canonical_digest({key:value for key,value in proposal.items() if key != "proposal_payload_digest"})
        self.assertTrue(list(Draft202012Validator(self.schema).iter_errors(proposal)))
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(proposal)
        self.assertEqual("unsafe_binding", caught.exception.category)

        proposal = deepcopy(self.by_name["editor_proposal_view"])
        proposal["record_bindings"].append(deepcopy(proposal["record_bindings"][0]))
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(proposal)
        self.assertEqual("proposal_validation_failure", caught.exception.category)

    def test_connection_cards_must_match_resulting_record_connections(self):
        cards = deepcopy(self.by_name["editor_proposal_view"]["diff"]["cards"])
        cards[0]["after"]["connections"] = cards[0]["after"]["connections"][:1]
        cards[0]["after"]["content_digest"] = content_digest(cards[0]["after"])
        with self.assertRaises(EditorSemanticError) as caught:
            _validate_connection_cards(cards)
        self.assertEqual("unsafe_binding", caught.exception.category)

        cards = deepcopy(self.by_name["editor_proposal_view"]["diff"]["cards"])
        cards[1]["connection"] = deepcopy(cards[0]["before"]["connections"][0])
        with self.assertRaises(EditorSemanticError) as caught:
            _validate_connection_cards(cards)
        self.assertEqual("unsafe_binding", caught.exception.category)

        cards = deepcopy(self.by_name["editor_proposal_view"]["diff"]["cards"])
        cards.pop()
        with self.assertRaises(EditorSemanticError) as caught:
            _validate_connection_cards(cards)
        self.assertEqual("unsafe_binding", caught.exception.category)

        no_op = deepcopy(next(item["payload"] for item in self.card_examples if item["name"] == "connection_updated_card"))
        no_op["connection"]["after"] = deepcopy(no_op["connection"]["before"])
        with self.assertRaises(EditorSemanticError) as caught:
            validate_card_semantics(no_op)
        self.assertEqual("proposal_validation_failure", caught.exception.category)

    def test_status_is_the_authority_source_for_views_proposals_and_approvals(self):
        view = deepcopy(self.by_name["head_record_view"])
        view["record"]["authority"] = "canon"
        view["record"]["content_digest"] = content_digest(view["record"])
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(view)
        self.assertEqual("invalid_authority_transition", caught.exception.category)
        self.assertEqual("record.authority", caught.exception.path)

        proposal = deepcopy(self.by_name["editor_proposal_view"])
        proposal["diff"]["cards"][0]["after"]["authority"] = "preparation"
        proposal["diff"]["cards"][0]["after"]["content_digest"] = content_digest(proposal["diff"]["cards"][0]["after"])
        proposal["diff"]["diff_digest"] = canonical_digest({key: proposal["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")})
        proposal["core_proposal"]["proposal"]["diff_digest"] = proposal["diff"]["diff_digest"]
        proposal["proposal_payload_digest"] = canonical_digest({key: item for key, item in proposal.items() if key != "proposal_payload_digest"})
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(proposal)
        self.assertEqual("invalid_authority_transition", caught.exception.category)

        approval = deepcopy(self.by_name["approval_request"])
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(approval, proposal=proposal)
        self.assertEqual("invalid_authority_transition", caught.exception.category)

    def test_logical_identifiers_are_unique_even_when_duplicate_objects_differ(self):
        cases = []
        value = deepcopy(self.by_name["editor_proposal_view"])
        duplicate = deepcopy(value["core_proposal"]["proposal"]["changes"][0])
        duplicate["subject_id"] = "record-ship"
        value["core_proposal"]["proposal"]["changes"].append(duplicate)
        cases.append(value)

        value = deepcopy(self.by_name["editor_proposal_view"])
        duplicate = deepcopy(value["diff"]["cards"][0])
        duplicate["subject_record_id"] = "record-ship"
        value["diff"]["cards"].append(duplicate)
        cases.append(value)

        for field, identifier, changed in (("authority_changes", "change_id", "record_id"), ("visibility_changes", "change_id", "record_id")):
            value = deepcopy(self.by_name["editor_proposal_view"])
            duplicate = deepcopy(value["diff"][field][0])
            duplicate[changed] = "record-ship"
            value["diff"][field].append(duplicate)
            cases.append(value)

        value = deepcopy(self.by_name["editor_proposal_view"])
        duplicate = deepcopy(value["record_bindings"][0])
        duplicate["record_digest"] = "f" * 64
        value["record_bindings"].append(duplicate)
        cases.append(value)

        impact = deepcopy(self.by_name["removal_impact"])
        impact["incoming_references"].append(deepcopy(impact["incoming_references"][0]))
        impact["incoming_references"][1]["context"] = "A different reference."
        impact["impact_digest"] = canonical_digest({key: impact[key] for key in ("record", "outgoing_connections", "incoming_references")})
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(impact)
        self.assertEqual("proposal_validation_failure", caught.exception.category)

        remove = deepcopy(self.by_name["remove_record_request"])
        remove["resolutions"].append(deepcopy(remove["resolutions"][0]))
        remove["resolutions"][1]["replacement_target_record_id"] = "record-station"
        remove["operation_request"]["payload_digest"] = _operation_digest(remove)
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(remove, impact=self.by_name["removal_impact"])
        self.assertEqual("incomplete_removal_resolution", caught.exception.category)

        for value in cases:
            with self.subTest(value=value["contract_name"]), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(value)
            self.assertEqual("proposal_validation_failure", caught.exception.category)

    def test_all_named_digest_inputs_normalize_crlf_and_cr(self):
        values = [
            {"record": {"body": "record\r\ntext"}},
            {"record": {"body": "impact\rtext"}, "incoming_references": []},
            {"cards": [], "summary": "diff\r\ntext"},
            {"proposal": {"summary": "proposal\rtext"}},
            {"status": "passed", "error_count": 0, "findings": [{"location":"record\r\nsummary"}]},
        ]
        for value in values:
            normalized = deepcopy(value)
            normalized = _normalize_text(normalized)
            with self.subTest(value=value):
                self.assertEqual(canonical_digest(value), canonical_digest(normalized))

    def test_text_and_validation_digests_normalize_without_trimming(self):
        record = deepcopy(self.by_name["head_record_view"]["record"])
        crlf = deepcopy(record)
        crlf["sections"][0]["body"] = "A quiet station.\r\n"
        lf = deepcopy(record)
        lf["sections"][0]["body"] = "A quiet station.\n"
        self.assertEqual(content_digest(crlf), content_digest(lf))
        validation = deepcopy(self.by_name["editor_proposal_view"]["validation"])
        self.assertEqual(validation["validation_digest"], validation_digest(validation))

    def test_negative_categories_have_executable_semantic_counterexamples(self):
        create = deepcopy(self.by_name["create_record_request"])
        edit = deepcopy(self.by_name["edit_record_with_connections_request"])
        correction = deepcopy(self.by_name["correction_request"])
        correction["mutation_kind"] = "remove"
        correction["operation_request"]["payload_digest"] = _operation_digest(correction)
        remove = deepcopy(self.by_name["remove_record_request"])
        cases = [
            (remove, {"required_reference_ids":{"reference_station_company", "reference_other"}}, "incomplete_removal_resolution"),
            (edit, {"existing_record_ids":{"record-station"}}, "invalid_connections"),
            (edit, {"current_head":{"revision_id":"revision_11", "ordinal":11, "tree_digest":"1"*64}}, "stale_revision"),
            (edit, {"record_digest_at_base":"c"*64}, "stale_record_digest"),
            (correction, {"proposal":self.by_name["editor_proposal_view"]}, "invalid_correction"),
            (create, {"stored_receipt":{"idempotency_key":"idem_create", "payload_digest":"b"*64, "outcome":"accepted"}}, "replay_mismatch"),
            (remove, {"current_workflow_version":8}, "workflow_conflict"),
        ]
        bad_authority = deepcopy(edit)
        bad_authority["candidate"]["authority"] = "preparation"
        bad_authority["candidate"]["content_digest"] = content_digest(bad_authority["candidate"])
        cases.append((bad_authority, {}, "invalid_authority_transition"))
        for payload, kwargs, category in cases:
            with self.subTest(category=category), self.assertRaises(EditorSemanticError) as caught:
                validate_editor_semantics(payload, **kwargs)
            self.assertEqual(category, caught.exception.category)

        unsafe = deepcopy(create)
        unsafe["operation_request"]["request_id"] = "../private"
        with self.assertRaises(EditorSemanticError) as caught:
            validate_editor_semantics(unsafe)
        self.assertEqual("unsafe_binding", caught.exception.category)

    def test_mutation_specific_cards_are_closed_and_all_kinds_are_present(self):
        kinds = {card["kind"] for item in self.examples for card in item["payload"].get("diff", {}).get("cards", [])}
        kinds |= {item["payload"]["kind"] for item in self.card_examples}
        self.assertEqual({"record_created", "record_updated", "record_removed", "connection_added", "connection_updated", "connection_removed", "reference_resolution"}, kinds)
        for name in ("record_created_card", "record_updated_card", "record_removed_card", "connection_added_card", "connection_updated_card", "connection_removed_card", "reference_resolution_card"):
            self.assertIn(name, self.schema["$defs"])
        self.assertNotEqual({}, self.schema["$defs"]["property_change"]["properties"]["before"])
        self.assertNotEqual({}, self.schema["$defs"]["card_common"]["properties"]["before"])


if __name__ == "__main__":
    unittest.main()
