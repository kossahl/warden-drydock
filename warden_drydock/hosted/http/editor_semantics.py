"""Semantic guards for the structured record-editor wire contracts.

JSON Schema describes the closed shapes.  These checks bind the shapes to the
same typed mutation projection that is sent to the deterministic engine.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from warden_drydock.standalone import frontmatter
from .contracts import canonical_digest, text_digest
from .editor import _document, _typed_equal, document_digest, parse_document


class EditorSemanticError(ValueError):
    def __init__(self, category: str, path: str) -> None:
        self.category = category
        self.path = path
        super().__init__(path)


def _fail(category: str, path: str) -> None:
    raise EditorSemanticError(category, path)


def _equal(left: object, right: object, category: str, path: str) -> None:
    if not _typed_equal(left, right):
        _fail(category, path)


def _unique(items: list[Mapping[str, Any]], key: str, path: str, category: str = "proposal_validation_failure") -> None:
    values = [item.get(key) for item in items]
    if len(values) != len(set(values)):
        _fail(category, path)


def _record(value: Mapping[str, Any], path: str) -> None:
    try:
        normalized = _document(value)
    except ValueError as exc:
        if str(exc) == "authority_status_mismatch":
            _fail("invalid_authority_transition", f"{path}.authority")
        if str(exc) == "content_digest_mismatch":
            _fail("idempotency_digest_conflict", f"{path}.content_digest")
        _fail("proposal_validation_failure", path)
    except (KeyError, TypeError):
        _fail("proposal_validation_failure", path)
    _equal(document_digest(normalized), value.get("content_digest"), "idempotency_digest_conflict", f"{path}.content_digest")


def _property_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in ("displayed_name", "status", "authority", "visibility"):
        before_present = name in before
        after_present = name in after
        if not _typed_equal(before.get(name), after.get(name)) or before_present != after_present:
            result.append({
                "property": name,
                "before": before.get(name),
                "after": after.get(name),
                "before_present": before_present,
                "after_present": after_present,
            })
    for collection, identifier, value_key in (("fields", "field_id", "value"), ("sections", "section_id", "body")):
        old = {item[identifier]: item for item in before[collection]}
        new = {item[identifier]: item for item in after[collection]}
        member_ids = list(old) + [member_id for member_id in new if member_id not in old]
        missing = object()
        for member_id in member_ids:
            old_value = old.get(member_id, {}).get(value_key, missing)
            new_value = new.get(member_id, {}).get(value_key, missing)
            old_present = old_value is not missing
            new_present = new_value is not missing
            if not _typed_equal(old_value, new_value) or old_present != new_present:
                result.append({
                    "property": f"{collection}.{member_id}",
                    "before": None if old_value is missing else old_value,
                    "after": None if new_value is missing else new_value,
                    "before_present": old_present,
                    "after_present": new_present,
                })
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
    _equal(set(actual), set(expected), "mutation_consistency", "diff.cards.connection_delta")
    for key, card in actual.items():
        connection, effects = expected[key]
        _equal(card["connection"], connection, "mutation_consistency", "diff.cards.connection")
        _equal(card["derived_backlinks"], effects, "mutation_consistency", "diff.cards.derived_backlinks")
    for card in records:
        if card["kind"] == "record_updated":
            if card["before"] == card["after"]:
                _fail("proposal_validation_failure", "diff.cards.record_updated")
            _equal(card["property_changes"], _property_changes(card["before"], card["after"]), "mutation_consistency", "diff.cards.property_changes")


def _resolution_check(resolutions: list[Mapping[str, Any]], references: Mapping[str, Mapping[str, Any]], *, existing_record_ids: set[str] | None = None, removed_id: str | None = None) -> None:
    _unique(resolutions, "reference_id", "resolutions", "incomplete_removal_resolution")
    _equal({item["reference_id"] for item in resolutions}, set(references), "incomplete_removal_resolution", "resolutions")
    for item in resolutions:
        reference = references[item["reference_id"]]
        if reference.get("resolution_required") is not True:
            _fail("proposal_validation_failure", "resolutions.reference_id")
        if item["action"] == "accept_unresolved" and not reference["permitted_unresolved"]:
            _fail("proposal_validation_failure", "resolutions.action")
        if item["action"] == "redirect":
            target = item["replacement_target_record_id"]
            if target in {reference["source_record_id"], reference["target_record_id"], removed_id}:
                _fail("proposal_validation_failure", "resolutions.replacement_target_record_id")
            if existing_record_ids is not None and target not in existing_record_ids:
                _fail("invalid_connections", "resolutions.replacement_target_record_id")


def _source_record(source: str, subject_id: str, resolution_cards: list[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    try:
        metadata = frontmatter(source)
        if metadata.get("id") != subject_id or metadata.get("ownership") != "campaign":
            return None
        record = parse_document(source, subject_id, metadata.get("type"))
        headings = [line for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.startswith("# ")]
        if headings != [f"# {record['displayed_name']}"]:
            return None
        # Compare the source projection with the same section bodies exposed by
        # the editor wire contract.  The blank line after a heading is part of
        # that representation, not disposable whitespace.  Trimming it here
        # makes every ordinary source-preserving edit fail semantic validation.
        record = dict(record)
        record["content_digest"] = document_digest(record)
        used: set[int] = set()
        connections = []
        for connection in record["connections"]:
            replacement = None
            for index, card in enumerate(resolution_cards):
                reference = card.get("before")
                if index in used or not isinstance(reference, Mapping):
                    continue
                if all(connection[key] == reference[key] for key in ("target_record_id", "relationship", "state", "context")):
                    replacement = reference["connection_id"]
                    used.add(index)
                    break
            connections.append(dict(connection, connection_id=replacement or connection["connection_id"]))
        if connections != record["connections"]:
            record = dict(record, connections=connections)
            record["content_digest"] = document_digest(record)
        return record
    except (KeyError, TypeError, ValueError):
        return None


def _source_change_failure(value: Mapping[str, Any]) -> None:
    diff = value["diff"]
    cards = diff["cards"]
    sources = diff.get("source_changes", [])
    subjects = {card["subject_record_id"] for card in cards}
    if {source.get("subject_record_id") for source in sources} != subjects:
        _fail("mutation_consistency", "diff.source_changes")
    if len({source["subject_record_id"] for source in sources}) != len(sources):
        _fail("mutation_consistency", "diff.source_changes")
    by_subject = {source["subject_record_id"]: source for source in sources}
    for subject, source in by_subject.items():
        subject_cards = [card for card in cards if card["subject_record_id"] == subject]
        record_cards = [card for card in subject_cards if card["kind"] in {"record_created", "record_updated", "record_removed"}]
        if len({card["kind"] for card in record_cards}) > 1:
            _fail("mutation_consistency", "diff.source_changes")
        kind = record_cards[0]["kind"] if record_cards else "record_updated"
        expected_change_type = {"record_created": "create", "record_updated": "update", "record_removed": "delete"}[kind]
        if source["change_type"] != expected_change_type:
            _fail("mutation_consistency", f"diff.source_changes.{subject}.change_type")
        before_source, after_source = source.get("before_source"), source.get("after_source")
        expected_presence = {
            "record_created": (None, False, True),
            "record_updated": (None, True, True),
            "record_removed": (None, True, False),
        }[kind]
        if (before_source is None) != (not expected_presence[1]) or (after_source is None) != (not expected_presence[2]):
            _fail("mutation_consistency", f"diff.source_changes.{subject}")
        for side, source_text in (("before", before_source), ("after", after_source)):
            if source_text is None:
                continue
            record = _source_record(source_text, subject)
            if record is None:
                _fail("mutation_consistency", f"diff.source_changes.{subject}.{side}_source")
            structured = next(
                (card.get(side) for card in subject_cards if isinstance(card.get(side), Mapping) and "record_id" in card[side]),
                None,
            )
            if structured is not None:
                for key in ("record_id", "record_type", "displayed_name", "ownership", "status", "authority", "visibility", "fields", "sections"):
                    _equal(record[key], structured[key], "mutation_consistency", f"diff.source_changes.{subject}.{side}_source.{key}")
                if len(record["connections"]) != len(structured["connections"]):
                    _fail("mutation_consistency", f"diff.source_changes.{subject}.{side}_source.connections")
                for actual, expected in zip(record["connections"], structured["connections"]):
                    _equal(
                        {key: actual[key] for key in ("target_record_id", "relationship", "state", "context")},
                        {key: expected[key] for key in ("target_record_id", "relationship", "state", "context")},
                        "mutation_consistency", f"diff.source_changes.{subject}.{side}_source.connections",
                    )


def _impact_digest(value: Mapping[str, Any]) -> str:
    return canonical_digest({
        key: value[key]
        for key in ("contract_name", "contract_version", "record", "outgoing_connections", "incoming_references")
        if key in value
    })


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
    diff_digest_input = {key: value["diff"][key] for key in ("cards", "affected_record_count", "authority_changes", "visibility_changes", "unresolved_reference_count", "impact_digest")}
    if "source_changes" in value["diff"]:
        diff_digest_input["source_changes"] = value["diff"]["source_changes"]
    _equal(value["diff"]["diff_digest"], canonical_digest(diff_digest_input), "idempotency_digest_conflict", "diff.diff_digest")
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
    if status in {"needs_review", "approving", "approved"} and any(
        isinstance(finding, Mapping) and finding.get("severity") == "error"
        for finding in validation.get("findings", [])
    ):
        _fail("proposal_validation_failure", "validation.findings")
    if status in {"approving", "approved"} and any(
        isinstance(finding, Mapping) and finding.get("severity") == "warning"
        for finding in validation.get("findings", [])
    ):
        _fail("proposal_validation_failure", "validation.findings")
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
    _connection_cards(cards)
    actual_authority: set[tuple[Any, ...]] = set()
    actual_visibility: set[tuple[Any, ...]] = set()
    for card in cards:
        for side in ("before", "after"):
            record = card.get(side)
            if isinstance(record, Mapping) and "record_id" in record:
                _equal(record["record_id"], card["subject_record_id"], "unsafe_binding", f"diff.cards.{card['change_id']}.{side}.record_id")
        if (
            card["kind"] == "record_updated"
            and isinstance(card.get("before"), Mapping)
            and isinstance(card.get("after"), Mapping)
            and card["before"]["record_type"] != card["after"]["record_type"]
        ):
            _fail("proposal_validation_failure", f"diff.cards.{card['change_id']}.after.record_type")
        for key in ("before", "after"):
            if isinstance(card.get(key), dict) and "content_digest" in card[key]:
                _record(card[key], f"card.{card['change_id']}.{key}")
        if card["kind"] in {"record_created", "record_updated", "record_removed"}:
            before, after = card.get("before"), card.get("after")
            before_authority = before["authority"] if isinstance(before, dict) else "absent"
            if after is None:
                authority_transition = before_authority in {"canon", "revealed"}
                after_authority = "absent"
            elif before is None:
                authority_transition = after["authority"] in {"canon", "revealed"}
                after_authority = after["authority"]
            else:
                authority_transition = before_authority != after["authority"]
                after_authority = after["authority"]
            if authority_transition:
                actual_authority.add((card["change_id"], card["subject_record_id"], before_authority, after_authority))
            if isinstance(before, dict) and isinstance(after, dict) and before["visibility"] != after["visibility"]:
                actual_visibility.add((card["change_id"], card["subject_record_id"], json.dumps(before["visibility"], sort_keys=True), json.dumps(after["visibility"], sort_keys=True)))
    declared_authority = {(x["change_id"], x["record_id"], x["from"], x["to"]) for x in value["diff"]["authority_changes"]}
    declared_visibility = {(x["change_id"], x["record_id"], json.dumps(x["before"], sort_keys=True), json.dumps(x["after"], sort_keys=True)) for x in value["diff"]["visibility_changes"]}
    _equal(actual_authority, declared_authority, "proposal_validation_failure", "authority_changes")
    _equal(actual_visibility, declared_visibility, "proposal_validation_failure", "visibility_changes")
    _equal(value["authority_outcome"], value["diff"]["authority_changes"], "unsafe_binding", "authority_outcome")
    _equal(value["visibility_outcome"], value["diff"]["visibility_changes"], "unsafe_binding", "visibility_outcome")
    for change in value["diff"]["authority_changes"]:
        card = next((card for card in cards if card["change_id"] == change["change_id"]), None)
        before_authority = card["before"]["authority"] if isinstance(card, dict) and isinstance(card.get("before"), dict) else "absent"
        after = card.get("after") if isinstance(card, dict) else None
        after_authority = "absent" if isinstance(card, dict) and card.get("kind") == "record_removed" and after is None else after.get("authority") if isinstance(after, dict) else None
        if card is None or after_authority is None or (change["record_id"], change["from"], change["to"]) != (card["subject_record_id"], before_authority, after_authority):
            _fail("unsafe_binding", "authority change card")
    for change in value["diff"]["visibility_changes"]:
        card = next((card for card in cards if card["change_id"] == change["change_id"]), None)
        broadens = change["before"]["audience"] == "warden" and change["after"]["audience"] != "warden"
        if card is None or card.get("before") is None or card.get("after") is None or (change["record_id"], change["before"], change["after"]) != (card["subject_record_id"], card["before"]["visibility"], card["after"]["visibility"]) or change["audience_broadens"] != broadens:
            _fail("unsafe_binding", "visibility change card")
    kinds = {"create": {"record_created", "connection_added"}, "edit": {"record_updated", "connection_added", "connection_updated", "connection_removed"}, "remove": {"record_removed", "connection_removed", "reference_resolution"}}
    if value["mutation_kind"] not in kinds or any(card["kind"] not in kinds[value["mutation_kind"]] for card in cards):
        _fail("proposal_validation_failure", "diff.cards.kind")
    primary_kind = {"create": "record_created", "edit": "record_updated", "remove": "record_removed"}.get(value["mutation_kind"])
    if primary_kind is None or sum(card["kind"] in {"record_created", "record_updated", "record_removed"} for card in cards) != 1 or sum(card["kind"] == primary_kind for card in cards) != 1:
        _fail("proposal_validation_failure", "diff.cards.record_mutation")
    record_cards = {
        card["subject_record_id"]: card
        for card in cards
        if card["kind"] in {"record_created", "record_updated", "record_removed"}
    }
    source_changes = {source["subject_record_id"]: source for source in value["diff"].get("source_changes", [])}
    for index, binding in enumerate(value["record_bindings"]):
        card = record_cards.get(binding["record_id"])
        if value["mutation_kind"] == "create":
            if binding["record_digest"] is not None:
                _fail("unsafe_binding", f"record_bindings.{index}.record_digest")
        elif card is not None and not isinstance(card.get("before"), Mapping):
            _fail("unsafe_binding", f"record_bindings.{index}.record_digest")
        elif card is not None:
            _equal(binding["record_digest"], card["before"]["content_digest"], "idempotency_digest_conflict", f"record_bindings.{index}.record_digest")
        else:
            source = source_changes.get(binding["record_id"])
            if source is None or source.get("before_source") is None:
                _fail("unsafe_binding", f"record_bindings.{index}.record_digest")
            resolution_cards = [
                item for item in cards
                if item["kind"] == "reference_resolution" and item["subject_record_id"] == binding["record_id"]
            ]
            parsed = _source_record(source["before_source"], binding["record_id"], resolution_cards)
            if parsed is None:
                _fail("unsafe_binding", f"record_bindings.{index}.record_digest")
            _equal(binding["record_digest"], parsed["content_digest"], "idempotency_digest_conflict", f"record_bindings.{index}.record_digest")
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
        record_card = record_cards.get(card["subject_record_id"])
        if card["kind"] in {"record_created", "record_updated", "record_removed"}:
            before = card.get("before")
            after = card.get("after")
            expected = {
                "change_type": "add" if card["kind"] == "record_created" else "remove" if card["kind"] == "record_removed" else "update",
                "subject_id": card["subject_record_id"],
                "from_authority": before.get("authority") if isinstance(before, Mapping) else "absent",
                "to_authority": after.get("authority") if isinstance(after, Mapping) else "absent",
                "content_digest": (after or before).get("content_digest") if isinstance(after or before, Mapping) else None,
            }
        elif card["kind"] in {"connection_added", "connection_updated", "connection_removed"}:
            expected = {
                "change_type": "update",
                "subject_id": card["subject_record_id"],
                "from_authority": (record_card.get("before") or record_card.get("after"))["authority"] if record_card else "preparation",
                "to_authority": (record_card.get("after") or record_card.get("before"))["authority"] if record_card else "preparation",
                "content_digest": (record_card.get("after") or record_card.get("before")).get("content_digest") if record_card else canonical_digest(card["connection"]),
            }
        elif card["kind"] == "reference_resolution":
            expected = {
                "change_type": "update",
                "subject_id": card["subject_record_id"],
                "from_authority": "preparation",
                "to_authority": "preparation",
                "content_digest": canonical_digest(card["after"]),
            }
        else:
            _fail("unsafe_binding", "core change kind")
        for key, expected_value in expected.items():
            _equal(change[key], expected_value, "unsafe_binding", f"core change {key}")
    _equal(value["diff"]["affected_record_count"], len({card["subject_record_id"] for card in cards}), "proposal_validation_failure", "affected_record_count")
    if value["mutation_kind"] != "remove":
        _equal(value["impact_binding"], None, "unsafe_binding", "impact_binding")
        _equal(value["resolutions"], [], "proposal_validation_failure", "resolutions")
    _equal(value["proposal_payload_digest"], canonical_digest({key: item for key, item in value.items() if key != "proposal_payload_digest"}), "idempotency_digest_conflict", "proposal_payload_digest")


def validate_editor_semantics(
    payload: Mapping[str, Any], *, proposal: Mapping[str, Any] | None = None,
    current_head: Mapping[str, Any] | None = None,
    current_workflow_version: int | None = None,
    record_digest_at_base: str | None = None,
    stored_receipt: Mapping[str, Any] | None = None,
    required_reference_ids: set[str] | None = None,
    impact: Mapping[str, Any] | None = None,
    existing_record_ids: set[str] | None = None,
) -> None:
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
        for index, reference in enumerate(payload["incoming_references"]):
            _equal(reference["target_record_id"], payload["binding"]["record_id"], "unsafe_binding", f"incoming_references.{index}.target_record_id")
            if reference["resolution_required"] is not True or reference["permitted_unresolved"] is not False:
                _fail("proposal_validation_failure", f"incoming_references.{index}")
        _equal(payload["impact_digest"], _impact_digest(payload), "idempotency_digest_conflict", "impact_digest")
        return
    if name == "editor_proposal_view":
        _source_change_failure(payload)
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
        if name in {"editor_record_create_request", "editor_record_edit_request", "editor_record_remove_request"}:
            _equal(operation["subject_id"], payload["binding"]["record_id"], "unsafe_binding", "operation_request.subject_id")
        if name == "editor_proposal_correction_request" and proposal is not None:
            if (
                payload["prior_proposal"] != {"proposal_id": proposal["proposal_id"], "proposal_version": proposal["proposal_version"]}
                or payload["mutation_kind"] != proposal["mutation_kind"]
            ):
                _fail("invalid_correction", "prior_proposal")
        if stored_receipt is not None and stored_receipt.get("idempotency_key") == operation["idempotency_key"] and stored_receipt.get("payload_digest") != operation["payload_digest"]:
            _fail("replay_mismatch", "operation_request.payload_digest")
        if operation["payload_digest"] != canonical_digest({key: value for key, value in payload.items() if key not in {"contract_name", "contract_version", "operation_request", "request_id", "idempotency_key", "payload_digest"}}):
            _fail("idempotency_digest_conflict", "operation_request.payload_digest")
        binding = payload["binding"]
        _equal(operation["expected_revision"], binding["base_revision"]["revision_id"], "unsafe_binding", "operation_request.expected_revision")
        _equal(operation["expected_editor_workflow_version"], binding["expected_editor_workflow_version"], "unsafe_binding", "workflow")
        if current_head is not None and binding["base_revision"] != current_head:
            _fail("stale_revision", "binding.base_revision")
        if current_workflow_version is not None and binding["expected_editor_workflow_version"] != current_workflow_version:
            _fail("workflow_conflict", "binding.expected_editor_workflow_version")
        if record_digest_at_base is not None and binding.get("record_digest") != record_digest_at_base:
            _fail("stale_record_digest", "binding.record_digest")
        if name == "editor_proposal_correction_request" and proposal is not None:
            _equal(operation["subject_id"], proposal["proposal_id"], "unsafe_binding", "operation_request.subject_id")
        if payload.get("candidate") is not None:
            _equal(payload["candidate"]["record_id"], binding["record_id"], "unsafe_binding", "candidate.record_id")
            _record(payload["candidate"], "candidate")
            if existing_record_ids is not None:
                for index, connection in enumerate(payload["candidate"]["connections"]):
                    if connection["target_record_id"] not in existing_record_ids:
                        _fail("invalid_connections", f"candidate.connections.{index}.target_record_id")
        if name == "editor_record_remove_request":
            if binding["record_digest"] is None:
                _fail("unsafe_binding", "binding.record_digest")
            if required_reference_ids is not None:
                _equal({item["reference_id"] for item in payload["resolutions"]}, required_reference_ids, "incomplete_removal_resolution", "resolutions")
            elif impact is None:
                _fail("unsafe_binding", "impact lookup")
            if impact is not None:
                _equal(payload["impact_digest"], impact["impact_digest"], "unsafe_binding", "impact_digest")
                _equal(payload["impact_binding"], {"binding": impact["binding"], "impact_digest": impact["impact_digest"]}, "unsafe_binding", "impact_binding")
                _resolution_check(payload["resolutions"], {item["reference_id"]: item for item in impact["incoming_references"]}, existing_record_ids=existing_record_ids, removed_id=binding["record_id"])
        if name == "editor_proposal_correction_request" and payload["mutation_kind"] == "remove":
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
        operation = payload["operation_request"]
        if stored_receipt is not None and stored_receipt.get("idempotency_key") == operation["idempotency_key"] and stored_receipt.get("payload_digest") != operation["payload_digest"]:
            _fail("replay_mismatch", "operation_request.payload_digest")
        operation_projection = {
            key: value
            for key, value in payload.items()
            if key not in {"contract_name", "contract_version", "operation_request", "request_id", "idempotency_key", "payload_digest"}
        }
        _equal(operation["payload_digest"], canonical_digest(operation_projection), "idempotency_digest_conflict", "operation_request.payload_digest")
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
            if (
                payload["proposal_status"] != "needs_review"
                or payload["validation_status"] != "passed"
                or payload["validation_digest"] != proposal["validation"]["validation_digest"]
                or proposal["validation"]["findings"]
            ):
                _fail("proposal_validation_failure", "approval gate")
            _equal(payload["diff"], proposal["diff"], "proposal_approval_conflict", "diff")
            _equal(payload["affected_record_count"], proposal["diff"]["affected_record_count"], "proposal_approval_conflict", "affected_record_count")
            _equal(payload["confirmed_change_ids"], [card["change_id"] for card in proposal["diff"]["cards"]], "proposal_approval_conflict", "confirmed_change_ids")
            _equal(payload["confirmed_authority_change_ids"], [item["change_id"] for item in proposal["diff"]["authority_changes"]], "proposal_approval_conflict", "confirmed_authority_change_ids")
            _equal(payload["confirmed_visibility_change_ids"], [item["change_id"] for item in proposal["diff"]["visibility_changes"]], "proposal_approval_conflict", "confirmed_visibility_change_ids")
