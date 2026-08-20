from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from warden_drydock.hosted.http.contracts import (
    HTTPContractSemanticError,
    IMPLEMENTED_INVARIANTS,
    INVARIANT_APPLIES_TO,
    append_draft,
    validate_http_semantics,
)


ROOT = Path(__file__).parents[3] / "docs" / "contracts" / "hosted" / "http" / "v1"


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def request_payload(payload: dict[str, object]) -> object:
    if "input" in payload:
        return payload["input"]
    excluded = {
        "contract_name",
        "contract_version",
        "request_id",
        "idempotency_key",
        "payload_digest",
        "operation_request",
    }
    return {key: value for key, value in payload.items() if key not in excluded}


class HostedHTTPContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "http.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)
        examples = json.loads((ROOT / "examples.json").read_text(encoding="utf-8"))
        cls.examples = {item["name"]: item["payload"] for item in examples["examples"]}

    def assert_valid(self, payload: object) -> None:
        errors = sorted(self.validator.iter_errors(payload), key=lambda item: list(item.path))
        self.assertEqual([], [item.message for item in errors])

    def assert_invalid(self, payload: object) -> None:
        self.assertTrue(list(self.validator.iter_errors(payload)))

    def source_envelope(self) -> dict[str, object]:
        generation = self.examples["completed_generation"]
        return {
            "campaign_id": generation["campaign_id"],
            "revision_id": generation["source_revision"],
            "retrieval_policy_version": 1,
            "session_id": None,
            "source_set_digest": generation["source_set_digest"],
            "sources": [{
                "source_id": item["source_id"],
                "authority": item["authority"],
                "order": item["order"],
                "text": item["excerpt"],
                "digest": item["excerpt_digest"],
            } for item in generation["sources"]],
        }

    def proposal_context(self, proposal: dict[str, object]) -> dict[str, object]:
        return {
            "stored_proposal": deepcopy(proposal),
            "generation": self.examples["completed_generation"],
            "record": self.examples["record"],
            "path_params": {
                "proposal_id": proposal["proposal_id"],
                "proposal_version": proposal["proposal_version"],
            },
        }

    def test_schema_is_separate_closed_and_all_examples_validate(self) -> None:
        index = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))
        transport_index = json.loads((ROOT.parents[1] / "index-v1.json").read_text(encoding="utf-8"))
        self.assertFalse(index["mutates_transport_contracts"])
        self.assertNotIn("http", {item["family"] for item in transport_index["families"]})
        self.assertTrue(all(definition.get("additionalProperties") is False or "oneOf" in definition
                            for definition in self.schema["$defs"].values()
                            if definition.get("type") == "object" or "oneOf" in definition))
        for name, payload in self.examples.items():
            with self.subTest(name=name):
                self.assert_valid(payload)
        invariant_index = json.loads((ROOT / "semantic-invariants.json").read_text(encoding="utf-8"))
        self.assertEqual({item["id"] for item in invariant_index["rules"]}, set(IMPLEMENTED_INVARIANTS))
        declared_pairs = {(item["id"], name) for item in invariant_index["rules"] for name in item["applies_to"]}
        self.assertEqual(declared_pairs, set(INVARIANT_APPLIES_TO))

    def test_route_operation_envelopes_reject_irrelevant_binding_fields(self) -> None:
        consent = deepcopy(self.examples["provider_consent"])
        consent["operation_request"]["subject_id"] = "proposal_alpha"
        self.assert_invalid(consent)

        campaign = deepcopy(self.examples["campaign_create"])
        campaign["operation_request"]["intent_digest"] = "a" * 64
        self.assert_invalid(campaign)

        correction = deepcopy(self.examples["proposal_correction"])
        correction["operation_request"]["intent_digest"] = "b" * 64
        self.assert_invalid(correction)

        rejection = deepcopy(self.examples["proposal_rejection"])
        rejection["operation_request"]["intent_digest"] = "c" * 64
        self.assert_invalid(rejection)

    def test_operation_and_display_digests_match_exact_canonical_inputs(self) -> None:
        for name in ("provider_consent", "campaign_create", "proposal_create", "proposal_correction", "proposal_rejection", "proposal_approval"):
            payload = self.examples[name]
            operation = payload.get("operation_request", payload)
            self.assertEqual(operation["payload_digest"], canonical_digest(request_payload(payload)))

        generation = self.examples["completed_generation"]
        source = generation["sources"][0]
        self.assertEqual(source["excerpt_digest"], text_digest(source["excerpt"]))
        proposal = self.examples["proposal"]
        change = proposal["exact_diff"][0]
        self.assertEqual(change["before_digest"], text_digest(change["before_content"]))
        self.assertEqual(change["after_digest"], text_digest(change["after_content"]))
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
        self.assertEqual(proposal["diff_digest"], canonical_digest(diff_value))
        self.assertEqual(proposal["proposal_payload_digest"], canonical_digest(proposal_value))

    def test_proposal_provenance_transform_and_approval_bindings_are_exact(self) -> None:
        create = self.examples["proposal_create"]
        proposal = self.examples["proposal"]
        generation = self.examples["completed_generation"]
        self.assertEqual(create["generation_id"], proposal["generation_id"])
        self.assertEqual(create["source_set_digest"], proposal["source_set_digest"])
        self.assertEqual(create["source_set_digest"], generation["source_set_digest"])
        self.assertEqual(create["terminal_draft_digest"], proposal["terminal_draft_digest"])
        before = proposal["exact_diff"][0]["before_content"]
        draft = generation["terminal_content"]
        expected = append_draft(before, draft)
        self.assertEqual(expected, proposal["exact_diff"][0]["after_content"])
        self.assertEqual(generation["terminal_content_digest"], create["terminal_draft_digest"])

        approval = self.examples["proposal_approval"]
        operation = approval["operation_request"]
        self.assertEqual(operation["subject_id"], approval["proposal_id"])
        self.assertEqual(operation["intent_digest"], approval["diff_digest"])
        self.assertEqual(operation["expected_revision"], approval["base_revision"])
        self.assertEqual(approval["expected_campaign_head"], approval["base_revision"])
        self.assertEqual(approval["diff_digest"], proposal["diff_digest"])
        self.assertEqual(approval["proposal_payload_digest"], proposal["proposal_payload_digest"])

    def test_consent_correction_and_terminal_results_preserve_bindings(self) -> None:
        readiness = self.examples["provider_readiness"]
        consent = self.examples["provider_consent"]
        self.assertEqual(readiness["consent_identity_digest"], consent["input"]["consent_identity_digest"])
        correction = self.examples["proposal_correction"]
        proposal = self.examples["proposal"]
        change = proposal["exact_diff"][0]
        self.assertEqual((correction["change_id"], correction["subject_id"]),
                         (change["change_id"], change["subject_id"]))
        conflict = self.examples["stale_approval_conflict"]
        self.assertEqual("conflict", conflict["proposal"]["status"])
        self.assertIsNone(conflict["published_revision"])
        published = self.examples["published_approval"]
        self.assertEqual(published["proposal"]["published_revision_id"],
                         published["published_revision"]["revision_id"])
        self.assertEqual("passed", published["published_revision"]["validation_status"])

    def test_semantic_validator_rejects_every_cross_object_binding_mismatch(self) -> None:
        generation = self.examples["completed_generation"]
        record = self.examples["record"]
        proposal = self.examples["proposal"]
        validate_http_semantics(self.examples["provider_consent"], context={
            "consent_identity_digest": self.examples["provider_readiness"]["consent_identity_digest"]})
        validate_http_semantics(self.examples["provider_readiness"])
        validate_http_semantics(generation, context={"source_envelope": self.source_envelope()})
        validate_http_semantics(proposal, context=self.proposal_context(proposal))
        validate_http_semantics(self.examples["proposal_create"], context={
            "generation": generation, "record": record, "proposal": proposal,
            "path_params": {"generation_id": "generation_alpha"}})
        validate_http_semantics(self.examples["proposal_correction"], context={
            "proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}})
        validate_http_semantics(self.examples["proposal_rejection"], context={
            "proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}})
        validate_http_semantics(self.examples["proposal_approval"], context={
            "proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}})
        conflict = self.examples["stale_approval_conflict"]
        published = self.examples["published_approval"]
        validate_http_semantics(conflict, context=self.proposal_context(conflict["proposal"]))
        validate_http_semantics(published, context=self.proposal_context(published["proposal"]))

        cases = []
        inconsistent = deepcopy(self.examples["proposal_approval"])
        inconsistent["operation_request"]["expected_revision"] = "revision_other"
        cases.append((inconsistent, {"proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}}, "proposal_approval_conflict"))
        wrong_correction = deepcopy(self.examples["proposal_correction"])
        wrong_correction["operation_request"]["subject_id"] = "proposal_other"
        cases.append((wrong_correction, {"proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}}, "proposal_approval_conflict"))
        wrong_rejection = deepcopy(self.examples["proposal_rejection"])
        wrong_rejection["operation_request"]["expected_revision"] = "revision_other"
        cases.append((wrong_rejection, {"proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}}, "proposal_approval_conflict"))
        wrong_consent = deepcopy(self.examples["provider_consent"])
        cases.append((wrong_consent, {"consent_identity_digest": "8" * 64}, "capability_rejected"))
        changed_correction = deepcopy(self.examples["proposal_correction"])
        changed_correction["after_content"] += "changed"
        cases.append((changed_correction, {"proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}}, "idempotency_digest_conflict"))
        for payload, context, category in cases:
            with self.subTest(name=payload["contract_name"], category=category), self.assertRaises(HTTPContractSemanticError) as raised:
                validate_http_semantics(payload, context=context)
            self.assertEqual(category, raised.exception.finding.category)

    def test_authority_context_is_required_and_provenance_mismatches_fail(self) -> None:
        with self.assertRaises(HTTPContractSemanticError) as missing_consent:
            validate_http_semantics(self.examples["provider_consent"])
        self.assertEqual("capability_rejected", missing_consent.exception.finding.category)

        generation = self.examples["completed_generation"]
        bad_envelope = self.source_envelope()
        bad_envelope["source_set_digest"] = "0" * 64
        with self.assertRaises(HTTPContractSemanticError) as bad_source:
            validate_http_semantics(generation, context={"source_envelope": bad_envelope})
        self.assertEqual("source_digest_conflict", bad_source.exception.finding.category)

        create = deepcopy(self.examples["proposal_create"])
        create["base_revision"] = "revision_other"
        create["payload_digest"] = canonical_digest(request_payload(create))
        context = {
            "generation": generation,
            "record": self.examples["record"],
            "proposal": self.examples["proposal"],
            "path_params": {"generation_id": "generation_alpha"},
        }
        with self.assertRaises(HTTPContractSemanticError) as silent_rebase:
            validate_http_semantics(create, context=context)
        self.assertEqual("source_digest_conflict", silent_rebase.exception.finding.category)

        wrong_proposal = deepcopy(self.examples["proposal"])
        wrong_proposal["campaign_id"] = "campaign_other"
        context["proposal"] = wrong_proposal
        with self.assertRaises(HTTPContractSemanticError) as wrong_campaign:
            validate_http_semantics(self.examples["proposal_create"], context=context)
        self.assertEqual("proposal_validation_failure", wrong_campaign.exception.finding.category)

        bad_readiness = deepcopy(self.examples["provider_readiness"])
        bad_readiness["consent_current"] = False
        with self.assertRaises(HTTPContractSemanticError) as readiness:
            validate_http_semantics(bad_readiness)
        self.assertEqual("capability_rejected", readiness.exception.finding.category)

        hidden_session = self.source_envelope()
        hidden_session["session_id"] = "session_hidden"
        digest_value = {
            "campaign_id": hidden_session["campaign_id"],
            "revision_id": hidden_session["revision_id"],
            "retrieval_policy_version": hidden_session["retrieval_policy_version"],
            "session_id": hidden_session["session_id"],
            "sources": [{"authority": item["authority"], "digest": item["digest"], "order": item["order"], "source_id": item["source_id"]} for item in hidden_session["sources"]],
        }
        hidden_session["source_set_digest"] = canonical_digest(digest_value)
        hidden_generation = deepcopy(generation)
        hidden_generation["source_set_digest"] = hidden_session["source_set_digest"]
        with self.assertRaises(HTTPContractSemanticError) as session_source:
            validate_http_semantics(hidden_generation, context={"source_envelope": hidden_session})
        self.assertEqual("source_digest_conflict", session_source.exception.finding.category)

        wrong_view = deepcopy(self.examples["proposal"])
        wrong_view["campaign_id"] = "campaign_other"
        with self.assertRaises(HTTPContractSemanticError) as view_binding:
            validate_http_semantics(wrong_view, context=self.proposal_context(self.examples["proposal"]))
        self.assertEqual("source_digest_conflict", view_binding.exception.finding.category)

        wrong_record = deepcopy(self.examples["record"])
        wrong_record["campaign_id"] = "campaign_other"
        wrong_record["revision_id"] = "revision_other"
        wrong_context = self.proposal_context(self.examples["proposal"])
        wrong_context["record"] = wrong_record
        with self.assertRaises(HTTPContractSemanticError) as record_binding:
            validate_http_semantics(self.examples["proposal"], context=wrong_context)
        self.assertEqual("http_single_record_append", record_binding.exception.finding.rule_id)

        wrong_type = deepcopy(self.examples["proposal"])
        wrong_type["exact_diff"][0]["record_type"] = "npc"
        changed = wrong_type["exact_diff"][0]
        wrong_type["diff_digest"] = canonical_digest({
            "change_id": changed["change_id"], "change_kind": changed["change_type"],
            "expected_content_digest": changed["before_digest"], "record_type": changed["record_type"],
            "replacement_digest": changed["after_digest"], "subject_id": changed["subject_id"]})
        wrong_type["proposal_payload_digest"] = canonical_digest({
            "id": changed["change_id"], "subject": changed["subject_id"], "replacement": changed["after_content"],
            "expected": changed["before_digest"], "kind": changed["change_type"], "record_type": changed["record_type"]})
        with self.assertRaises(HTTPContractSemanticError) as create_type_binding:
            validate_http_semantics(self.examples["proposal_create"], context={
                "generation": self.examples["completed_generation"], "record": self.examples["record"],
                "proposal": wrong_type, "path_params": {"generation_id": "generation_alpha"}})
        self.assertEqual("http_single_record_append", create_type_binding.exception.finding.rule_id)
        wrong_type_context = self.proposal_context(wrong_type)
        with self.assertRaises(HTTPContractSemanticError) as view_type_binding:
            validate_http_semantics(wrong_type, context=wrong_type_context)
        self.assertEqual("http_single_record_append", view_type_binding.exception.finding.rule_id)

    def test_each_mandatory_invariant_has_a_failing_behavior_vector(self) -> None:
        proposal = self.examples["proposal"]
        generation = self.examples["completed_generation"]
        record = self.examples["record"]
        vectors: list[tuple[str, dict[str, object], dict[str, object]]] = []

        changed_create = deepcopy(self.examples["campaign_create"])
        changed_create["input"]["campaign_name"] = "Changed"
        vectors.append(("http_operation_digest", changed_create, {}))

        bad_content = deepcopy(generation)
        bad_content["terminal_content_digest"] = "0" * 64
        vectors.append(("http_content_digests", bad_content, {"source_envelope": self.source_envelope()}))

        bad_source = deepcopy(generation)
        bad_source["source_set_digest"] = "0" * 64
        vectors.append(("http_source_set_binding", bad_source, {"source_envelope": self.source_envelope()}))

        bad_proposal_digest = deepcopy(proposal)
        bad_proposal_digest["diff_digest"] = "0" * 64
        vectors.append(("http_proposal_digests", bad_proposal_digest, self.proposal_context(proposal)))

        vectors.append(("http_consent_identity", self.examples["provider_consent"], {"consent_identity_digest": "0" * 64}))

        bad_provenance = deepcopy(self.examples["proposal_create"])
        bad_provenance["campaign_id"] = "campaign_other"
        bad_provenance["payload_digest"] = canonical_digest(request_payload(bad_provenance))
        vectors.append(("http_proposal_provenance", bad_provenance, {
            "generation": generation, "record": record, "proposal": proposal,
            "path_params": {"generation_id": "generation_alpha"}}))

        changed_view = deepcopy(proposal)
        changed_view["exact_diff"][0]["after_content"] += " changed"
        changed_view["exact_diff"][0]["after_digest"] = text_digest(changed_view["exact_diff"][0]["after_content"])
        change = changed_view["exact_diff"][0]
        changed_view["diff_digest"] = canonical_digest({
            "change_id": change["change_id"], "change_kind": change["change_type"],
            "expected_content_digest": change["before_digest"], "record_type": change["record_type"],
            "replacement_digest": change["after_digest"], "subject_id": change["subject_id"]})
        changed_view["proposal_payload_digest"] = canonical_digest({
            "id": change["change_id"], "subject": change["subject_id"], "replacement": change["after_content"],
            "expected": change["before_digest"], "kind": change["change_type"], "record_type": change["record_type"]})
        vectors.append(("http_single_record_append", self.examples["proposal_create"], {
            "generation": generation, "record": record, "proposal": changed_view,
            "path_params": {"generation_id": "generation_alpha"}}))

        bad_correction = deepcopy(self.examples["proposal_correction"])
        bad_correction["change_id"] = "change_other"
        bad_correction["operation_request"]["payload_digest"] = canonical_digest(request_payload(bad_correction))
        vectors.append(("http_correction_binding", bad_correction, {
            "proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}}))

        bad_approval = deepcopy(self.examples["proposal_approval"])
        bad_approval["operation_request"]["expected_revision"] = "revision_other"
        vectors.append(("http_approval_equality", bad_approval, {
            "proposal": proposal, "path_params": {"proposal_id": "proposal_alpha", "proposal_version": 1}}))

        bad_result = deepcopy(self.examples["published_approval"])
        bad_result["published_revision"]["revision_id"] = "revision_other"
        vectors.append(("http_approval_result", bad_result, self.proposal_context(bad_result["proposal"])))

        vectors.append(("http_path_body_equality", self.examples["ask_start"], {
            "path_params": {"campaign_id": "campaign_other", "source_revision": "revision_alpha"}}))
        vectors.append(("http_sse_resume", self.examples["generation_event"], {
            "after": 3, "last_event_id": 3, "last_sequence": 2}))
        unsafe_error = {"contract_name":"error_response","contract_version":1,"error":{
            "category":"provider_terminal_failure","code":"provider_failed","stage":"ask","request_id":"request_error",
            "retryable":False,"message":"sk-synthetic raw provider prompt text"}}
        vectors.append(("http_safe_error", unsafe_error, {}))

        self.assertEqual(set(IMPLEMENTED_INVARIANTS), {item[0] for item in vectors})
        for rule_id, payload, context in vectors:
            with self.subTest(rule_id=rule_id), self.assertRaises(HTTPContractSemanticError) as raised:
                validate_http_semantics(payload, context=context)
            self.assertEqual(rule_id, raised.exception.finding.rule_id)

    def test_append_transform_preserves_normalized_boundary_newlines_exactly(self) -> None:
        before = "---\r\nid: campaign-main\r\n---\r\n\r\n"
        draft = "\r\nFirst line.\r\nSecond line.\r\n"
        expected = "---\nid: campaign-main\n---\n\n\n\n## Proposed addition\n\n\nFirst line.\nSecond line.\n"
        self.assertEqual(expected, append_draft(before, draft))

    def test_adversarial_contract_shapes_fail_closed(self) -> None:
        contradictory = deepcopy(self.examples["published_approval"])
        contradictory["proposal"]["status"] = "conflict"
        contradictory["published_revision"] = None
        self.assert_invalid(contradictory)

        null_before = deepcopy(self.examples["proposal"])
        null_before["exact_diff"][0]["before_content"] = None
        self.assert_invalid(null_before)

        private_path = {"contract_name":"error_response","contract_version":1,"error":{
            "category":"service_unavailable","code":"failed","stage":"read","request_id":"request_error",
            "message":"read failed at /etc/passwd","retryable":False}}
        self.assert_invalid(private_path)

        table_fact = deepcopy(self.examples["campaign_revision"])
        table_fact["records"][0]["authority"] = "table_fact"
        self.assert_invalid(table_fact)

        multiple = deepcopy(self.examples["proposal"])
        multiple["exact_diff"].append(deepcopy(multiple["exact_diff"][0]))
        self.assert_invalid(multiple)

if __name__ == "__main__":
    unittest.main()
