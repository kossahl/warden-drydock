from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.ai.provider import OpenAIResponsesAdapter
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input, text_digest
from warden_drydock.hosted.proposals.service import ProposalStatus
from warden_drydock.hosted.engine import Status
from warden_drydock.hosted.operations.server import Handler
from warden_drydock.hosted.http.repository import InMemoryHTTPRepository
from warden_drydock.hosted.revisions import InMemoryWorkflowRepository


class SliceApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.provider = SyntheticProvider()
        self.app = SliceApplication(Path(self.temporary.name), provider=self.provider)

    @staticmethod
    def operation(operation: str, request_id: str, key: str, *, expected_revision=None, subject_id=None, intent_digest=None) -> dict:
        value = {"contract_name": "operation_request", "contract_version": 2,
                 "request_id": request_id, "operation": operation,
                 "idempotency_key": key, "payload_digest": "0" * 64,
                 "expected_revision": expected_revision,
                 "expected_workflow_version": None}
        if subject_id is not None:
            value["subject_id"] = subject_id
        if intent_digest is not None:
            value["intent_digest"] = intent_digest
        return value

    @staticmethod
    def bind(payload: dict) -> dict:
        operation = payload.get("operation_request", payload)
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def consent(self) -> dict:
        readiness = self.app.provider_readiness()[1]
        payload = {"contract_name": "provider_consent_request", "contract_version": 2,
                   "operation_request": self.operation("provider_consent", "request_consent", "idem_consent"),
                   "input": {"explicit": True, "consent_identity_digest": readiness["consent_identity_digest"]}}
        return self.app.provider_consent(self.bind(payload))[1]

    def campaign(self, *, key="idem_campaign") -> dict:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_campaign", key),
                   "input": {"campaign_id": "campaign_alpha", "campaign_name": "Synthetic Campaign", "adapter_id": "mothership"}}
        return self.app.create_campaign(self.bind(payload))[1]

    def generation(self, *, generation_id="generation_alpha") -> dict:
        self.consent()
        revision = self.campaign()["head_revision"]
        request = {"contract_name": "generation_start_request", "contract_version": 2,
                   "generation_id": generation_id, "campaign_id": "campaign_alpha",
                   "source_revision": revision, "action": "ask",
                   "prompt": "What is the campaign called?", "context": {"scope": "campaign"}}
        status, pending, reserved = self.app.start_generation("campaign_alpha", revision, request)
        self.assertEqual((202, True, "pending", 0), (status, reserved, pending["status"], self.provider.calls))
        self.app.dispatch_generation(generation_id)
        return self.app.generation_view(generation_id)[1]

    def proposal(self) -> dict:
        generation = self.generation()
        payload = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_proposal", "idempotency_key": "idem_proposal",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": "proposal_alpha", "campaign_id": generation["campaign_id"],
                   "source_revision": generation["source_revision"], "base_revision": generation["source_revision"],
                   "source_set_digest": generation["source_set_digest"],
                   "terminal_draft_digest": generation["terminal_content_digest"], "subject_id": "campaign-main"}
        return self.app.create_proposal(generation["generation_id"], self.bind(payload))[1]

    def approval(self, proposal: dict, *, key="idem_approval") -> dict:
        operation = self.operation("proposal_approve", "request_approval", key,
                                   expected_revision=proposal["base_revision"],
                                   subject_id=proposal["proposal_id"], intent_digest=proposal["diff_digest"])
        payload = {"contract_name": "proposal_approval_request", "contract_version": 2,
                   "operation_request": operation, "proposal_id": proposal["proposal_id"],
                   "proposal_version": proposal["proposal_version"], "source_revision": proposal["source_revision"],
                   "base_revision": proposal["base_revision"], "expected_campaign_head": proposal["base_revision"],
                   "diff_digest": proposal["diff_digest"], "proposal_payload_digest": proposal["proposal_payload_digest"],
                   "warden_confirmed": True}
        return self.bind(payload)

    def test_create_replay_is_exact_and_does_not_allocate_again(self) -> None:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_campaign", "idem_campaign"),
                   "input": {"campaign_id": "campaign_alpha", "campaign_name": "Synthetic Campaign", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        first = self.app.create_campaign(payload)
        second = self.app.create_campaign(deepcopy(payload))
        self.assertEqual((201, 200), (first[0], second[0]))
        self.assertEqual(first[1], second[1])
        self.assertEqual(1, len(self.app.campaigns))

    def test_readiness_marks_unusable_file_credential_unavailable_with_stale_consent(self) -> None:
        secret_root = Path(self.temporary.name) / "provider-secrets"
        secret_root.mkdir()
        secret = secret_root / "openai_api_key"
        secret.write_text("synthetic-credential", encoding="utf-8")
        dispatches = []
        self.provider = OpenAIResponsesAdapter(lambda payload: dispatches.append(payload))
        self.app = SliceApplication(Path(self.temporary.name) / "file-provider", provider=self.provider)
        with mock.patch.dict(os.environ, {
            "DRYDOCK_SECRETS": str(secret_root),
            "OPENAI_API_KEY_FILE": str(secret),
        }, clear=True):
            self.consent()
            secret.write_text(" \n", encoding="utf-8")
            status, readiness = self.app.provider_readiness()
        self.assertEqual(200, status)
        self.assertEqual({
            "provider_configured": True,
            "provider_available": False,
            "consent_current": False,
            "consent_identity_digest": None,
            "ai_available": False,
        }, {key: readiness[key] for key in (
            "provider_configured", "provider_available", "consent_current",
            "consent_identity_digest", "ai_available",
        )})
        self.assertEqual([], dispatches)

    def test_restart_recovers_snapshot_layout_workspaces_and_counter(self) -> None:
        runtime = Path(self.temporary.name) / "private-runtime"
        snapshots = Path(self.temporary.name) / "established-snapshots"
        receipts = InMemoryHTTPRepository()
        workflow = InMemoryWorkflowRepository()
        first = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider, receipts=receipts,
                                 workflow_repository=workflow)
        self.app = first
        created = self.campaign()
        revision_id = created["head_revision"]
        first_handle = first.campaigns["campaign_alpha"].workspaces[revision_id]
        self.assertTrue((snapshots / "snapshots" / created["viewed_revision"]["tree_digest"]).is_dir())
        self.assertFalse((runtime / "revision-store").exists())

        self.assertFalse((runtime / "http-state-v1.json").exists())
        (runtime / "http-state-v1.json").write_text("corrupt", encoding="utf-8")
        restarted = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider, receipts=receipts,
                                     workflow_repository=workflow)
        record = restarted.record_view("campaign_alpha", revision_id, "campaign-main")[1]
        self.assertEqual((revision_id, "campaign-main"), (record["revision_id"], record["record_id"]))
        self.assertEqual(revision_id, restarted.revision_view("campaign_alpha", revision_id)[1]["head_revision"])

        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_second", "idem_second"),
                   "input": {"campaign_id": "campaign_second", "campaign_name": "Second Campaign", "adapter_id": "mothership"}}
        second = restarted.create_campaign(self.bind(payload))[1]
        second_handle = restarted.campaigns["campaign_second"].workspaces[second["head_revision"]]
        self.assertNotEqual(first_handle, second_handle)
        self.assertGreater(int(second_handle.value.rsplit("_", 1)[1]), int(first_handle.value.rsplit("_", 1)[1]))

    def test_restart_rematerializes_workspace_that_differs_from_immutable_snapshot(self) -> None:
        created = self.campaign()
        handle = self.app.campaigns["campaign_alpha"].workspaces[created["head_revision"]]
        workspace = self.app.registry._resolve(handle)
        readme = workspace / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nTampered.\n", encoding="utf-8")
        restarted = SliceApplication(
            Path(self.temporary.name), provider=self.provider, receipts=self.app.receipts,
            workflow_repository=self.app.workflow,
        )
        recovered = restarted.registry._resolve(restarted.campaigns["campaign_alpha"].workspaces[created["head_revision"]])
        self.assertNotIn("Tampered.", (recovered / "README.md").read_text(encoding="utf-8"))

    def test_restart_recovers_proposal_provenance_and_published_linkage(self) -> None:
        runtime = Path(self.temporary.name) / "proposal-runtime"
        snapshots = Path(self.temporary.name) / "proposal-snapshots"
        receipts = InMemoryHTTPRepository()
        workflow = InMemoryWorkflowRepository()
        first = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider,
                                 receipts=receipts, workflow_repository=workflow)
        self.app = first
        proposal = self.proposal()
        approval = self.approval(proposal)
        result = first.approve_proposal("proposal_alpha", 1, approval)[1]
        revision_id = result["published_revision"]["revision_id"]

        restarted = SliceApplication(
            runtime, snapshot_root=snapshots, provider=self.provider,
            receipts=receipts, workflow_repository=workflow,
            proposal_repository=first.proposal_repository,
            ai_repository=first.ai_repository,
        )
        recovered = restarted.proposal_view("proposal_alpha", 1)[1]
        self.assertEqual(("published", revision_id),
                         (recovered["status"], recovered["published_revision_id"]))
        self.assertEqual(revision_id, restarted.record_view("campaign_alpha", revision_id, "campaign-main")[1]["revision_id"])
        replay = restarted.approve_proposal("proposal_alpha", 1, deepcopy(approval))[1]
        self.assertTrue(replay["exact_replay"])

    def test_campaign_validation_and_claim_precede_initializer(self) -> None:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_bad", "idem_bad"),
                   "input": {"campaign_id": "BAD", "campaign_name": "Bad", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        with mock.patch.object(self.app.receipts, "claim", wraps=self.app.receipts.claim) as claim, \
             mock.patch.object(self.app.engine, "initialize", wraps=self.app.engine.initialize) as initialize:
            with self.assertRaises(HTTPFailure) as caught:
                self.app.create_campaign(payload)
        self.assertEqual(422, caught.exception.status)
        claim.assert_not_called()
        initialize.assert_not_called()
        self.assertEqual({}, self.app.registry._paths)
        self.assertIsNone(self.app.workflow.head("BAD"))

    def test_gate_failure_has_no_head_or_snapshot_and_exact_retry_replays_failure(self) -> None:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_gate", "idem_gate"),
                   "input": {"campaign_id": "campaign_gate", "campaign_name": "Gate Campaign", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        order = []
        real_claim = self.app.receipts.claim
        real_initialize = self.app.engine.initialize
        def claim(*args):
            order.append("claim")
            return real_claim(*args)
        def initialize(*args):
            order.append("initialize")
            return real_initialize(*args)
        invalid = type("Invalid", (), {"status": Status.INVALID})()
        with mock.patch.object(self.app.receipts, "claim", side_effect=claim), \
             mock.patch.object(self.app.engine, "initialize", side_effect=initialize) as initialize_call, \
             mock.patch.object(self.app.engine, "index", return_value=invalid):
            with self.assertRaises(HTTPFailure) as caught:
                self.app.create_campaign(payload)
            retry = self.app.create_campaign(deepcopy(payload))
        self.assertEqual(["claim", "initialize"], order)
        self.assertEqual(1, initialize_call.call_count)
        self.assertEqual((422, "campaign_index_failed"), (caught.exception.status, caught.exception.payload["error"]["code"]))
        self.assertEqual((422, caught.exception.payload), retry)
        self.assertIsNone(self.app.workflow.head("campaign_gate"))
        self.assertEqual((), self.app.revisions.store.inventory())
        self.assertEqual({}, self.app.registry._paths)

    def test_transient_prepublication_failure_releases_claim_for_exact_retry(self) -> None:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_transient", "idem_transient"),
                   "input": {"campaign_id": "campaign_transient", "campaign_name": "Transient Campaign", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        with mock.patch.object(self.app.engine, "initialize", side_effect=RuntimeError("temporary failure")):
            with self.assertRaises(HTTPFailure) as caught:
                self.app.create_campaign(payload)
        self.assertEqual((503, True), (caught.exception.status, caught.exception.payload["error"]["retryable"]))
        status, created = self.app.create_campaign(deepcopy(payload))
        self.assertEqual((201, "campaign_transient"), (status, created["campaign_id"]))

    def test_campaign_pipeline_order_is_claim_initialize_index_context_validate_publish(self) -> None:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_order", "idem_order"),
                   "input": {"campaign_id": "campaign_order", "campaign_name": "Ordered Campaign", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        order = []
        def ordered(name, function):
            def call(*args, **kwargs):
                order.append(name)
                return function(*args, **kwargs)
            return call
        with mock.patch.object(self.app.receipts, "claim", side_effect=ordered("claim", self.app.receipts.claim)), \
             mock.patch.object(self.app.engine, "initialize", side_effect=ordered("initialize", self.app.engine.initialize)), \
             mock.patch.object(self.app.engine, "index", side_effect=ordered("index", self.app.engine.index)), \
             mock.patch.object(self.app.engine, "context", side_effect=ordered("context", self.app.engine.context)), \
             mock.patch.object(self.app.engine, "validate", side_effect=ordered("validate", self.app.engine.validate)), \
             mock.patch.object(self.app.revisions, "publish", side_effect=ordered("publish", self.app.revisions.publish)):
            self.app.create_campaign(payload)
        self.assertEqual(["claim", "initialize", "index", "context", "validate", "publish"], order)

    def test_campaign_crash_after_publish_reconciles_pending_receipt_without_sidecar(self) -> None:
        runtime = Path(self.temporary.name) / "campaign-crash-runtime"
        snapshots = Path(self.temporary.name) / "campaign-crash-snapshots"
        workflow = InMemoryWorkflowRepository()
        receipts = InMemoryHTTPRepository()
        first = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider,
                                 workflow_repository=workflow, receipts=receipts)
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_crash", "idem_crash"),
                   "input": {"campaign_id": "campaign_crash", "campaign_name": "Crash Campaign", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        with mock.patch.object(first, "revision_view", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                first.create_campaign(payload)
        state = runtime / "http-state-v1.json"
        if state.exists():
            state.write_text("corrupt", encoding="utf-8")
        restarted = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider,
                                     workflow_repository=workflow, receipts=receipts)
        status, recovered = restarted.create_campaign(deepcopy(payload))
        self.assertEqual((200, "campaign_crash"), (status, recovered["campaign_id"]))
        self.assertEqual(1, len(restarted.campaigns["campaign_crash"].revisions))

    def test_campaign_crash_after_cache_before_receipt_reconciles(self) -> None:
        runtime = Path(self.temporary.name) / "campaign-receipt-runtime"
        snapshots = Path(self.temporary.name) / "campaign-receipt-snapshots"
        workflow = InMemoryWorkflowRepository()
        receipts = InMemoryHTTPRepository()
        first = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider,
                                 workflow_repository=workflow, receipts=receipts)
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_receipt", "idem_receipt"),
                   "input": {"campaign_id": "campaign_receipt", "campaign_name": "Receipt Campaign", "adapter_id": "mothership"}}
        payload = self.bind(payload)
        with mock.patch.object(first, "_store", side_effect=RuntimeError("receipt unavailable")):
            with self.assertRaises(HTTPFailure) as caught:
                first.create_campaign(payload)
        self.assertEqual((503, "campaign_creation_failed"),
                         (caught.exception.status, caught.exception.payload["error"]["code"]))
        restarted = SliceApplication(runtime, snapshot_root=snapshots, provider=self.provider,
                                     workflow_repository=workflow, receipts=receipts)
        self.assertEqual(200, restarted.create_campaign(deepcopy(payload))[0])

    def test_start_persists_sources_before_dispatch_and_resume_detects_gap(self) -> None:
        generation = self.generation()
        self.assertEqual("complete", generation["status"])
        self.assertEqual("preparation", generation["sources"][0]["authority"])
        self.assertEqual(1, self.provider.calls)
        events = self.app.generation_events("generation_alpha", after=0, last_event_id=None)[1]
        self.assertEqual([1, 2, 3], [item["sequence"] for item in events])
        with self.assertRaises(HTTPFailure) as caught:
            self.app.generation_events("generation_alpha", after=9, last_event_id=None)
        self.assertEqual((409, "stream_sequence_conflict"), (caught.exception.status, caught.exception.payload["error"]["category"]))

    def test_pending_generation_is_dispatchable_after_restart_and_exact_start_retry(self) -> None:
        self.consent()
        revision = self.campaign()["head_revision"]
        ask = {"contract_name": "generation_start_request", "contract_version": 2,
               "generation_id": "generation_restart", "campaign_id": "campaign_alpha",
               "source_revision": revision, "action": "ask", "prompt": "What is the campaign called?", "context": {"scope": "campaign"}}
        self.assertTrue(self.app.start_generation("campaign_alpha", revision, ask)[2])
        restarted = SliceApplication(
            Path(self.temporary.name), provider=self.provider, receipts=self.app.receipts,
            workflow_repository=self.app.workflow, ai_repository=self.app.ai_repository,
            proposal_repository=self.app.proposal_repository,
        )
        status, pending, should_dispatch = restarted.start_generation("campaign_alpha", revision, deepcopy(ask))
        self.assertEqual((202, "pending", True), (status, pending["status"], should_dispatch))
        workers = [threading.Thread(target=restarted.dispatch_generation, args=("generation_restart",)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual((1, "complete"), (self.provider.calls, restarted.generation_view("generation_restart")[1]["status"]))

    def test_ask_generation_id_digest_conflict_is_sanitized_409_without_dispatch(self) -> None:
        self.consent()
        revision = self.campaign()["head_revision"]
        request = {"contract_name": "generation_start_request", "contract_version": 2,
                   "generation_id": "generation_conflict", "campaign_id": "campaign_alpha",
                   "source_revision": revision, "action": "ask", "prompt": "What is the campaign called?", "context": {"scope": "campaign"}}
        self.app.start_generation("campaign_alpha", revision, request)
        conflicting = {**request, "prompt": "What is the campaign name?"}
        with self.assertRaises(HTTPFailure) as caught:
            self.app.start_generation("campaign_alpha", revision, conflicting)
        self.assertEqual((409, "idempotency_digest_conflict"),
                         (caught.exception.status, caught.exception.payload["error"]["code"]))
        stored = self.app.ai_repository.get_generation("generation_conflict")
        self.assertEqual(("What is the campaign called?", [], 0), (stored.request.prompt, stored.events, self.provider.calls))

    def test_proposal_is_server_constructed_and_canon_changes_only_after_approval(self) -> None:
        proposal = self.proposal()
        before = self.app.record_view("campaign_alpha", proposal["base_revision"], "campaign-main")[1]
        change = proposal["exact_diff"][0]
        self.assertEqual(before["content"] + "\n\n## Proposed addition\n\n" + self.app.generation_view("generation_alpha")[1]["terminal_content"], change["after_content"])
        self.assertEqual(change["from_authority"], change["to_authority"])
        self.assertNotIn("## Proposed addition", before["content"])

        status, result = self.app.approve_proposal("proposal_alpha", 1, self.approval(proposal))
        self.assertEqual((200, "published"), (status, result["outcome"]))
        revision_id = result["published_revision"]["revision_id"]
        published = self.app.record_view("campaign_alpha", revision_id, "campaign-main")[1]
        self.assertEqual(change["after_content"], published["content"])
        retry = self.app.approve_proposal("proposal_alpha", 1, self.approval(proposal))[1]
        self.assertTrue(retry["exact_replay"])

    def test_proposal_create_receipt_prevents_second_version(self) -> None:
        proposal = self.proposal()
        generation = self.app.generation_view("generation_alpha")[1]
        payload = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_proposal", "idempotency_key": "idem_proposal",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": proposal["proposal_id"], "campaign_id": proposal["campaign_id"],
                   "source_revision": proposal["source_revision"], "base_revision": proposal["base_revision"],
                   "source_set_digest": proposal["source_set_digest"], "terminal_draft_digest": proposal["terminal_draft_digest"],
                   "subject_id": "campaign-main"}
        status, replay = self.app.create_proposal("generation_alpha", self.bind(payload))
        self.assertEqual((200, proposal), (status, replay))
        self.assertEqual([1], [item.version for item in self.app.proposal_repository.items.values()])

    def test_http_proposal_create_rejects_client_supplied_multi_change_shape(self) -> None:
        generation = self.generation()
        payload = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_multi", "idempotency_key": "idem_multi",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": "proposal_multi", "campaign_id": generation["campaign_id"],
                   "source_revision": generation["source_revision"], "base_revision": generation["source_revision"],
                   "source_set_digest": generation["source_set_digest"],
                   "terminal_draft_digest": generation["terminal_content_digest"], "subject_id": "campaign-main",
                   "changes": [{"change_id": "change_one"}, {"change_id": "change_two"}]}
        with self.assertRaises(HTTPFailure) as caught:
            self.app.create_proposal(generation["generation_id"], self.bind(payload))
        self.assertEqual((422, "invalid_request_shape"),
                         (caught.exception.status, caught.exception.payload["error"]["code"]))
        self.assertEqual({}, self.app.proposal_repository.items)

    def test_proposal_create_crash_after_add_reconciles_pending_receipt(self) -> None:
        generation = self.generation()
        payload = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_crash_proposal", "idempotency_key": "idem_crash_proposal",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": "proposal_crash", "campaign_id": generation["campaign_id"],
                   "source_revision": generation["source_revision"], "base_revision": generation["source_revision"],
                   "source_set_digest": generation["source_set_digest"],
                   "terminal_draft_digest": generation["terminal_content_digest"], "subject_id": "campaign-main"}
        payload = self.bind(payload)
        with mock.patch.object(self.app, "_store", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.app.create_proposal(generation["generation_id"], payload)
        status, recovered = self.app.create_proposal(generation["generation_id"], deepcopy(payload))
        self.assertEqual((200, 1), (status, recovered["proposal_version"]))
        self.assertEqual([1], [item.version for item in self.app.proposal_repository.versions("proposal_crash")])

    def test_restart_recovers_abandoned_proposal_create_claim_before_mutation(self) -> None:
        generation = self.generation()
        payload = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_abandoned", "idempotency_key": "idem_abandoned",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": "proposal_abandoned", "campaign_id": generation["campaign_id"],
                   "source_revision": generation["source_revision"], "base_revision": generation["source_revision"],
                   "source_set_digest": generation["source_set_digest"],
                   "terminal_draft_digest": generation["terminal_content_digest"], "subject_id": "campaign-main"}
        payload = self.bind(payload)
        self.assertTrue(self.app.receipts.claim("proposal_create", "idem_abandoned", payload["payload_digest"]))
        restarted = SliceApplication(
            Path(self.temporary.name), provider=self.provider, receipts=self.app.receipts,
            workflow_repository=self.app.workflow, ai_repository=self.app.ai_repository,
            proposal_repository=self.app.proposal_repository,
        )
        status, created = restarted.create_proposal(generation["generation_id"], payload)
        self.assertEqual((201, 1), (status, created["proposal_version"]))

    def test_restart_recovers_abandoned_claims_for_every_mutating_operation(self) -> None:
        operations = ("provider_consent", "campaign_create", "proposal_create",
                      "proposal_correct", "proposal_reject", "proposal_approve")
        receipts = InMemoryHTTPRepository()
        for operation in operations:
            self.assertTrue(receipts.claim(operation, f"idem_{operation}", "a" * 64))
        restarted = SliceApplication(Path(self.temporary.name) / "abandoned-all", provider=self.provider,
                                     receipts=receipts)
        self.assertEqual(
            {(operation, f"idem_{operation}", "a" * 64) for operation in operations},
            restarted._abandoned_claims,
        )
        for operation in operations:
            self.assertTrue(receipts.claim(operation, f"idem_{operation}", "a" * 64))

    def test_pending_generation_cannot_create_proposal(self) -> None:
        self.consent()
        revision = self.campaign()["head_revision"]
        ask = {"contract_name": "generation_start_request", "contract_version": 2,
               "generation_id": "generation_pending", "campaign_id": "campaign_alpha",
               "source_revision": revision, "action": "ask", "prompt": "What is the campaign called?", "context": {"scope": "campaign"}}
        self.app.start_generation("campaign_alpha", revision, ask)
        generation = self.app.generation_view("generation_pending")[1]
        request = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_pending", "idempotency_key": "idem_pending",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": "proposal_pending", "campaign_id": generation["campaign_id"],
                   "source_revision": generation["source_revision"], "base_revision": generation["source_revision"],
                   "source_set_digest": generation["source_set_digest"],
                   "terminal_draft_digest": "0" * 64, "subject_id": "campaign-main"}
        with self.assertRaises(HTTPFailure) as caught:
            self.app.create_proposal("generation_pending", self.bind(request))
        self.assertEqual((422, "generation_not_complete"),
                         (caught.exception.status, caught.exception.payload["error"]["code"]))
        self.assertEqual({}, self.app.proposal_repository.items)

    def test_failed_generation_cannot_create_proposal(self) -> None:
        self.app = SliceApplication(Path(self.temporary.name) / "failed-proposal", provider=SyntheticProvider(failures=1))
        generation = self.generation(generation_id="generation_failed_proposal")
        self.assertEqual("failed", generation["status"])
        request = {"contract_name": "proposal_create_request", "contract_version": 2,
                   "request_id": "request_failed_proposal", "idempotency_key": "idem_failed_proposal",
                   "payload_digest": "0" * 64, "generation_id": generation["generation_id"],
                   "proposal_id": "proposal_failed", "campaign_id": generation["campaign_id"],
                   "source_revision": generation["source_revision"], "base_revision": generation["source_revision"],
                   "source_set_digest": generation["source_set_digest"],
                   "terminal_draft_digest": "0" * 64, "subject_id": "campaign-main"}
        with self.assertRaises(HTTPFailure) as caught:
            self.app.create_proposal(generation["generation_id"], self.bind(request))
        self.assertEqual((422, "generation_not_complete"),
                         (caught.exception.status, caught.exception.payload["error"]["code"]))
        self.assertEqual({}, self.app.proposal_repository.items)

    def test_stale_head_returns_preserved_conflict_without_publication(self) -> None:
        proposal = self.proposal()
        self.app.workflow.heads["campaign_alpha"] = ("revision_external", 2)
        status, result = self.app.approve_proposal("proposal_alpha", 1, self.approval(proposal))
        self.assertEqual((409, "conflict", None, "stale_revision"),
                         (status, result["outcome"], result["published_revision"], result["error"]["category"]))
        self.assertEqual(proposal["exact_diff"], result["proposal"]["exact_diff"])
        self.assertEqual(1, len(self.app.campaigns["campaign_alpha"].revisions))

    def test_approval_crash_after_publish_reconciles_and_does_not_republish(self) -> None:
        proposal = self.proposal()
        request = self.approval(proposal, key="idem_publish_crash")
        with mock.patch.object(self.app.proposal_repository, "finalize", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.app.approve_proposal("proposal_alpha", 1, request)
        revision_count = len(self.app.revisions.store.inventory())
        restarted = SliceApplication(
            Path(self.temporary.name), provider=self.provider, receipts=self.app.receipts,
            workflow_repository=self.app.workflow, ai_repository=self.app.ai_repository,
            proposal_repository=self.app.proposal_repository,
        )
        status, result = restarted.approve_proposal("proposal_alpha", 1, deepcopy(request))
        self.assertEqual((200, "published", True), (status, result["outcome"], result["exact_replay"]))
        self.assertEqual(revision_count, len(restarted.revisions.store.inventory()))

    def test_approval_restart_after_domain_claim_before_publication_resumes_once(self) -> None:
        proposal = self.proposal()
        request = self.approval(proposal, key="idem_domain_claim_crash")
        digest = request["operation_request"]["payload_digest"]
        self.assertTrue(self.app.receipts.claim("proposal_approve", "idem_domain_claim_crash", digest))
        claimed = self.app.proposal_repository.claim(self.app._proposal_item("proposal_alpha", 1))
        self.assertEqual(ProposalStatus.APPROVING, claimed.status)
        restarted = SliceApplication(
            Path(self.temporary.name), provider=self.provider, receipts=self.app.receipts,
            workflow_repository=self.app.workflow, ai_repository=self.app.ai_repository,
            proposal_repository=self.app.proposal_repository,
        )
        status, result = restarted.approve_proposal("proposal_alpha", 1, deepcopy(request))
        self.assertEqual((200, "published"), (status, result["outcome"]))
        self.assertEqual(2, len(restarted.revisions.store.inventory()))
        replay = restarted.approve_proposal("proposal_alpha", 1, deepcopy(request))[1]
        self.assertTrue(replay["exact_replay"])
        self.assertEqual(2, len(restarted.revisions.store.inventory()))

    def test_approval_crash_after_finalize_before_receipt_reconciles(self) -> None:
        proposal = self.proposal()
        request = self.approval(proposal, key="idem_finalize_crash")
        with mock.patch.object(self.app, "_store", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.app.approve_proposal("proposal_alpha", 1, request)
        restarted = SliceApplication(
            Path(self.temporary.name), provider=self.provider, receipts=self.app.receipts,
            workflow_repository=self.app.workflow, ai_repository=self.app.ai_repository,
            proposal_repository=self.app.proposal_repository,
        )
        status, result = restarted.approve_proposal("proposal_alpha", 1, deepcopy(request))
        self.assertEqual((200, "published", True), (status, result["outcome"], result["exact_replay"]))

    def test_binding_mismatch_fails_before_claim(self) -> None:
        proposal = self.proposal()
        request = self.approval(proposal)
        request["diff_digest"] = "0" * 64
        request = self.bind(request)
        with self.assertRaises(HTTPFailure) as caught:
            self.app.approve_proposal("proposal_alpha", 1, request)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(ProposalStatus.DRAFT, self.app._proposal_item("proposal_alpha", 1).status)

    def test_repository_missing_proposal_maps_to_contract_404(self) -> None:
        self.app.proposal_repository.get = mock.Mock(return_value=None)
        with self.assertRaises(HTTPFailure) as caught:
            self.app.proposal_view("proposal_missing", 1)
        self.assertEqual(
            (404, "not_found", "proposal_not_found"),
            (caught.exception.status, caught.exception.payload["error"]["category"], caught.exception.payload["error"]["code"]),
        )

    def test_correction_keeps_one_change_and_rejection_is_idempotent(self) -> None:
        proposal = self.proposal()
        change = proposal["exact_diff"][0]
        corrected_content = change["after_content"] + "\nCorrection."
        operation = self.operation(
            "proposal_correct", "request_correct", "idem_correct",
            expected_revision=proposal["base_revision"], subject_id=proposal["proposal_id"],
        )
        request = {"contract_name": "proposal_correction_request", "contract_version": 2,
                   "operation_request": operation, "proposal_id": proposal["proposal_id"],
                   "proposal_version": 1, "source_revision": proposal["source_revision"],
                   "base_revision": proposal["base_revision"], "change_id": change["change_id"],
                   "subject_id": change["subject_id"], "after_content": corrected_content}
        status, corrected = self.app.correct_proposal("proposal_alpha", 1, self.bind(request))
        self.assertEqual((201, 2, 1, corrected_content),
                         (status, corrected["proposal_version"], len(corrected["exact_diff"]), corrected["exact_diff"][0]["after_content"]))

        reject_operation = self.operation(
            "proposal_reject", "request_reject", "idem_reject",
            expected_revision=corrected["base_revision"], subject_id=corrected["proposal_id"],
        )
        rejection = {"contract_name": "proposal_rejection_request", "contract_version": 2,
                     "operation_request": reject_operation, "proposal_id": corrected["proposal_id"],
                     "proposal_version": 2, "source_revision": corrected["source_revision"],
                     "base_revision": corrected["base_revision"]}
        rejection = self.bind(rejection)
        first = self.app.reject_proposal("proposal_alpha", 2, rejection)
        second = self.app.reject_proposal("proposal_alpha", 2, deepcopy(rejection))
        self.assertEqual((200, 200, "rejected"), (first[0], second[0], second[1]["status"]))

    def test_correction_and_rejection_pending_receipts_reconcile_after_mutation(self) -> None:
        proposal = self.proposal()
        change = proposal["exact_diff"][0]
        operation = self.operation(
            "proposal_correct", "request_correct_crash", "idem_correct_crash",
            expected_revision=proposal["base_revision"], subject_id=proposal["proposal_id"],
        )
        correction = {"contract_name": "proposal_correction_request", "contract_version": 2,
                      "operation_request": operation, "proposal_id": proposal["proposal_id"],
                      "proposal_version": 1, "source_revision": proposal["source_revision"],
                      "base_revision": proposal["base_revision"], "change_id": change["change_id"],
                      "subject_id": change["subject_id"], "after_content": change["after_content"] + "\nRecovered."}
        correction = self.bind(correction)
        with mock.patch.object(self.app, "_store", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.app.correct_proposal("proposal_alpha", 1, correction)
        status, corrected = self.app.correct_proposal("proposal_alpha", 1, deepcopy(correction))
        self.assertEqual((200, 2), (status, corrected["proposal_version"]))

        reject_operation = self.operation(
            "proposal_reject", "request_reject_crash", "idem_reject_crash",
            expected_revision=corrected["base_revision"], subject_id=corrected["proposal_id"],
        )
        rejection = self.bind({"contract_name": "proposal_rejection_request", "contract_version": 2,
            "operation_request": reject_operation, "proposal_id": corrected["proposal_id"],
            "proposal_version": 2, "source_revision": corrected["source_revision"],
            "base_revision": corrected["base_revision"]})
        with mock.patch.object(self.app, "_store", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.app.reject_proposal("proposal_alpha", 2, rejection)
        status, rejected = self.app.reject_proposal("proposal_alpha", 2, deepcopy(rejection))
        self.assertEqual((200, "rejected"), (status, rejected["status"]))

    def test_correction_cannot_change_record_authority_or_allocate_version(self) -> None:
        proposal = self.proposal()
        change = proposal["exact_diff"][0]
        corrected_content = change["after_content"].replace("status: draft", "status: canon", 1)
        operation = self.operation(
            "proposal_correct", "request_authority", "idem_authority",
            expected_revision=proposal["base_revision"], subject_id=proposal["proposal_id"],
        )
        request = {"contract_name": "proposal_correction_request", "contract_version": 2,
                   "operation_request": operation, "proposal_id": proposal["proposal_id"],
                   "proposal_version": 1, "source_revision": proposal["source_revision"],
                   "base_revision": proposal["base_revision"], "change_id": change["change_id"],
                   "subject_id": change["subject_id"], "after_content": corrected_content}
        with self.assertRaises(HTTPFailure) as caught:
            self.app.correct_proposal("proposal_alpha", 1, self.bind(request))
        self.assertEqual((422, "authority_transition_not_allowed"),
                         (caught.exception.status, caught.exception.payload["error"]["code"]))
        self.assertEqual([("proposal_alpha", 1)], list(self.app.proposal_repository.items))
        self.assertEqual(1, len(self.app.campaigns["campaign_alpha"].revisions))
        self.assertEqual(proposal["base_revision"], self.app.workflow.head("campaign_alpha"))

    def test_invalid_correction_is_rejected_before_version_allocation(self) -> None:
        proposal = self.proposal()
        change = proposal["exact_diff"][0]
        corrected_content = change["after_content"].replace("ownership: campaign", "ownership: invalid", 1)
        self.assertNotEqual(change["after_content"], corrected_content)
        operation = self.operation(
            "proposal_correct", "request_invalid_correction", "idem_invalid_correction",
            expected_revision=proposal["base_revision"], subject_id=proposal["proposal_id"],
        )
        request = {"contract_name": "proposal_correction_request", "contract_version": 2,
                   "operation_request": operation, "proposal_id": proposal["proposal_id"],
                   "proposal_version": 1, "source_revision": proposal["source_revision"],
                   "base_revision": proposal["base_revision"], "change_id": change["change_id"],
                   "subject_id": change["subject_id"], "after_content": corrected_content}
        with self.assertRaises(HTTPFailure) as caught:
            self.app.correct_proposal("proposal_alpha", 1, self.bind(request))
        self.assertEqual((422, "proposal_validation_failure"),
                         (caught.exception.status, caught.exception.payload["error"]["category"]))
        self.assertEqual([("proposal_alpha", 1)], list(self.app.proposal_repository.items))
        self.assertEqual(1, len(self.app.campaigns["campaign_alpha"].revisions))

    def test_provider_failure_is_resumable_and_contains_no_raw_error(self) -> None:
        self.app = SliceApplication(Path(self.temporary.name) / "failure", provider=SyntheticProvider(failures=1))
        generation = self.generation(generation_id="generation_failed")
        self.assertEqual("failed", generation["status"])
        events = self.app.generation_events("generation_failed", after=0, last_event_id=None)[1]
        self.assertEqual("failure", events[-1]["event_type"])
        self.assertNotIn("synthetic unavailable", repr(events))

    def test_http_routes_create_and_read_the_bound_revision(self) -> None:
        static = Path(self.temporary.name) / "static"
        static.mkdir()
        (static / "index.html").write_text("ok", encoding="utf-8")
        Handler.application = self.app
        prior_csrf = Handler.csrf_secret
        Handler.csrf_secret = "a" * 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        original = {name: os.environ.get(name) for name in ("DRYDOCK_ALLOWED_HOSTS", "DRYDOCK_STATIC")}
        os.environ["DRYDOCK_ALLOWED_HOSTS"] = f"127.0.0.1:{server.server_port}"
        os.environ["DRYDOCK_STATIC"] = str(static)
        thread.start()
        def cleanup():
            server.shutdown()
            thread.join(5)
            server.server_close()
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            Handler.application = None
            Handler.csrf_secret = prior_csrf
        self.addCleanup(cleanup)

        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/v1/provider/readiness") as response:
            csrf = response.headers["X-CSRF-Token"]
            cookie_header = response.headers["Set-Cookie"]
            cookie = cookie_header.split(";", 1)[0]
            readiness = json.load(response)
        self.assertTrue(readiness["provider_configured"])
        self.assertIn("SameSite=Strict", cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        for headers in ({"Host": "invalid.example"}, {"Origin": "http://invalid.example"}):
            invalid_binding = urllib.request.Request(
                base + "/api/v1/provider/readiness", headers=headers,
            )
            with self.assertRaises(urllib.error.HTTPError) as binding_rejected:
                urllib.request.urlopen(invalid_binding)
            self.assertEqual(403, binding_rejected.exception.code)
            self.assertEqual("application/json", binding_rejected.exception.headers.get_content_type())
            self.assertIsNone(binding_rejected.exception.headers.get("X-CSRF-Token"))
            self.assertIsNone(binding_rejected.exception.headers.get("Set-Cookie"))
            self.assertIsNone(binding_rejected.exception.headers.get("Access-Control-Allow-Origin"))
            binding_error = json.load(binding_rejected.exception)
            self.assertEqual(
                ("error_response", "unsafe_binding", "request_binding_rejected"),
                (binding_error["contract_name"], binding_error["error"]["category"], binding_error["error"]["code"]),
            )
            binding_rejected.exception.close()
        invalid_head = urllib.request.Request(
            base + "/api/v1/provider/readiness",
            headers={"Origin": "http://invalid.example"}, method="HEAD",
        )
        with self.assertRaises(urllib.error.HTTPError) as head_rejected:
            urllib.request.urlopen(invalid_head)
        self.assertEqual(403, head_rejected.exception.code)
        self.assertIsNone(head_rejected.exception.headers.get("X-CSRF-Token"))
        self.assertIsNone(head_rejected.exception.headers.get("Set-Cookie"))
        self.assertEqual(b"", head_rejected.exception.read())
        head_rejected.exception.close()
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_route", "idem_route"),
                   "input": {"campaign_id": "campaign_route", "campaign_name": "Route Campaign", "adapter_id": "mothership"}}
        body = json.dumps(self.bind(payload)).encode()
        missing_csrf = urllib.request.Request(
            base + "/api/v1/campaigns", data=body,
            headers={"Content-Type": "application/json", "Cookie": cookie}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(missing_csrf)
        self.assertEqual(403, rejected.exception.code)
        self.assertEqual("application/json", rejected.exception.headers.get_content_type())
        self.assertIsNone(rejected.exception.headers.get("Access-Control-Allow-Origin"))
        self.assertIsNone(rejected.exception.headers.get("X-CSRF-Token"))
        self.assertIsNone(rejected.exception.headers.get("Set-Cookie"))
        csrf_error = json.load(rejected.exception)
        self.assertEqual(
            ("error_response", "unsafe_binding", "csrf_binding_rejected"),
            (csrf_error["contract_name"], csrf_error["error"]["category"], csrf_error["error"]["code"]),
        )
        self.assertNotIn(csrf, repr(csrf_error))
        rejected.exception.close()
        self.assertEqual({}, self.app.campaigns)
        request = urllib.request.Request(base + "/api/v1/campaigns", data=body,
                                         headers={"Content-Type": "application/json", "X-CSRF-Token": csrf, "Cookie": cookie}, method="POST")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(201, response.status)
            campaign = json.load(response)
        record_url = f"{base}/api/v1/campaigns/campaign_route/revisions/{campaign['head_revision']}/records/campaign-main"
        record = json.load(urllib.request.urlopen(record_url))
        self.assertEqual(("campaign_route", campaign["head_revision"]), (record["campaign_id"], record["revision_id"]))


if __name__ == "__main__":
    unittest.main()
