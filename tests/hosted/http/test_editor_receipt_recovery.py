from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock

from tests.hosted.http import test_editor_backend as backend
from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.http.editor import document_digest


class EditorReceiptRecoveryTests(unittest.TestCase):
    setUp = backend.EditorBackendTests.setUp
    _campaign = backend.EditorBackendTests._campaign
    _edit = backend.EditorBackendTests._edit
    _create_record = backend.EditorBackendTests._create_record
    _approve_editor = backend.EditorBackendTests._approve_editor
    _editor_approval_payload = backend.EditorBackendTests._editor_approval_payload

    def _restart(self):
        self.app = SliceApplication(
            Path(self.tmp.name), provider=SyntheticProvider(),
            receipts=self.receipts, workflow_repository=self.workflow,
            proposal_repository=self.app.proposal_repository,
        )

    def _request(self, kind):
        if kind == "edit":
            with mock.patch.object(self.app, "editor_record_edit", return_value=(201, {})) as call:
                self._edit()
            return "editor_record_edit", call.call_args.args
        if kind == "create":
            with mock.patch.object(self.app, "editor_record_create", return_value=(201, {})) as call, mock.patch.object(self, "_approve_editor"):
                self._create_record("npc-recovery")
            return "editor_record_create", call.call_args.args
        if kind == "remove":
            revision = self._create_record("npc-recovery")
            impact = self.app.editor_removal_impact("campaign_alpha", revision, "npc-recovery")[1]
            payload = {
                "contract_name": "editor_record_remove_request", "contract_version": 1,
                "binding": impact["binding"], "impact_digest": impact["impact_digest"],
                "impact_binding": {"binding": impact["binding"], "impact_digest": impact["impact_digest"]},
                "resolutions": [],
            }
            subject = "npc-recovery"
            method = "editor_record_remove"
            args = ("campaign_alpha", revision, subject)
        else:
            revision, _, (_, proposal) = self._edit()
            subject = proposal["proposal_id"]
            method = "editor_proposal_" + kind
            args = (subject, proposal["proposal_version"])
            if kind == "correct":
                candidate = deepcopy(proposal["diff"]["cards"][0]["after"])
                candidate["displayed_name"] = "Corrected Campaign"
                candidate["content_digest"] = document_digest(candidate)
                payload = {
                    "contract_name": "editor_proposal_correction_request", "contract_version": 1,
                    "prior_proposal": {"proposal_id": subject, "proposal_version": proposal["proposal_version"]},
                    "binding": proposal["record_bindings"][0], "mutation_kind": "edit",
                    "candidate": candidate, "resolutions": [], "impact_digest": None, "impact_binding": None,
                }
            else:
                payload = self._editor_approval_payload(proposal)
                for key in ("diff", "affected_record_count", "confirmed_change_ids", "confirmed_authority_change_ids", "confirmed_visibility_change_ids"):
                    payload.pop(key)
                payload["contract_name"] = "editor_proposal_rejection_request"
                payload["reason_code"] = "warden_rejected"
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_recovery", "operation": method,
            "idempotency_key": "idem_recovery", "expected_revision": revision,
            "expected_editor_workflow_version": self.app._editor_version("campaign_alpha"),
            "subject_id": subject,
        }
        if kind == "reject":
            operation["intent_digest"] = payload["diff_digest"]
        payload["operation_request"] = operation
        operation["payload_digest"] = self.app._editor_payload_digest(payload)
        return method, (*args, payload)

    def test_all_mutations_recover_exact_response_after_receipt_crash(self):
        for kind in ("create", "edit", "remove", "correct", "reject"):
            with self.subTest(kind=kind):
                self.setUp()
                method, args = self._request(kind)
                expected_workflow = self.app._editor_version("campaign_alpha") + 1
                captured = []

                def crash(operation, key, digest, status, response):
                    captured.append(deepcopy(response))
                    raise SystemExit("receipt crash")

                with mock.patch.object(self.app, "_store", side_effect=crash):
                    with self.assertRaises(SystemExit):
                        getattr(self.app, method)(*deepcopy(args))
                head = self.app.workflow.head("campaign_alpha")
                audit = tuple(self.app.proposal_repository.audit)
                self._restart()
                self._restart()
                for _ in range(2):
                    self.assertEqual((200, captured[0]), getattr(self.app, method)(*deepcopy(args)))
                    self.assertEqual(expected_workflow, self.app._editor_version("campaign_alpha"))
                    self.assertEqual(head, self.app.workflow.head("campaign_alpha"))
                    self.assertEqual(audit, tuple(self.app.proposal_repository.audit))

    def test_uncommitted_claim_can_retry_without_relaxing_workflow(self):
        method, args = self._request("edit")
        with mock.patch.object(self.app.proposal_repository, "add_editor", side_effect=SystemExit("before commit")):
            with self.assertRaises(SystemExit):
                getattr(self.app, method)(*args)
        self._restart()
        self.assertEqual(201, getattr(self.app, method)(*args)[0])
        stale = deepcopy(args)
        stale[-1]["operation_request"]["idempotency_key"] = "idem_new_stale"
        with self.assertRaises(HTTPFailure) as failure:
            getattr(self.app, method)(*stale)
        self.assertEqual("workflow_conflict", failure.exception.payload["error"]["code"])

    def test_changed_abandoned_payload_is_rejected(self):
        method, args = self._request("edit")
        with mock.patch.object(self.app, "_store", side_effect=SystemExit("receipt crash")):
            with self.assertRaises(SystemExit):
                getattr(self.app, method)(*args)
        self._restart()
        changed = deepcopy(args)
        changed[-1]["candidate"]["displayed_name"] = "Different"
        changed[-1]["candidate"]["content_digest"] = document_digest(changed[-1]["candidate"])
        changed[-1]["operation_request"]["payload_digest"] = self.app._editor_payload_digest(changed[-1])
        with self.assertRaises(HTTPFailure) as failure:
            getattr(self.app, method)(*changed)
        self.assertEqual("idempotency_digest_conflict", failure.exception.payload["error"]["code"])
        self.assertEqual(200, getattr(self.app, method)(*args)[0])

    def test_proposal_commit_recovers_before_sidecar_and_after_later_approval(self):
        for kind in ("create", "edit", "remove", "correct"):
            with self.subTest(kind=kind):
                self.setUp()
                method, args = self._request(kind)
                committed = []
                add = self.app.proposal_repository.add_editor

                def commit_then_crash(item, campaign_id, expected):
                    self.assertTrue(add(item, campaign_id, expected))
                    committed.append(deepcopy(item.editor_metadata))
                    raise SystemExit("after proposal commit")

                with mock.patch.object(self.app.proposal_repository, "add_editor", side_effect=commit_then_crash):
                    with self.assertRaises(SystemExit):
                        getattr(self.app, method)(*args)
                self._restart()
                self._approve_editor(committed[0])
                self._restart()
                head = self.app.workflow.head("campaign_alpha")
                workflow = self.app._editor_version("campaign_alpha")
                audit = tuple(self.app.proposal_repository.audit)
                self.assertEqual((200, committed[0]), getattr(self.app, method)(*args))
                self.assertEqual(head, self.app.workflow.head("campaign_alpha"))
                self.assertEqual(workflow, self.app._editor_version("campaign_alpha"))
                self.assertEqual(audit, tuple(self.app.proposal_repository.audit))

    def test_rejection_recovers_each_fallback_commit_window(self):
        for boundary in ("reject", "advance_editor", "save_editor_metadata"):
            with self.subTest(boundary=boundary):
                self.setUp()
                method, args = self._request("reject")
                owner = self.app.proposals if boundary == "reject" else self.app.proposal_repository
                original = getattr(owner, boundary)

                def commit_then_crash(*positional, **keywords):
                    original(*positional, **keywords)
                    raise SystemExit("after " + boundary)

                with mock.patch.object(owner, boundary, side_effect=commit_then_crash):
                    with self.assertRaises(SystemExit):
                        getattr(self.app, method)(*args)
                self._restart()
                status, result = getattr(self.app, method)(*args)
                self.assertEqual((200, "rejected", 3), (status, result["outcome"], result["editor_workflow_version"]))
                self.assertEqual(3, self.app._editor_version("campaign_alpha"))
                self.assertEqual("rejected", self.app.editor_proposal_read(*args[:2])[1]["core_proposal"]["proposal"]["status"])
                audit = tuple(self.app.proposal_repository.audit)
                self._restart()
                self.assertEqual((status, result), getattr(self.app, method)(*args))
                self.assertEqual(audit, tuple(self.app.proposal_repository.audit))
