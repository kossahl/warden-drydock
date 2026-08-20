from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Mapping


_PRIVATE_PATH = re.compile(r"[\\/]")
_REQUEST_META = {
    "contract_name",
    "contract_version",
    "request_id",
    "idempotency_key",
    "payload_digest",
    "operation_request",
}


@dataclass(frozen=True)
class SemanticFinding:
    rule_id: str
    category: str
    code: str


class HTTPContractSemanticError(ValueError):
    def __init__(self, finding: SemanticFinding) -> None:
        self.finding = finding
        super().__init__(finding.code)


def normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def text_digest(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def append_draft(before: str, draft: str) -> str:
    return normalize_text(before) + "\n\n## Proposed addition\n\n" + normalize_text(draft)


def request_digest_input(payload: Mapping[str, object]) -> object:
    if "input" in payload:
        return payload["input"]
    return {key: value for key, value in payload.items() if key not in _REQUEST_META}


def validate_http_semantics(
    payload: Mapping[str, object],
    *,
    context: Mapping[str, object] | None = None,
) -> None:
    context = context or {}
    name = payload.get("contract_name")
    _require_route_context(name, context)
    if name in {
        "provider_consent_request",
        "campaign_create_request",
        "proposal_create_request",
        "proposal_correction_request",
        "proposal_rejection_request",
        "proposal_approval_request",
    }:
        _operation_digest(payload)
    if name == "provider_consent_request":
        _consent_identity(payload, context)
    if name == "provider_readiness_response":
        _readiness_truth(payload)
    if name == "generation_view":
        _generation_digests(payload)
        _source_set_binding(payload, context)
    if name in {"proposal_view", "proposal_approval_result"}:
        proposal = payload if name == "proposal_view" else payload["proposal"]
        _proposal_digests(proposal)
        _proposal_view_binding(proposal, context)
        _approval_result(payload) if name == "proposal_approval_result" else None
    if name == "proposal_create_request":
        _proposal_provenance(payload, context)
        _single_record_append(payload, context)
    if name == "proposal_correction_request":
        _proposal_operation_equality(payload)
        _correction_binding(payload, context)
    if name == "proposal_rejection_request":
        _proposal_operation_equality(payload)
        _stored_proposal_binding(payload, context)
    if name == "proposal_approval_request":
        _approval_equality(payload)
        _stored_proposal_binding(payload, context)
    if name in {"error_response", "proposal_approval_result"}:
        _safe_error(payload)
    _path_body_equality(payload, context)
    _sse_resume(context)


def _fail(rule_id: str, category: str, code: str) -> None:
    raise HTTPContractSemanticError(SemanticFinding(rule_id, category, code))


def _require_route_context(name: object, context: Mapping[str, object]) -> None:
    required = {
        "provider_consent_request": ("consent_identity_digest",),
        "ask_start_request": ("path_params",),
        "generation_view": ("source_envelope",),
        "proposal_view": ("stored_proposal", "generation", "record", "path_params"),
        "proposal_approval_result": ("stored_proposal", "generation", "record", "path_params"),
        "proposal_create_request": ("generation", "record", "proposal", "path_params"),
        "proposal_correction_request": ("proposal", "path_params"),
        "proposal_rejection_request": ("proposal", "path_params"),
        "proposal_approval_request": ("proposal", "path_params"),
    }.get(name, ())
    missing = [key for key in required if key not in context]
    if not missing:
        return
    if name == "provider_consent_request":
        _fail("http_consent_identity", "capability_rejected", "consent_identity_unavailable")
    if name == "generation_view":
        _fail("http_source_set_binding", "source_digest_conflict", "source_envelope_unavailable")
    if "path_params" in missing:
        _fail("http_path_body_equality", "unsafe_binding", "route_binding_unavailable")
    if "generation" in missing:
        _fail("http_proposal_provenance", "source_digest_conflict", "generation_binding_unavailable")
    if name in {"proposal_view", "proposal_approval_result"}:
        _fail("http_proposal_provenance", "source_digest_conflict", "stored_provenance_unavailable")
    _fail("http_single_record_append", "proposal_validation_failure", "proposal_context_unavailable")


def _operation_digest(payload: Mapping[str, object]) -> None:
    operation = payload.get("operation_request", payload)
    if operation["payload_digest"] != canonical_digest(request_digest_input(payload)):
        _fail("http_operation_digest", "idempotency_digest_conflict", "payload_digest_mismatch")


def _consent_identity(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    current = context.get("consent_identity_digest")
    if payload["input"]["consent_identity_digest"] != current:
        _fail("http_consent_identity", "capability_rejected", "explicit_consent_required")


def _readiness_truth(payload: Mapping[str, object]) -> None:
    configured = payload["provider_configured"]
    available = payload["provider_available"]
    consent = payload["consent_current"]
    identity = payload["consent_identity_digest"]
    expected_ai = bool(configured and available and consent and identity is not None)
    if payload["ai_available"] is not expected_ai or (available and not configured) or (consent and (not configured or identity is None)):
        _fail("http_consent_identity", "capability_rejected", "provider_readiness_inconsistent")


def _generation_digests(payload: Mapping[str, object]) -> None:
    for source in payload["sources"]:
        if source["excerpt_digest"] != text_digest(source["excerpt"]):
            _fail("http_content_digests", "unsafe_binding", "excerpt_digest_mismatch")
    content = payload["terminal_content"]
    digest = payload["terminal_content_digest"]
    if (content is None) != (digest is None) or (content is not None and digest != text_digest(content)):
        _fail("http_content_digests", "unsafe_binding", "terminal_content_digest_mismatch")


def _source_set_binding(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    envelope = context["source_envelope"]
    if envelope.get("session_id") is not None:
        _fail("http_source_set_binding", "source_digest_conflict", "session_source_not_allowed")
    source_values = [{
        "authority": item["authority"],
        "digest": item["digest"],
        "order": item["order"],
        "source_id": item["source_id"],
    } for item in envelope["sources"]]
    computed = canonical_digest({
        "campaign_id": envelope["campaign_id"],
        "revision_id": envelope["revision_id"],
        "retrieval_policy_version": envelope["retrieval_policy_version"],
        "session_id": envelope.get("session_id"),
        "sources": source_values,
    })
    public_sources = [{
        "source_id": item["source_id"],
        "authority": item["authority"],
        "revision_id": payload["source_revision"],
        "order": item["order"],
        "excerpt": item["text"],
        "excerpt_digest": item["digest"],
    } for item in envelope["sources"]]
    if not (
        computed == envelope["source_set_digest"] == payload["source_set_digest"]
        and payload["campaign_id"] == envelope["campaign_id"]
        and payload["source_revision"] == envelope["revision_id"]
        and payload["sources"] == public_sources
    ):
        _fail("http_source_set_binding", "source_digest_conflict", "source_set_binding_mismatch")


def _proposal_digests(proposal: Mapping[str, object]) -> None:
    changes = proposal["exact_diff"]
    if len(changes) != 1:
        _fail("http_single_record_append", "proposal_validation_failure", "single_change_required")
    change = changes[0]
    if change["from_authority"] != change["to_authority"]:
        _fail("http_single_record_append", "proposal_validation_failure", "authority_transition_not_supported")
    if change["before_digest"] != text_digest(change["before_content"]) or change["after_digest"] != text_digest(change["after_content"]):
        _fail("http_content_digests", "unsafe_binding", "display_content_digest_mismatch")
    diff_value = {
        "change_id": change["change_id"],
        "change_kind": change["change_type"],
        "expected_content_digest": change["before_digest"],
        "record_type": change["record_type"],
        "replacement_digest": change["after_digest"],
        "subject_id": change["subject_id"],
    }
    proposal_value = {
        "id": change["change_id"],
        "subject": change["subject_id"],
        "replacement": change["after_content"],
        "expected": change["before_digest"],
        "kind": change["change_type"],
        "record_type": change["record_type"],
    }
    if proposal["diff_digest"] != canonical_digest(diff_value) or proposal["proposal_payload_digest"] != canonical_digest(proposal_value):
        _fail("http_proposal_digests", "proposal_approval_conflict", "proposal_digest_mismatch")


def _proposal_view_binding(proposal: Mapping[str, object], context: Mapping[str, object]) -> None:
    stored = context["stored_proposal"]
    generation = context["generation"]
    record = context["record"]
    fields = (
        "proposal_id", "proposal_version", "campaign_id", "generation_id",
        "source_revision", "base_revision", "source_set_digest",
        "terminal_draft_digest", "diff_digest", "proposal_payload_digest",
        "status", "validation_status", "published_revision_id",
    )
    if any(proposal[field] != stored[field] for field in fields) or proposal["exact_diff"] != stored["exact_diff"]:
        _fail("http_proposal_provenance", "source_digest_conflict", "stored_proposal_mismatch")
    if not (
        proposal["generation_id"] == generation["generation_id"]
        and proposal["campaign_id"] == generation["campaign_id"]
        and proposal["source_revision"] == generation["source_revision"]
        and proposal["source_set_digest"] == generation["source_set_digest"]
        and proposal["terminal_draft_digest"] == generation["terminal_content_digest"]
        and proposal["base_revision"] == proposal["source_revision"]
    ):
        _fail("http_source_set_binding", "source_digest_conflict", "proposal_generation_mismatch")
    change = proposal["exact_diff"][0]
    if not (
        record["campaign_id"] == proposal["campaign_id"]
        and record["revision_id"] == proposal["base_revision"]
        and change["subject_id"] == record["record_id"]
        and change["record_type"] == record["record_type"]
        and change["before_content"] == normalize_text(record["content"])
        and change["from_authority"] == record["authority"]
        and change["to_authority"] == record["authority"]
    ):
        _fail("http_single_record_append", "proposal_validation_failure", "record_authority_binding_mismatch")
    path = context["path_params"]
    if "proposal_id" in path and path["proposal_id"] != proposal["proposal_id"]:
        _fail("http_path_body_equality", "unsafe_binding", "path_body_mismatch")
    if "proposal_version" in path and int(path["proposal_version"]) != proposal["proposal_version"]:
        _fail("http_path_body_equality", "unsafe_binding", "path_body_mismatch")


def _proposal_provenance(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    generation = context["generation"]
    expected = (
        generation["generation_id"],
        generation["campaign_id"],
        generation["source_revision"],
        generation["source_set_digest"],
        generation["terminal_content_digest"],
    )
    actual = (
        payload["generation_id"],
        payload["campaign_id"],
        payload["source_revision"],
        payload["source_set_digest"],
        payload["terminal_draft_digest"],
    )
    if actual != expected or generation["status"] != "complete" or payload["base_revision"] != payload["source_revision"]:
        _fail("http_proposal_provenance", "source_digest_conflict", "generation_binding_mismatch")


def _single_record_append(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    record = context["record"]
    generation = context["generation"]
    proposed = context["proposal"]
    change = proposed["exact_diff"][0]
    if (
        proposed["proposal_id"] != payload["proposal_id"]
        or proposed["campaign_id"] != payload["campaign_id"]
        or proposed["generation_id"] != payload["generation_id"]
        or proposed["source_revision"] != payload["source_revision"]
        or proposed["base_revision"] != payload["base_revision"]
        or proposed["source_set_digest"] != payload["source_set_digest"]
        or proposed["terminal_draft_digest"] != payload["terminal_draft_digest"]
        or record["campaign_id"] != payload["campaign_id"]
        or record["revision_id"] != payload["base_revision"]
        or payload["subject_id"] != record["record_id"]
        or change["subject_id"] != record["record_id"]
        or change["record_type"] != record["record_type"]
        or change["from_authority"] != record["authority"]
        or change["to_authority"] != record["authority"]
        or change["before_content"] != normalize_text(record["content"])
        or change["after_content"] != append_draft(record["content"], generation["terminal_content"])
    ):
        _fail("http_single_record_append", "proposal_validation_failure", "append_transform_mismatch")


def _proposal_operation_equality(payload: Mapping[str, object]) -> None:
    operation = payload["operation_request"]
    if operation.get("subject_id") != payload["proposal_id"] or operation.get("expected_revision") != payload["base_revision"]:
        _fail("http_correction_binding", "proposal_approval_conflict", "proposal_operation_binding_mismatch")


def _correction_binding(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    proposal = context["proposal"]
    change = proposal["exact_diff"][0]
    if (
        payload["proposal_id"] != proposal["proposal_id"]
        or payload["proposal_version"] != proposal["proposal_version"]
        or payload["source_revision"] != proposal["source_revision"]
        or payload["base_revision"] != proposal["base_revision"]
        or payload["change_id"] != change["change_id"]
        or payload["subject_id"] != change["subject_id"]
    ):
        _fail("http_correction_binding", "proposal_approval_conflict", "correction_change_mismatch")


def _stored_proposal_binding(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    proposal = context["proposal"]
    pairs = (
        (payload["proposal_id"], proposal["proposal_id"]),
        (payload["proposal_version"], proposal["proposal_version"]),
        (payload["source_revision"], proposal["source_revision"]),
        (payload["base_revision"], proposal["base_revision"]),
    )
    if any(actual != expected for actual, expected in pairs):
        _fail("http_correction_binding", "proposal_approval_conflict", "stored_proposal_binding_mismatch")
    if payload.get("contract_name") == "proposal_approval_request" and (
        payload["diff_digest"] != proposal["diff_digest"]
        or payload["proposal_payload_digest"] != proposal["proposal_payload_digest"]
    ):
        _fail("http_proposal_digests", "proposal_approval_conflict", "stored_proposal_digest_mismatch")


def _approval_equality(payload: Mapping[str, object]) -> None:
    operation = payload["operation_request"]
    if not (
        operation.get("subject_id") == payload["proposal_id"]
        and operation.get("intent_digest") == payload["diff_digest"]
        and operation.get("expected_revision") == payload["expected_campaign_head"] == payload["base_revision"]
    ):
        _fail("http_approval_equality", "proposal_approval_conflict", "approval_binding_mismatch")


def _approval_result(payload: Mapping[str, object]) -> None:
    proposal = payload["proposal"]
    if payload["outcome"] == "published":
        revision = payload["published_revision"]
        if revision is None or proposal["published_revision_id"] != revision["revision_id"] or revision["validation_status"] != "passed":
            _fail("http_approval_result", "proposal_approval_conflict", "published_revision_binding_mismatch")
    elif payload["published_revision"] is not None:
        _fail("http_approval_result", "proposal_approval_conflict", "conflict_publication_mismatch")


def _path_body_equality(payload: Mapping[str, object], context: Mapping[str, object]) -> None:
    path = context.get("path_params", {})
    for field, value in path.items():
        if field in payload and str(payload[field]) != str(value):
            _fail("http_path_body_equality", "unsafe_binding", "path_body_mismatch")


def _sse_resume(context: Mapping[str, object]) -> None:
    if not any(key in context for key in ("after", "last_event_id", "last_sequence")):
        return
    after = context.get("after")
    event_id = context.get("last_event_id")
    last = context.get("last_sequence", 0)
    if after is not None and event_id is not None and after != event_id:
        _fail("http_sse_resume", "stream_sequence_conflict", "resume_sequence_mismatch")
    observed = event_id if event_id is not None else (after if after is not None else 0)
    if not isinstance(observed, int) or observed < 0:
        _fail("http_sse_resume", "unsafe_binding", "resume_sequence_invalid")
    if observed > last:
        _fail("http_sse_resume", "stream_sequence_conflict", "resume_sequence_gap")


def _safe_error(payload: Mapping[str, object]) -> None:
    error = payload.get("error")
    if error is None and payload.get("contract_name") == "proposal_approval_result":
        return
    if error is not None and set(error) - {"category", "code", "stage", "request_id", "retryable"}:
        _fail("http_safe_error", "unsafe_binding", "unsafe_error_field")


IMPLEMENTED_INVARIANTS = frozenset({
    "http_operation_digest",
    "http_content_digests",
    "http_source_set_binding",
    "http_proposal_digests",
    "http_consent_identity",
    "http_proposal_provenance",
    "http_single_record_append",
    "http_correction_binding",
    "http_approval_equality",
    "http_approval_result",
    "http_path_body_equality",
    "http_sse_resume",
    "http_safe_error",
})

INVARIANT_APPLIES_TO = frozenset({
    ("http_operation_digest", name) for name in (
        "provider_consent_request", "campaign_create_request", "proposal_create_request",
        "proposal_correction_request", "proposal_rejection_request", "proposal_approval_request")
} | {
    ("http_content_digests", name) for name in ("generation_view", "proposal_view", "proposal_approval_result")
} | {
    ("http_source_set_binding", name) for name in ("generation_view", "proposal_create_request", "proposal_view")
} | {
    ("http_proposal_digests", name) for name in ("proposal_view", "proposal_approval_request", "proposal_approval_result")
} | {
    ("http_consent_identity", name) for name in ("provider_consent_request", "provider_readiness_response")
} | {
    ("http_proposal_provenance", name) for name in ("proposal_create_request", "proposal_view")
} | {
    ("http_single_record_append", "proposal_create_request"),
    ("http_correction_binding", "proposal_correction_request"),
    ("http_correction_binding", "proposal_rejection_request"),
    ("http_approval_equality", "proposal_approval_request"),
    ("http_approval_result", "proposal_approval_result"),
    ("http_sse_resume", "generation_event"),
    ("http_safe_error", "error_response"),
    ("http_safe_error", "proposal_approval_result"),
} | {
    ("http_path_body_equality", name) for name in (
        "ask_start_request", "proposal_create_request", "proposal_correction_request",
        "proposal_rejection_request", "proposal_approval_request")
})
