from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from warden_drydock.hosted.ai.models import (
    Action, GenerationRecord, GenerationRequest, SourceEnvelope, SourceExcerpt,
    StreamEvent,
)
from warden_drydock.hosted.engine.models import ExactTextChange
from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.proposals.service import ProposalStatus, ProposalVersion, _diff_digest, _payload_digest


class AtlasWorkflowCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = SliceApplication(Path(self.temporary.name))
        self.viewed = self.app.create_campaign(self._bind({
            "contract_name": "campaign_create_request", "contract_version": 2,
            "operation_request": {
                "contract_name": "operation_request", "contract_version": 2,
                "request_id": "request_create", "operation": "campaign_create",
                "idempotency_key": "idem_create", "payload_digest": "0" * 64,
                "expected_revision": None, "expected_workflow_version": None,
            },
            "input": {"campaign_id": "campaign_workflow", "campaign_name": "Workflow", "adapter_id": "mothership"},
        }))[1]["viewed_revision"]
        detail = self.app.atlas_record_detail(
            "campaign_workflow", "campaign-main", self.viewed["revision_id"],
            self.viewed["ordinal"], self.viewed["tree_digest"],
        )[1]
        self.record_digest = detail["record"]["content_digest"]

    @staticmethod
    def _bind(payload: dict) -> dict:
        payload["operation_request"]["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def _generation(self, generation_id: str, action: Action, *, record: bool, status: str = "complete") -> GenerationRecord:
        envelope = SourceEnvelope(
            "campaign_workflow", self.viewed["revision_id"],
            (SourceExcerpt("campaign-main", "preparation", "safe", 1),),
        )
        request = GenerationRequest(
            generation_id, "campaign_workflow", self.viewed["revision_id"], action,
            "not exposed", envelope,
            "campaign-main" if record else None,
            self.record_digest if record else None,
        )
        item = GenerationRecord(request)
        self.app.ai_repository.reserve_generation(item)
        if status != "pending":
            item.terminal_status = status
            item.events.append(StreamEvent(1, "completion" if status == "complete" else "failure", retryable=status == "failed"))
            self.app.ai_repository.save_generation(item)
        return item

    def _query(self, method, **overrides):
        values = {
            "campaign_id": "campaign_workflow", "revision_id": self.viewed["revision_id"],
            "ordinal": self.viewed["ordinal"], "tree_digest": self.viewed["tree_digest"],
            "actions": (), "statuses": (), "record_id": None, "limit": 50, "cursor": None,
        }
        values.update(overrides)
        if method == self.app.atlas_proposal_collection:
            values.pop("actions")
        return method(**values)

    def test_generation_collection_is_summary_only_filtered_and_forward_paginated(self) -> None:
        self._generation("generation_one", Action.ASK, record=False)
        self._generation("generation_two", Action.CHECK, record=True, status="failed")
        payload = self._query(self.app.atlas_generation_collection)[1]
        self.assertEqual((2, ["generation_two", "generation_one"]), (
            payload["contract_version"], [item["generation_id"] for item in payload["items"]],
        ))
        self.assertNotIn("prompt", str(payload))
        filtered = self._query(
            self.app.atlas_generation_collection, actions=("check",), statuses=("failed",),
            record_id="campaign-main",
        )[1]
        self.assertEqual(["generation_two"], [item["generation_id"] for item in filtered["items"]])
        self.assertTrue(filtered["items"][0]["retryable"])
        first = self._query(self.app.atlas_generation_collection, limit=1)[1]
        second = self._query(self.app.atlas_generation_collection, limit=1, cursor=first["next_cursor"])[1]
        self.assertEqual("generation_one", second["items"][0]["generation_id"])
        with self.assertRaises(HTTPFailure) as caught:
            self._query(self.app.atlas_generation_collection, limit=2, cursor=first["next_cursor"])
        self.assertEqual((422, "invalid_cursor_binding"), (caught.exception.status, caught.exception.payload["error"]["code"]))

    def test_proposal_collection_binds_generation_subject_and_immutable_version(self) -> None:
        generation = self._generation("generation_proposal", Action.GENERATE, record=True)
        change = ExactTextChange("change_one", "campaign-main", self.record_digest, "replacement")
        proposal = ProposalVersion(
            "proposal_one", 1, "campaign_workflow", self.viewed["revision_id"],
            (change,), _diff_digest((change,)), _payload_digest((change,)),
            generation_id=generation.request.generation_id,
            source_revision=self.viewed["revision_id"],
            source_set_digest=generation.request.envelope.source_set_digest,
            terminal_draft_digest="a" * 64,
        )
        self.app.proposal_repository.add(proposal)
        payload = self._query(self.app.atlas_proposal_collection)[1]
        item = payload["items"][0]
        self.assertEqual(("generate", "campaign-main", self.record_digest, 1), (
            item["action"], item["subject_record_id"], item["subject_content_digest"],
            item["proposal_version"],
        ))
        self.assertNotIn("replacement", str(payload))
        self.assertNotIn("terminal_draft_digest", str(payload))

    def test_reads_do_not_require_provider_or_consent_and_bad_provenance_fails_closed(self) -> None:
        generation = self._generation("generation_bad", Action.GENERATE, record=False)
        change = ExactTextChange("change_bad", "campaign-main", self.record_digest, "replacement")
        proposal = ProposalVersion(
            "proposal_bad", 1, "campaign_workflow", self.viewed["revision_id"],
            (change,), _diff_digest((change,)), _payload_digest((change,)),
            generation_id=generation.request.generation_id,
            source_revision=self.viewed["revision_id"], source_set_digest="f" * 64,
            terminal_draft_digest="a" * 64,
        )
        self.app.proposal_repository.add(proposal)
        self.assertEqual(200, self._query(self.app.atlas_generation_collection)[0])
        with self.assertRaises(HTTPFailure) as caught:
            self._query(self.app.atlas_proposal_collection)
        self.assertEqual((409, "source_digest_conflict"), (
            caught.exception.status, caught.exception.payload["error"]["category"],
        ))

    def test_corrupt_generation_repository_binding_is_sanitized_as_source_conflict(self) -> None:
        def corrupt_rows(*_args):
            raise ValueError("unsafe_binding")

        self.app.ai_repository.generation_rows = corrupt_rows
        with self.assertRaises(HTTPFailure) as caught:
            self._query(self.app.atlas_generation_collection)
        self.assertEqual((409, "source_digest_conflict", "generation_provenance_mismatch"), (
            caught.exception.status,
            caught.exception.payload["error"]["category"],
            caught.exception.payload["error"]["code"],
        ))
        self.assertNotIn("unsafe_binding", str(caught.exception.payload))
        self.assertNotIn("prompt", str(caught.exception.payload))

    def test_proposal_collection_does_not_reconcile_or_mutate_workflow_state(self) -> None:
        generation = self._generation("generation_read_only", Action.GENERATE, record=True)
        for suffix, status in (("approved", ProposalStatus.APPROVED), ("quarantined", ProposalStatus.QUARANTINED)):
            change = ExactTextChange(f"change_{suffix}", "campaign-main", self.record_digest, "replacement")
            proposal = ProposalVersion(
                f"proposal_{suffix}", 1, "campaign_workflow", self.viewed["revision_id"],
                (change,), _diff_digest((change,)), _payload_digest((change,)), status,
                generation_id=generation.request.generation_id,
                source_revision=self.viewed["revision_id"],
                source_set_digest=generation.request.envelope.source_set_digest,
                terminal_draft_digest="a" * 64,
            )
            self.app.proposal_repository.add(proposal)
        before_items = dict(self.app.proposal_repository.items)
        before_audit = tuple(self.app.proposal_repository.audit)
        payload = self._query(self.app.atlas_proposal_collection)[1]
        self.assertEqual(
            {"proposal_approved": "draft", "proposal_quarantined": "quarantined"},
            {item["proposal_id"]: item["status"] for item in payload["items"]},
        )
        self.assertEqual(before_items, self.app.proposal_repository.items)
        self.assertEqual(before_audit, tuple(self.app.proposal_repository.audit))
        self.assertTrue(all(item.published_revision_id is None for item in self.app.proposal_repository.items.values()))


if __name__ == "__main__":
    unittest.main()
