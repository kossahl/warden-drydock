from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.editor import document_digest, mutate_document, parse_document
from warden_drydock.hosted.http.editor_semantics import validate_editor_semantics
from warden_drydock.hosted.http.repository import InMemoryHTTPRepository
from warden_drydock.hosted.proposals.service import ProposalStatus
from warden_drydock.hosted.engine.models import ChangeKind, ExactTextChange, exact_diff_digest, content_digest
from warden_drydock.hosted.revisions import InMemoryWorkflowRepository


class EditorBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipts = InMemoryHTTPRepository()
        self.workflow = InMemoryWorkflowRepository()
        self.app = SliceApplication(Path(self.tmp.name), provider=SyntheticProvider(),
                                    receipts=self.receipts, workflow_repository=self.workflow)
        self._campaign()

    def _campaign(self):
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": {"contract_name": "operation_request", "contract_version": 2,
                       "request_id": "request_campaign", "operation": "campaign_create",
                       "idempotency_key": "idem_campaign", "payload_digest": "0" * 64,
                       "expected_revision": None, "expected_workflow_version": None},
                   "input": {"campaign_id": "campaign_alpha", "campaign_name": "Editor", "adapter_id": "mothership"}}
        payload["operation_request"]["payload_digest"] = canonical_digest(request_digest_input(payload))
        self.app.create_campaign(payload)

    def _edit(self, key="idem_edit"):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["displayed_name"] = "Edited Campaign"
        candidate["content_digest"] = document_digest(candidate)
        operation = {"contract_name": "editor_operation_request", "contract_version": 1,
                     "request_id": "request_edit", "operation": "editor_record_edit",
                     "idempotency_key": key, "payload_digest": "0" * 64,
                     "expected_revision": revision, "expected_editor_workflow_version": 1,
                     "subject_id": "campaign-main"}
        payload = {"contract_name": "editor_record_edit_request", "contract_version": 1,
                   "operation_request": operation,
                   "binding": {"campaign_id": "campaign_alpha", "base_revision": view["viewed_revision"],
                               "record_id": "campaign-main", "record_digest": view["record"]["content_digest"],
                               "expected_editor_workflow_version": 1},
                   "candidate": candidate}
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        return revision, payload, self.app.editor_record_edit("campaign_alpha", revision, "campaign-main", payload)

    def _editor_approval_payload(self, proposal):
        action = {
            "contract_name": "editor_proposal_approval_request", "contract_version": 1,
            "proposal": {"proposal_id": proposal["proposal_id"], "proposal_version": proposal["proposal_version"]},
            "proposal_status": "needs_review", "mutation_kind": proposal["mutation_kind"],
            "source_revision": proposal["source_revision"], "base_revision": proposal["base_revision"],
            "expected_campaign_head": proposal["expected_campaign_head"],
            "expected_editor_workflow_version": proposal["editor_workflow_version"],
            "proposal_payload_digest": proposal["proposal_payload_digest"],
            "diff_digest": proposal["diff"]["diff_digest"], "diff": proposal["diff"],
            "record_bindings": proposal["record_bindings"], "impact_digest": proposal["impact_digest"],
            "impact_binding": proposal["impact_binding"], "resolutions": proposal["resolutions"],
            "validation_status": proposal["validation"]["status"],
            "validation_digest": proposal["validation"]["validation_digest"],
            "affected_record_count": proposal["diff"]["affected_record_count"],
            "confirmed_change_ids": [card["change_id"] for card in proposal["diff"]["cards"]],
            "confirmed_authority_change_ids": [item["change_id"] for item in proposal["diff"]["authority_changes"]],
            "confirmed_visibility_change_ids": [item["change_id"] for item in proposal["diff"]["visibility_changes"]],
            "authority_outcome": proposal["authority_outcome"], "visibility_outcome": proposal["visibility_outcome"],
            "warden_confirmed": True,
        }
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": f"request_approve_{proposal['proposal_id']}",
            "operation": "editor_proposal_approve",
            "idempotency_key": f"idem_approve_{proposal['proposal_id']}",
            "expected_revision": proposal["base_revision"]["revision_id"],
            "expected_editor_workflow_version": proposal["editor_workflow_version"],
            "subject_id": proposal["proposal_id"], "intent_digest": proposal["diff"]["diff_digest"],
            "payload_digest": "0" * 64,
        }
        action["operation_request"] = operation
        operation["payload_digest"] = canonical_digest(request_digest_input(action))
        return action

    def _approve_editor(self, proposal):
        return self.app.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"],
            self._editor_approval_payload(proposal),
        )

    def _create_record(self, record_id, *, record_type="npc", connections=None):
        revision = self.app.workflow.head("campaign_alpha")
        workflow = self.app._editor_version("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = {
            "record_id": record_id, "record_type": record_type, "displayed_name": record_id,
            "status": "draft", "authority": "preparation",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [{"field_id": "ownership", "value": "campaign"}],
            "sections": [{"section_id": "summary", "body": "Synthetic record."}],
            "connections": connections or [], "content_digest": "0" * 64,
        }
        candidate["content_digest"] = document_digest(candidate)
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": f"request_create_{record_id}", "operation": "editor_record_create",
            "idempotency_key": f"idem_create_{record_id}", "payload_digest": "0" * 64,
            "expected_revision": revision, "expected_editor_workflow_version": workflow,
            "subject_id": record_id,
        }
        payload = {
            "contract_name": "editor_record_create_request", "contract_version": 1,
            "operation_request": operation,
            "binding": {"campaign_id": "campaign_alpha", "base_revision": view["viewed_revision"],
                        "record_id": record_id, "record_digest": None,
                        "expected_editor_workflow_version": workflow},
            "candidate": candidate,
        }
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        _, proposal = self.app.editor_record_create("campaign_alpha", revision, payload)
        self._approve_editor(proposal)
        return self.app.workflow.head("campaign_alpha")

    def test_edit_is_exact_and_replay_does_not_advance_workflow(self):
        revision, payload, (status, proposal) = self._edit()
        self.assertEqual((201, 2), (status, proposal["editor_workflow_version"]))
        replay_status, replay = self.app.editor_record_edit("campaign_alpha", revision, "campaign-main", deepcopy(payload))
        self.assertEqual((200, proposal), (replay_status, replay))
        self.assertEqual(2, self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]["editor_workflow_version"])
        self.assertEqual("edit", proposal["diff"]["summary"])

    def test_stale_record_digest_fails_before_mutation(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        payload = {"contract_name": "editor_record_edit_request", "contract_version": 1,
            "operation_request": {"contract_name": "editor_operation_request", "contract_version": 1,
                "request_id": "request_stale", "operation": "editor_record_edit", "idempotency_key": "idem_stale",
                "payload_digest": "0" * 64, "expected_revision": revision,
                "expected_editor_workflow_version": 1, "subject_id": "campaign-main"}, "binding": {
            "campaign_id": "campaign_alpha", "base_revision": view["viewed_revision"],
            "record_id": "campaign-main", "record_digest": "0" * 64,
            "expected_editor_workflow_version": 1}, "candidate": view["record"]}
        payload["operation_request"]["payload_digest"] = canonical_digest(request_digest_input(payload))
        with self.assertRaises(HTTPFailure) as error:
            self.app.editor_record_edit("campaign_alpha", revision, "campaign-main", payload)
        self.assertEqual((409, "stale_record_digest"), (error.exception.status, error.exception.payload["error"]["code"]))

    def test_approval_uses_immutable_revision_boundary_and_restart_readback(self):
        _, _, (_, proposal) = self._edit()
        action = {"contract_name": "editor_proposal_approval_request", "contract_version": 1,
            "proposal": {"proposal_id": proposal["proposal_id"], "proposal_version": 1},
            "proposal_status": "needs_review", "mutation_kind": "edit",
            "source_revision": proposal["source_revision"], "base_revision": proposal["base_revision"],
            "expected_campaign_head": proposal["expected_campaign_head"],
            "expected_editor_workflow_version": proposal["editor_workflow_version"],
            "proposal_payload_digest": proposal["proposal_payload_digest"], "diff_digest": proposal["diff"]["diff_digest"],
            "diff": proposal["diff"], "record_bindings": proposal["record_bindings"],
            "impact_digest": None, "impact_binding": None, "resolutions": [],
            "validation_status": "passed", "validation_digest": proposal["validation"]["validation_digest"],
            "affected_record_count": proposal["diff"]["affected_record_count"],
            "confirmed_change_ids": [card["change_id"] for card in proposal["diff"]["cards"]],
            "confirmed_authority_change_ids": [], "confirmed_visibility_change_ids": [],
            "authority_outcome": [], "visibility_outcome": [], "warden_confirmed": True}
        action["operation_request"] = {"contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_approve", "operation": "editor_proposal_approve", "idempotency_key": "idem_approve",
            "expected_revision": proposal["base_revision"]["revision_id"], "expected_editor_workflow_version": 2,
            "subject_id": proposal["proposal_id"], "intent_digest": proposal["diff"]["diff_digest"], "payload_digest": "0" * 64}
        action["operation_request"]["payload_digest"] = canonical_digest(request_digest_input(action))
        result = self.app.editor_proposal_approve(proposal["proposal_id"], 1, action)[1]
        self.assertEqual("published", result["outcome"])
        restarted = SliceApplication(Path(self.tmp.name), provider=SyntheticProvider(),
                                     receipts=self.receipts, workflow_repository=self.workflow,
                                     proposal_repository=self.app.proposal_repository)
        self.assertEqual("published", restarted.editor_proposal_read(proposal["proposal_id"], 1)[1]["publication"]["status"])
        self.assertEqual(ProposalStatus.PUBLISHED, self.app.proposal_repository.get(proposal["proposal_id"], 1).status)

    def test_editor_approval_replays_after_publication_before_receipt_storage(self):
        _, _, (_, proposal) = self._edit("idem_editor_receipt_crash")
        with mock.patch.object(self.app, "_store", side_effect=SystemExit("receipt crash")):
            with self.assertRaises(SystemExit):
                self._approve_editor(proposal)

        restarted = SliceApplication(
            Path(self.tmp.name), provider=SyntheticProvider(), receipts=self.receipts,
            workflow_repository=self.workflow, proposal_repository=self.app.proposal_repository,
            atlas_repository=self.app.atlas_repository,
        )
        status, replay = restarted.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"],
            self._editor_approval_payload(proposal),
        )
        self.assertEqual((200, "published"), (status, replay["outcome"]))
        published = replay["published_revision"]["revision_id"]
        projection = restarted.atlas_repository.get("campaign_alpha", published)
        self.assertEqual((proposal["proposal_id"], proposal["proposal_version"]), (
            projection.history_entry.proposal_id,
            projection.history_entry.proposal_version,
        ))
        status, exact_replay = restarted.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"],
            self._editor_approval_payload(proposal),
        )
        self.assertEqual((200, replay), (status, exact_replay))

    def test_restart_recovers_editor_publication_and_exact_replay_once(self):
        _, _, (_, proposal) = self._edit("idem_editor_pending_recovery")
        approval = self._editor_approval_payload(proposal)
        with mock.patch.object(self.app.workflow, "finalize_head", side_effect=SystemExit("publication crash")):
            with self.assertRaises(SystemExit):
                self.app.editor_proposal_approve(
                    proposal["proposal_id"], proposal["proposal_version"], approval,
                )

        restarted = SliceApplication(
            Path(self.tmp.name), provider=SyntheticProvider(), receipts=self.receipts,
            workflow_repository=self.workflow, proposal_repository=self.app.proposal_repository,
            atlas_repository=self.app.atlas_repository,
        )
        head = restarted.workflow.head("campaign_alpha")
        self.assertNotEqual(proposal["base_revision"]["revision_id"], head)
        self.assertEqual(2, len(restarted.revisions.store.inventory()))
        self.assertFalse(any(restarted.revisions.store.quarantine.rglob("snapshot-manifest-v1.json")))
        recovered = restarted.proposal_repository.get(proposal["proposal_id"], proposal["proposal_version"])
        self.assertEqual(ProposalStatus.PUBLISHED, recovered.status)
        self.assertEqual(head, recovered.published_revision_id)
        self.assertEqual(3, restarted._editor_version("campaign_alpha"))
        self.assertEqual(
            1,
            sum(
                1 for item in restarted.proposal_repository.audit
                if item == (proposal["proposal_id"], proposal["proposal_version"], "published")
            ),
        )
        self.assertEqual(
            1,
            sum(
                1 for item in restarted.workflow.audit
                if item == (
                    restarted._id("intent", proposal["proposal_id"], proposal["proposal_version"]),
                    "finalized",
                )
            ),
        )

        status, retry = restarted.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"], deepcopy(approval),
        )
        self.assertEqual((200, "published", head), (status, retry["outcome"], retry["published_revision"]["revision_id"]))
        workflow_after_retry = restarted._editor_version("campaign_alpha")
        audit_after_retry = tuple(restarted.proposal_repository.audit)
        intent_audit_after_retry = tuple(restarted.workflow.audit)
        inventory_after_retry = tuple(restarted.revisions.store.inventory())

        status, exact_replay = restarted.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"], deepcopy(approval),
        )
        self.assertEqual((200, retry), (status, exact_replay))
        self.assertEqual(workflow_after_retry, restarted._editor_version("campaign_alpha"))
        self.assertEqual(audit_after_retry, tuple(restarted.proposal_repository.audit))
        self.assertEqual(intent_audit_after_retry, tuple(restarted.workflow.audit))
        self.assertEqual(inventory_after_retry, tuple(restarted.revisions.store.inventory()))
        self.assertEqual(
            (200, retry),
            restarted.receipts.replay(
                "editor_proposal_approve",
                approval["operation_request"]["idempotency_key"],
                approval["operation_request"]["payload_digest"],
            ),
        )

    def test_restart_reconciles_editor_state_after_head_finalize_returns(self):
        _, _, (_, proposal) = self._edit("idem_editor_finalized_recovery")
        approval = self._editor_approval_payload(proposal)
        original_finalize_head = self.app.workflow.finalize_head

        def finalize_then_crash(intent):
            finalized = original_finalize_head(intent)
            self.assertTrue(finalized)
            raise SystemExit("crash after finalize_head")

        with mock.patch.object(
            self.app.workflow, "finalize_head", side_effect=finalize_then_crash
        ):
            with self.assertRaises(SystemExit):
                self._approve_editor(proposal)

        published_manifest = self.app.revisions.store.inventory()[-1]
        self.assertEqual(published_manifest.revision_id, self.workflow.head("campaign_alpha"))
        self.assertEqual(2, self.app._editor_version("campaign_alpha"))
        self.assertEqual(
            ProposalStatus.APPROVING,
            self.app.proposal_repository.get(
                proposal["proposal_id"], proposal["proposal_version"]
            ).status,
        )
        self.assertNotIn(
            (proposal["proposal_id"], proposal["proposal_version"], "published"),
            self.app.proposal_repository.audit,
        )

        restarted = SliceApplication(
            Path(self.tmp.name), provider=SyntheticProvider(), receipts=self.receipts,
            workflow_repository=self.workflow,
            proposal_repository=self.app.proposal_repository,
            atlas_repository=self.app.atlas_repository,
        )
        recovered = restarted.proposal_repository.get(
            proposal["proposal_id"], proposal["proposal_version"]
        )
        self.assertEqual(ProposalStatus.PUBLISHED, recovered.status)
        self.assertEqual(published_manifest.revision_id, recovered.published_revision_id)
        self.assertEqual(3, restarted._editor_version("campaign_alpha"))
        self.assertEqual(published_manifest.revision_id, restarted.workflow.head("campaign_alpha"))
        self.assertEqual(
            1,
            sum(
                item == (proposal["proposal_id"], proposal["proposal_version"], "published")
                for item in restarted.proposal_repository.audit
            ),
        )
        intent_id = restarted._id(
            "intent", proposal["proposal_id"], proposal["proposal_version"]
        )
        self.assertEqual(
            1,
            sum(item == (intent_id, "finalized") for item in restarted.workflow.audit),
        )
        projection = restarted.atlas_repository.get(
            "campaign_alpha", published_manifest.revision_id
        )
        self.assertEqual(
            (proposal["proposal_id"], proposal["proposal_version"]),
            (projection.history_entry.proposal_id, projection.history_entry.proposal_version),
        )

        status, retry = restarted.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"], deepcopy(approval)
        )
        self.assertEqual((200, "published"), (status, retry["outcome"]))
        self.assertEqual(
            published_manifest.revision_id,
            retry["published_revision"]["revision_id"],
        )
        self.assertEqual(
            published_manifest.tree_digest,
            retry["published_revision"]["tree_digest"],
        )
        self.assertEqual(
            (200, retry),
            restarted.receipts.replay(
                "editor_proposal_approve",
                approval["operation_request"]["idempotency_key"],
                approval["operation_request"]["payload_digest"],
            ),
        )
        audit_after_retry = tuple(restarted.proposal_repository.audit)
        workflow_audit_after_retry = tuple(restarted.workflow.audit)
        status, exact_replay = restarted.editor_proposal_approve(
            proposal["proposal_id"], proposal["proposal_version"], deepcopy(approval)
        )
        self.assertEqual((200, retry), (status, exact_replay))
        self.assertEqual(audit_after_retry, tuple(restarted.proposal_repository.audit))
        self.assertEqual(workflow_audit_after_retry, tuple(restarted.workflow.audit))

    def test_removal_impact_derives_typed_incoming_references(self):
        revision = self.app.workflow.head("campaign_alpha")
        status, impact = self.app.editor_removal_impact("campaign_alpha", revision, "campaign-main")
        self.assertEqual(200, status)
        self.assertEqual("server_derived_from_typed_connections", impact["binding"].get("backlink_policy", "server_derived_from_typed_connections"))
        self.assertEqual([], impact["incoming_references"])

    def test_mutation_preserves_source_sections_comments_and_single_connections_heading(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this section.

<!-- Preserve this source comment. -->
## Connections

- `guards` -> [[record-gate]] (`current`) — Watches the gate.

## Connections

- `visits` -> [[record-gate]] (`current`) — Checks in.

## Notes
Preserve this unrelated section.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["visibility"] = {"audience": "shared", "warden_only": False}
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(1, result.count("## Connections"))
        self.assertIn("<!-- Preserve this source comment. -->", result)
        self.assertIn("## Notes\nPreserve this unrelated section.", result)
        self.assertIn("warden_only: false", result)

    def test_mutation_preserves_crlf_without_doubled_carriage_returns(self):
        source = "---\r\nid: record-main\r\ntype: npc\r\nname: Keeper\r\nstatus: draft\r\nvisibility: warden\r\n---\r\n\r\n## Summary\r\nKeep this section.\r\n"
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Updated Keeper"
        candidate["sections"][0]["body"] = "First line\r\nSecond line"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertNotIn("\r\r\n", result)
        self.assertNotIn("\n\n", result.replace("\r\n", ""))
        self.assertEqual(result.count("\r\n"), result.count("\n"))
        self.assertIn("name: Updated Keeper\r\n", result)
        self.assertIn("First line\r\nSecond line\r\n", result)

    def test_create_rejects_unknown_adapter_record_type_before_workflow_claim(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate.update({"record_id": "record-unsupported", "record_type": "not-an-adapter-type", "displayed_name": "Unsupported"})
        candidate["content_digest"] = document_digest(candidate)
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_create_unsupported", "operation": "editor_record_create",
            "idempotency_key": "idem_create_unsupported", "payload_digest": "0" * 64,
            "expected_revision": revision, "expected_editor_workflow_version": 1,
            "subject_id": "record-unsupported",
        }
        payload = {
            "contract_name": "editor_record_create_request", "contract_version": 1,
            "operation_request": operation,
            "binding": {"campaign_id": "campaign_alpha", "base_revision": view["viewed_revision"],
                        "record_id": "record-unsupported", "record_digest": None,
                        "expected_editor_workflow_version": 1},
            "candidate": candidate,
        }
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        with self.assertRaises(HTTPFailure) as caught:
            self.app.editor_record_create("campaign_alpha", revision, payload)
        self.assertEqual((422, "record_type_unknown"), (caught.exception.status, caught.exception.payload["error"]["code"]))
        self.assertEqual(1, self.app._editor_version("campaign_alpha"))

    def test_removal_approval_stages_outgoing_and_incoming_connection_changes_atomically(self):
        target_revision = self._create_record("record-target", connections=[{
            "connection_id": "connection_target", "target_record_id": "campaign-main",
            "relationship": "related-to", "state": "current", "context": "Target context.",
        }])
        current_revision = self._create_record("record-source", connections=[{
            "connection_id": "connection_source", "target_record_id": "record-target",
            "relationship": "related-to", "state": "current", "context": "Source context.",
        }])
        _, impact = self.app.editor_removal_impact("campaign_alpha", current_revision, "record-target")
        reference = impact["incoming_references"][0]
        resolution = {"reference_id": reference["reference_id"], "action": "redirect", "replacement_target_record_id": "campaign-main"}
        workflow = self.app._editor_version("campaign_alpha")
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_remove_target", "operation": "editor_record_remove",
            "idempotency_key": "idem_remove_target", "payload_digest": "0" * 64,
            "expected_revision": current_revision, "expected_editor_workflow_version": workflow,
            "subject_id": "record-target",
        }
        payload = {
            "contract_name": "editor_record_remove_request", "contract_version": 1,
            "operation_request": operation, "binding": impact["binding"],
            "impact_digest": impact["impact_digest"],
            "impact_binding": {"binding": impact["binding"], "impact_digest": impact["impact_digest"]},
            "resolutions": [resolution],
        }
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        _, proposal = self.app.editor_record_remove("campaign_alpha", current_revision, "record-target", payload)
        self.assertEqual(3, len(proposal["diff"]["cards"]))
        self._approve_editor(proposal)
        new_revision = self.app.workflow.head("campaign_alpha")
        with self.assertRaises(HTTPFailure):
            self.app.editor_record_read("campaign_alpha", new_revision, "record-target")
        source = self.app.editor_record_read("campaign_alpha", new_revision, "record-source")[1]["record"]
        self.assertEqual("campaign-main", source["connections"][0]["target_record_id"])

    def test_correction_rebases_entered_content_to_current_head_without_stale_binding(self):
        old_revision, _, (_, proposal) = self._edit("idem_rebase")
        before = self.app._record("campaign_alpha", old_revision, "campaign-main")["content"]
        external = ExactTextChange(
            "change_external", "campaign-main", content_digest(before),
            before.replace("name: Editor", "name: External Head", 1), ChangeKind.UPDATE, "campaign",
        )
        external_item = self.app.proposals.draft(
            "proposal_external", "campaign_alpha", old_revision, (external,)
        )
        self.app.proposals.approve(
            external_item, diff_digest=external_item.diff_digest,
            base_revision=old_revision, payload_digest=external_item.payload_digest,
        )
        current_revision = self.app.workflow.head("campaign_alpha")
        current_view = self.app.editor_record_read("campaign_alpha", current_revision, "campaign-main")[1]
        entered = deepcopy(proposal["diff"]["cards"][0]["after"])
        binding = {
            "campaign_id": "campaign_alpha", "base_revision": current_view["viewed_revision"],
            "record_id": "campaign-main", "record_digest": current_view["record"]["content_digest"],
            "expected_editor_workflow_version": proposal["editor_workflow_version"],
        }
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_rebase", "operation": "editor_proposal_correct",
            "idempotency_key": "idem_rebase_correction", "payload_digest": "0" * 64,
            "expected_revision": current_revision, "expected_editor_workflow_version": proposal["editor_workflow_version"],
            "subject_id": proposal["proposal_id"],
        }
        correction = {
            "contract_name": "editor_proposal_correction_request", "contract_version": 1,
            "operation_request": operation,
            "prior_proposal": {"proposal_id": proposal["proposal_id"], "proposal_version": proposal["proposal_version"]},
            "binding": binding, "mutation_kind": "edit", "candidate": entered,
            "resolutions": [], "impact_digest": None, "impact_binding": None,
        }
        operation["payload_digest"] = canonical_digest(request_digest_input(correction))
        _, corrected = self.app.editor_proposal_correct(proposal["proposal_id"], 1, correction)
        self.assertEqual(2, corrected["proposal_version"])
        self.assertEqual(current_revision, corrected["base_revision"]["revision_id"])
        self.assertEqual("Edited Campaign", corrected["diff"]["cards"][0]["after"]["displayed_name"])
        self.assertEqual(old_revision, self.app.proposal_repository.get(proposal["proposal_id"], 1).base_revision)

    def test_published_proposal_read_is_schema_and_semantically_valid(self):
        _, _, (_, proposal) = self._edit("idem_published_read")
        _, result = self._approve_editor(proposal)
        _, read = self.app.editor_proposal_read(proposal["proposal_id"], proposal["proposal_version"])
        schema = json.loads(Path("docs/contracts/hosted/http/editor/v1/editor.schema.json").read_text())
        validator = Draft202012Validator(schema)
        self.assertEqual([], list(validator.iter_errors(result)))
        validate_editor_semantics(result)
        self.assertEqual([], list(validator.iter_errors(read)))
        validate_editor_semantics(read)
        published = read["publication"]["published_revision"]
        self.assertTrue(published["immutable"])
        self.assertEqual(read["base_revision"]["ordinal"] + 1, published["ordinal"])
