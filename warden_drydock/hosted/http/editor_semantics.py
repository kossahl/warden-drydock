"""Semantic guards for the structured record-editor wire contracts.

JSON Schema describes the closed shapes.  These checks bind the shapes to the
same typed mutation projection that is sent to the deterministic engine.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .contracts import canonical_digest, text_digest
from .editor import _document, document_digest


class EditorSemanticError(ValueError):
    def __init__(self, category: str, path: str) -> None:
        self.category = category
        self.path = path
        super().__init__(path)


def _fail(category: str, path: str) -> None:
    raise EditorSemanticError(category, path)


def _equal(left: object, right: object, category: str, path: str) -> None:
    if left != right:
        _fail(category, path)


def _unique(items: list[Mapping[str, Any]], key: str, path: str, category: str = "proposal_validation_failure") -> None:
    values = [item.get(key) for item in items]
    if len(values) != len(set(values)):
        _fail(category, path)


def _record(value: Mapping[str, Any], path: str) -> None:
    try:
        normalized = _document(value)
    except (KeyError, TypeError, ValueError):
        _fail("proposal_validation_failure", path)
    _equal(document_digest(normalized), value.get("content_digest"), "idempotency_digest_conflict", f"{path}.content_digest")


def _property_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in ("displayed_name", "status", "authority", "visibility"):
        if before[name] != after[name]:
            result.append({"property": name, "before": before[name], "after": after[name]})
    for collection, identifier, value_key in (("fields", "field_id", "value"), ("sections", "section_id", "body")):
        old = {item[identifier]: item for item in before[collection]}
        new = {item[identifier]: item for item in after[collection]}
        for member_id in sorted(set(old) | set(new)):
            old_value = old.get(member_id, {}).get(value_key)
            new_value = new.get(member_id, {}).get(value_key)
            if old_value != new_value:
                result.append({"property": f"{collection}.{member_id}", "before": old_value, "after": new_value})
    return result


def _connection_cards(cards: list[Mapping[str, Any]]) -> None:
    records = [card for card in cards if card["kind"] in {"record_created", "record_updated", "record_removed"}]
    if len({card["subject_record_id"] for card in records}) != len(records):
        _fail("unsafe_binding", "diff.cards.record_subject")
    expected: dict[tuple[str, str, str], tuple[object, list[dict[str, Any]]]] = {}
    for card in records:
        before = card.get("before") if isinstance(card.get("before"), dict) else {}
        after = card.get("after") if isinstance(card.get("after"), dict) else {}
        old = {item["connection_id"]: item for item in before.get("connections", [])}
        new = {item["connection_id"]: item for item in after.get("connections", [])}
        subject = card["subject_record_id"]
        for connection_id in sorted(set(old) | set(new)):
            previous, current = old.get(connection_id), new.get(connection_id)
            if previous is None:
                kind, connection = "connection_added", current
                effects = [{"source_record_id": subject, "target_record_id": current["target_record_id"], "connection_id": connection_id, "effect": "added"}]
            elif current is None:
                kind, connection = "connection_removed", previous
                effects = [{"source_record_id": subject, "target_record_id": previous["target_record_id"], "connection_id": connection_id, "effect": "removed"}]
            elif previous != current:
                kind, connection = "connection_updated", {"before": previous, "after": current}
                effects = (
                    [{"source_record_id": subject, "target_record_id": previous["target_record_id"], "connection_id": connection_id, "effect": "removed"}, {"source_record_id": subject, "target_record_id": current["target_record_id"], "connection_id": connection_id, "effect": "added"}]
                    if previous["target_record_id"] != current["target_record_id"]
                    else [{"source_record_id": subject, "target_record_id": current["target_record_id"], "connection_id": connection_id, "effect": "updated"}]
                )
            else:
                continue
            expected[(subject, kind, connection_id)] = (connection, effects)
    actual: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for card in cards:
        if card["kind"] not in {"connection_added", "connection_updated", "connection_removed"}:
            continue
        connection = card["connection"]
        connection_id = connection["before"]["connection_id"] if card["kind"] == "connection_updated" else connection["connection_id"]
        key = (card["subject_record_id"], card["kind"], connection_id)
        if key in actual:
            _fail("proposal_validation_failure", "diff.cards.change_id")
        actual[key] = card
    _equal(set(actual), set(expected), "unsafe_binding", "diff.cards.connection_delta")
    for key, card in actual.items():
        connection, effects = expected[key]
        _equal(card["connection"], connection, "unsafe_binding", "diff.cards.connection")
        _equal(card["derived_backlinks"], effects, "unsafe_binding", "diff.cards.derived_backlinks")
    for card in records:
        if card["kind"] == "record_updated":
            if card["before"] == card["after"]:
                _fail("proposal_validation_failure", "diff.cards.record_updated")
            _equal(card["property_changes"], _property_changes(card["before"], card["after"]), "proposal_validation_failure", "diff.cards.property_changes")


def _resolution_check(resolutions: list[Mapping[str, Any]], references: Mapping[str, Mapping[str, Any]], *, existing_record_ids: set[str] | None = None, removed_id: str | None = None) -> None:
    _unique(resolutions, "reference_id", "resolutions", "incomplete_removal_resolution")
    _equal({item["reference_id"] for item in resolutions}, set(references), "incomplete_removal_resolution", "resolutions")
    for item in resolutions:
        reference = references[item["reference_id"]]
        if item["action"] == "accept_unresolved" and not reference["permitted_unresolved"]:
            _fail("proposal_validation_failure", "resolutions.action")
        if item["action"] == "redirect":
            target = item["replacement_target_record_id"]
            if target in {reference["source_record_id"], reference["target_record_id"], removed_id}:
                _fail("proposal_validation_failure", "resolutions.replacement_target_record_id")
            if existing_record_ids is not None and target not in existing_record_ids:
                _fail("invalid_connections", "resolutions.replacement_target_record_id")


def _proposal(value: Mapping[str, Any], *, impact: Mapping[str, Any] | None = None, existing_record_ids: set[str] | None = None) -> None:
    core = value["core_proposal"]["proposal"]
    publication = value["publication"]
    status = core["status"]
    if status == "approved":
        if publication["status"] != "published" or publication["published_revision"] is None:
            _fail("proposal_approval_conflict", "publication")
    elif status == "approving":
        if publication["status"] not in {"not_published", "quarantined"} or publication["published_revision"] is not None:
            _fail("proposal_approval_conflict", "publication")
    elif publication["status"] != "not_published" or publication["published_revision"] is not None:
        _fail("proposal_approval_conflict", "publication")
    if status == "approved":
        published = publication["published_revision"]
        if (
            published.get("immutable") is not True
            or published["ordinal"] != value["base_revision"]["ordinal"] + 1
            or published["revision_id"] == value["base_revision"]["revision_id"]
        ):
            _fail("unsafe_binding", "publication.published_revision")
    _equal(core["expected_campaign_head"], value["base_revision"]["revision_id"], "unsafe_binding", "core_proposal.proposal.expected_campaign_head")
    _equal(core["expected_editor_workflow_version"], value["editor_workflow_version"], "unsafe_binding", "core_proposal.proposal.expected_editor_workflow_version")
    _equal(core["authority_change_ids"], [item["change_id"] for item in value["diff"]["authority_changes"]], "unsafe_binding", "authority_change_ids")
    _equal(core["visibility_change_ids"], [item["change_id"] for item in value["diff"]["visibility_changes"]], "unsafe_binding", "visibility_change_ids")
    _equal(core["proposal_id"], value["proposal_id"], "unsafe_binding", "proposal identity")
    _equal(core["proposal_version"], value["proposal_version"], "unsafe_binding", "proposal identity")
    _equal(core["campaign_id"], value["campaign_id"], "unsafe_binding", "proposal identity")
    if value["source_revision"]["revision_id"] != core["source_revision"] or value["base_revision"]["revision_id"] != core["base_revision"] or value["expected_campaign_head"]["revision_id"] != core["base_revision"]:
        _fail("unsafe_binding", "proposal revision binding")
    _equal(value["source_revision"], value["base_revision"], "unsafe_binding", "source_revision")
    _equal(value["base_revision"], value["expected_campaign_head"], "unsafe_binding", "expected_campaign_head")
    _equal(value["diff"]["diff_digest"], canonical_digest({key: value["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")}), "idempotency_digest_conflict", "diff.diff_digest")
    _equal(core["diff_digest"], value["diff"]["diff_digest"], "unsafe_binding", "core_proposal.diff_digest")
    validation = value["validation"]
    core_validation = value["core_proposal"]["validation"]
    _equal(
        (core_validation["status"], core_validation["validation_digest"], core_validation["error_count"]),
        (validation["status"], validation["validation_digest"], validation["error_count"]),
        "unsafe_binding", "core_proposal.validation",
    )
    _equal(validation["validation_digest"], canonical_digest({key: validation[key] for key in ("status", "error_count", "findings")}), "idempotency_digest_conflict", "validation.validation_digest")
    if status in {"needs_review", "approving", "approved"} and (validation["status"], validation["error_count"]) != ("passed", 0):
        _fail("proposal_validation_failure", "validation")
    approval_binding = value["core_proposal"].get("approval_binding")
    if status in {"approving", "approved"} and approval_binding is None:
        _fail("proposal_approval_conflict", "approval_binding")
    if status not in {"approving", "approved"} and approval_binding is not None:
        _fail("proposal_approval_conflict", "approval_binding")
    if approval_binding is not None:
        expected = {"proposal_id": value["proposal_id"], "proposal_version": value["proposal_version"], "diff_digest": value["diff"]["diff_digest"], "base_revision": core["base_revision"], "source_revision": core["source_revision"], "expected_campaign_head": core["expected_campaign_head"], "expected_editor_workflow_version": core["expected_editor_workflow_version"], "validation_status": validation["status"], "validation_digest": validation["validation_digest"], "authority_change_ids": core["authority_change_ids"], "visibility_change_ids": core["visibility_change_ids"], "warden_confirmed": True}
        _equal(approval_binding, expected, "proposal_approval_conflict", "approval_binding")
    cards = value["diff"]["cards"]
    _unique(cards, "change_id", "diff.cards.change_id")
    _unique(core["changes"], "change_id", "core_proposal.changes.change_id")
    _unique(value["record_bindings"], "record_id", "record_bindings.record_id")
    _unique(value["diff"]["authority_changes"], "change_id", "authority_changes.change_id")
    _unique(value["diff"]["visibility_changes"], "change_id", "visibility_changes.change_id")
    _unique(value["authority_outcome"], "change_id", "authority_outcome.change_id")
    _unique(value["visibility_outcome"], "change_id", "visibility_outcome.change_id")
    _equal({card["change_id"] for card in cards}, {change["change_id"] for change in core["changes"]}, "unsafe_binding", "proposal changes/diff cards")
    _equal({binding["record_id"] for binding in value["record_bindings"]}, {card["subject_record_id"] for card in cards}, "unsafe_binding", "record bindings")
    for binding in value["record_bindings"]:
        _equal(binding["campaign_id"], value["campaign_id"], "unsafe_binding", "record binding campaign")
        _equal(binding["base_revision"], value["base_revision"], "unsafe_binding", "record binding revision")
        _equal(binding["expected_editor_workflow_version"], value["editor_workflow_version"], "unsafe_binding", "record binding workflow")
    actual_authority: set[tuple[Any, ...]] = set()
    actual_visibility: set[tuple[Any, ...]] = set()
    for card in cards:
        for key in ("before", "after"):
            if isinstance(card.get(key), dict) and "content_digest" in card[key]:
                _record(card[key], f"card.{card['change_id']}.{key}")
        if card["kind"] in {"record_created", "record_updated"} and card.get("before") is not None and card.get("after") is not None:
            before, after = card["before"], card["after"]
            if before["authority"] != after["authority"]:
                actual_authority.add((card["change_id"], card["subject_record_id"], before["authority"], after["authority"]))
            if before["visibility"] != after["visibility"]:
                actual_visibility.add((card["change_id"], card["subject_record_id"], json.dumps(before["visibility"], sort_keys=True), json.dumps(after["visibility"], sort_keys=True)))
    declared_authority = {(x["change_id"], x["record_id"], x["from"], x["to"]) for x in value["diff"]["authority_changes"]}
    declared_visibility = {(x["change_id"], x["record_id"], json.dumps(x["before"], sort_keys=True), json.dumps(x["after"], sort_keys=True)) for x in value["diff"]["visibility_changes"]}
    _equal(actual_authority, declared_authority, "proposal_validation_failure", "authority_changes")
    _equal(actual_visibility, declared_visibility, "proposal_validation_failure", "visibility_changes")
    _equal(value["authority_outcome"], value["diff"]["authority_changes"], "unsafe_binding", "authority_outcome")
    _equal(value["visibility_outcome"], value["diff"]["visibility_changes"], "unsafe_binding", "visibility_outcome")
    for change in value["diff"]["authority_changes"]:
        card = next((card for card in cards if card["change_id"] == change["change_id"]), None)
        if card is None or card.get("before") is None or card.get("after") is None or (change["record_id"], change["from"], change["to"]) != (card["subject_record_id"], card["before"]["authority"], card["after"]["authority"]):
            _fail("unsafe_binding", "authority change card")
    for change in value["diff"]["visibility_changes"]:
        card = next((card for card in cards if card["change_id"] == change["change_id"]), None)
        broadens = change["before"]["audience"] == "warden" and change["after"]["audience"] != "warden"
        if card is None or card.get("before") is None or card.get("after") is None or (change["record_id"], change["before"], change["after"]) != (card["subject_record_id"], card["before"]["visibility"], card["after"]["visibility"]) or change["audience_broadens"] != broadens:
            _fail("unsafe_binding", "visibility change card")
    _connection_cards(cards)
    kinds = {"create": {"record_created", "connection_added"}, "edit": {"record_updated", "connection_added", "connection_updated", "connection_removed"}, "remove": {"record_removed", "connection_removed", "reference_resolution"}}
    if value["mutation_kind"] not in kinds or any(card["kind"] not in kinds[value["mutation_kind"]] for card in cards):
        _fail("proposal_validation_failure", "diff.cards.kind")
    if value["mutation_kind"] == "create" and sum(card["kind"] == "record_created" for card in cards) != 1:
        _fail("proposal_validation_failure", "diff.cards.record_created")
    if value["mutation_kind"] in {"edit", "remove"} and any(binding["record_digest"] is None for binding in value["record_bindings"]):
        _fail("unsafe_binding", "record_bindings.record_digest")
    if value["mutation_kind"] == "create" and any(binding["record_digest"] is not None for binding in value["record_bindings"]):
        _fail("unsafe_binding", "record_bindings.record_digest")
    if value["mutation_kind"] == "remove":
        if impact is None or value["impact_digest"] != impact["impact_digest"] or value["diff"]["impact_digest"] != value["impact_digest"] or value["impact_binding"] != {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}:
            _fail("unsafe_binding", "impact binding")
        references = {item["reference_id"]: item for item in impact["incoming_references"]}
        _resolution_check(value["resolutions"], references, existing_record_ids=existing_record_ids, removed_id=next(card["subject_record_id"] for card in cards if card["kind"] == "record_removed"))
        resolution_cards = {card["before"]["reference_id"]: card for card in cards if card["kind"] == "reference_resolution"}
        _equal(set(resolution_cards), set(references), "incomplete_removal_resolution", "diff.cards.resolutions")
        by_id = {item["reference_id"]: item for item in value["resolutions"]}
        for ref_id, reference in references.items():
            card = resolution_cards[ref_id]
            _equal(card["before"], reference, "unsafe_binding", "reference resolution")
            _equal(card["resolution"], by_id[ref_id], "unsafe_binding", "reference resolution")
        removed = next(card for card in cards if card["kind"] == "record_removed")
        _equal(removed["before"], impact["record"], "unsafe_binding", "removed record")
        _equal(removed["before"]["connections"], impact["outgoing_connections"], "unsafe_binding", "removed connections")
        _equal(value["diff"]["unresolved_reference_count"], sum(item["action"] == "accept_unresolved" for item in value["resolutions"]), "proposal_validation_failure", "unresolved_reference_count")
    for card in cards:
        change = next((item for item in core["changes"] if item["change_id"] == card["change_id"]), None)
        if change is None:
            _fail("unsafe_binding", "core change")
        expected_type = "add" if card["kind"] == "record_created" else "remove" if card["kind"] == "record_removed" else "update"
        _equal((change["change_type"], change["subject_id"]), (expected_type, card["subject_record_id"]), "unsafe_binding", "core change binding")
        document = card.get("after") if isinstance(card.get("after"), dict) and "content_digest" in card["after"] else card.get("before")
        if isinstance(document, dict) and "content_digest" in document:
            _equal(change["content_digest"], document["content_digest"], "unsafe_binding", "core change digest")
    _equal(value["diff"]["affected_record_count"], len({card["subject_record_id"] for card in cards}), "proposal_validation_failure", "affected_record_count")
    if value["mutation_kind"] != "remove":
        _equal(value["impact_binding"], None, "unsafe_binding", "impact_binding")
        _equal(value["resolutions"], [], "proposal_validation_failure", "resolutions")
    _equal(value["proposal_payload_digest"], canonical_digest({key: item for key, item in value.items() if key != "proposal_payload_digest"}), "idempotency_digest_conflict", "proposal_payload_digest")


def validate_editor_semantics(payload: Mapping[str, Any], *, proposal: Mapping[str, Any] | None = None, current_head: Mapping[str, Any] | None = None, current_workflow_version: int | None = None, impact: Mapping[str, Any] | None = None, existing_record_ids: set[str] | None = None) -> None:
    name = payload.get("contract_name")
    if name == "editor_record_view":
        _equal(payload["historical"], payload["viewed_revision"] != payload["head_revision"], "unsafe_binding", "historical")
        _equal(payload["editable"], not payload["historical"], "unsafe_binding", "editable")
        _record(payload["record"], "record")
        return
    if name == "editor_removal_impact":
        _record(payload["record"], "record")
        _equal(payload["binding"]["record_id"], payload["record"]["record_id"], "unsafe_binding", "binding.record_id")
        _equal(payload["binding"]["record_digest"], payload["record"]["content_digest"], "unsafe_binding", "binding.record_digest")
        _equal(payload["outgoing_connections"], payload["record"]["connections"], "unsafe_binding", "outgoing_connections")
        _unique(payload["outgoing_connections"], "connection_id", "outgoing_connections.connection_id")
        _unique(payload["incoming_references"], "reference_id", "incoming_references.reference_id")
        _equal(payload["impact_digest"], canonical_digest({key: payload[key] for key in ("record", "outgoing_connections", "incoming_references")}), "idempotency_digest_conflict", "impact_digest")
        return
    if name == "editor_proposal_view":
        _proposal(payload, impact=impact, existing_record_ids=existing_record_ids)
        return
    if name == "editor_proposal_approval_result":
        if payload["outcome"] != "published":
            _fail("proposal_approval_conflict", "outcome")
        published = payload["published_revision"]
        if published.get("immutable") is not True:
            _fail("unsafe_binding", "published_revision.immutable")
        if payload["proposal"]["proposal_version"] < 1:
            _fail("unsafe_binding", "proposal.proposal_version")
        return
    if name == "editor_proposal_rejection_result":
        if payload["outcome"] != "rejected" or payload["proposal"]["proposal_version"] < 1:
            _fail("proposal_approval_conflict", "rejection result")
        return
    if name in {"editor_record_create_request", "editor_record_edit_request", "editor_record_remove_request", "editor_proposal_correction_request"}:
        operation = payload["operation_request"]
        if operation["payload_digest"] != canonical_digest({key: value for key, value in payload.items() if key not in {"contract_name", "contract_version", "operation_request", "request_id", "idempotency_key", "payload_digest"}}):
            _fail("idempotency_digest_conflict", "operation_request.payload_digest")
        binding = payload["binding"]
        _equal(operation["expected_revision"], binding["base_revision"]["revision_id"], "unsafe_binding", "operation_request.expected_revision")
        _equal(operation["expected_editor_workflow_version"], binding["expected_editor_workflow_version"], "unsafe_binding", "workflow")
        if current_head is not None and binding["base_revision"] != current_head:
            _fail("stale_revision", "binding.base_revision")
        if current_workflow_version is not None and binding["expected_editor_workflow_version"] != current_workflow_version:
            _fail("workflow_conflict", "binding.expected_editor_workflow_version")
        if payload.get("candidate") is not None:
            _equal(payload["candidate"]["record_id"], binding["record_id"], "unsafe_binding", "candidate.record_id")
            _record(payload["candidate"], "candidate")
            if existing_record_ids is not None:
                for index, connection in enumerate(payload["candidate"]["connections"]):
                    if connection["target_record_id"] not in existing_record_ids:
                        _fail("invalid_connections", f"candidate.connections.{index}.target_record_id")
        if name == "editor_record_remove_request":
            if impact is None:
                _fail("unsafe_binding", "impact lookup")
            _equal(payload["impact_digest"], impact["impact_digest"], "unsafe_binding", "impact_digest")
            _equal(payload["impact_binding"], {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}, "unsafe_binding", "impact_binding")
            _resolution_check(payload["resolutions"], {item["reference_id"]: item for item in impact["incoming_references"]}, existing_record_ids=existing_record_ids, removed_id=binding["record_id"])
        return
    if name in {"editor_proposal_approval_request", "editor_proposal_rejection_request"}:
        if proposal is None:
            _fail("proposal_approval_conflict", "loaded proposal")
        _proposal(proposal, impact=impact, existing_record_ids=existing_record_ids)
        for key in ("source_revision", "base_revision", "expected_campaign_head", "proposal_payload_digest", "impact_digest", "impact_binding", "resolutions", "record_bindings", "authority_outcome", "visibility_outcome"):
            _equal(payload[key], proposal[key], "proposal_approval_conflict", key)
        _equal(payload["proposal"], {"proposal_id": proposal["proposal_id"], "proposal_version": proposal["proposal_version"]}, "proposal_approval_conflict", "proposal")
        _equal(payload["expected_editor_workflow_version"], proposal["editor_workflow_version"], "proposal_approval_conflict", "workflow")
        _equal(payload["diff_digest"], proposal["diff"]["diff_digest"], "proposal_approval_conflict", "diff_digest")
        _equal(payload["validation_status"], proposal["validation"]["status"], "proposal_approval_conflict", "validation_status")
        _equal(payload["validation_digest"], proposal["validation"]["validation_digest"], "proposal_approval_conflict", "validation_digest")
        _equal(payload["operation_request"]["subject_id"], proposal["proposal_id"], "proposal_approval_conflict", "operation_request.subject_id")
        _equal(payload["operation_request"]["expected_revision"], payload["base_revision"]["revision_id"], "proposal_approval_conflict", "operation_request.expected_revision")
        _equal(payload["operation_request"]["expected_editor_workflow_version"], payload["expected_editor_workflow_version"], "unsafe_binding", "operation_request.workflow")
        _equal(payload["operation_request"]["intent_digest"], payload["diff_digest"], "proposal_approval_conflict", "operation_request.intent_digest")
        if current_head is not None and payload["expected_campaign_head"] != current_head:
            _fail("stale_revision", "expected_campaign_head")
        if current_workflow_version is not None and payload["expected_editor_workflow_version"] != current_workflow_version:
            _fail("workflow_conflict", "expected_editor_workflow_version")
        _equal(payload["proposal_status"], proposal["core_proposal"]["proposal"]["status"], "proposal_approval_conflict", "proposal_status")
        _equal(payload["mutation_kind"], proposal["mutation_kind"], "proposal_approval_conflict", "mutation_kind")
        if payload.get("warden_confirmed") is not True:
            _fail("proposal_approval_conflict", "warden_confirmed")
        if name == "editor_proposal_approval_request":
            if payload["proposal_status"] != "needs_review" or payload["validation_status"] != "passed" or payload["validation_digest"] != proposal["validation"]["validation_digest"]:
                _fail("proposal_validation_failure", "approval gate")
            _equal(payload["diff"], proposal["diff"], "proposal_approval_conflict", "diff")
            _equal(payload["affected_record_count"], proposal["diff"]["affected_record_count"], "proposal_approval_conflict", "affected_record_count")
            _equal(payload["confirmed_change_ids"], [card["change_id"] for card in proposal["diff"]["cards"]], "proposal_approval_conflict", "confirmed_change_ids")
            _equal(payload["confirmed_authority_change_ids"], [item["change_id"] for item in proposal["diff"]["authority_changes"]], "proposal_approval_conflict", "confirmed_authority_change_ids")
            _equal(payload["confirmed_visibility_change_ids"], [item["change_id"] for item in proposal["diff"]["visibility_changes"]], "proposal_approval_conflict", "confirmed_visibility_change_ids")
