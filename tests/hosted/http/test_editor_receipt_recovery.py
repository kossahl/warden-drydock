from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from tests.hosted.http import test_editor_backend as backend
from warden_drydock.hosted.engine.models import ChangeKind, ExactTextChange, exact_diff_digest
from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.repository import PostgresHTTPRepository
from warden_drydock.hosted.http.editor import document_digest
from warden_drydock.hosted.proposals import PostgresProposalRepository
from warden_drydock.hosted.proposals.service import ProposalStatus, _payload_digest
from warden_drydock.hosted.projections import PostgresAtlasProjectionRepository
from warden_drydock.hosted.revisions import (
    IntentStatus, PostgresWorkflowRepository, PublicationIntent,
    PublicationIntentError, PublicationKind,
)


DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None


class EditorReceiptRecoveryTests(unittest.TestCase):
    setUp = backend.EditorBackendTests.setUp
    _campaign = backend.EditorBackendTests._campaign
    _edit = backend.EditorBackendTests._edit
    _create_record_proposal = backend.EditorBackendTests._create_record_proposal
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
                if kind == "reject":
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
        if kind in {"approve", "reject"}:
            operation["intent_digest"] = payload["diff_digest"]
        payload["operation_request"] = operation
        operation["payload_digest"] = self.app._editor_payload_digest(payload)
        return method, (*args, payload)

    def test_all_mutations_recover_exact_response_after_receipt_crash(self):
        for kind in ("create", "edit", "remove", "correct", "approve", "reject"):
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

    def test_noop_editor_proposal_rejects_before_claim_and_exact_retry_repeats_validation(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_noop", "operation": "editor_record_edit",
            "idempotency_key": "idem_noop", "payload_digest": "0" * 64,
            "expected_revision": revision, "expected_editor_workflow_version": 1,
            "subject_id": "campaign-main",
        }
        payload = {
            "contract_name": "editor_record_edit_request", "contract_version": 1,
            "operation_request": operation,
            "binding": {
                "campaign_id": "campaign_alpha", "base_revision": view["viewed_revision"],
                "record_id": "campaign-main", "record_digest": view["record"]["content_digest"],
                "expected_editor_workflow_version": 1,
            },
            "candidate": deepcopy(view["record"]),
        }
        operation["payload_digest"] = self.app._editor_payload_digest(payload)

        for _ in range(2):
            with self.assertRaises(HTTPFailure) as failure:
                self.app.editor_record_edit("campaign_alpha", revision, "campaign-main", deepcopy(payload))
            self.assertEqual("proposal_validation_failure", failure.exception.payload["error"]["category"])
        self.assertEqual(1, self.app._editor_version("campaign_alpha"))

    def test_editor_proposal_releases_claim_when_atomic_cas_does_not_commit(self):
        for failure in (False, RuntimeError("before commit")):
            with self.subTest(failure=type(failure).__name__):
                self.setUp()
                method, args = self._request("edit")
                add = self.app.proposal_repository.add_editor
                calls = 0

                def fail_once(*positional, **keywords):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if isinstance(failure, BaseException):
                            raise failure
                        return failure
                    return add(*positional, **keywords)

                with mock.patch.object(self.app.proposal_repository, "add_editor", side_effect=fail_once):
                    if failure is False:
                        with self.assertRaises(HTTPFailure):
                            getattr(self.app, method)(*deepcopy(args))
                    else:
                        with self.assertRaises(RuntimeError):
                            getattr(self.app, method)(*deepcopy(args))
                    status, result = getattr(self.app, method)(*deepcopy(args))
                self.assertEqual((201, "edit"), (status, result["diff"]["summary"]))

    def test_approval_retries_in_same_process_after_staging_failure(self):
        _, _, (_, proposal) = self._edit()
        payload = self._editor_approval_payload(proposal)
        args = (proposal["proposal_id"], proposal["proposal_version"], payload)
        with mock.patch.object(self.app.proposals, "_stage", side_effect=RuntimeError("staging failed")):
            with self.assertRaises(HTTPFailure) as failure:
                self.app.editor_proposal_approve(*args)
        self.assertEqual("proposal_validation_failure", failure.exception.payload["error"]["code"])
        status, result = self.app.editor_proposal_approve(*args)
        self.assertEqual((200, "published"), (status, result["outcome"]))
        self.assertEqual((status, result), self.app.editor_proposal_approve(*args))

    def test_approval_claim_recovers_after_process_dies_before_publication_snapshot(self):
        _, _, (_, proposal) = self._edit("idem_editor_claim_crash")
        payload = self._editor_approval_payload(proposal)
        args = (proposal["proposal_id"], proposal["proposal_version"], payload)

        with mock.patch.object(self.app.proposals, "_stage", side_effect=SystemExit("claim crash")):
            with self.assertRaises(SystemExit):
                self.app.editor_proposal_approve(*args)

        self.assertEqual(
            ProposalStatus.APPROVING,
            self.app.proposal_repository.get(proposal["proposal_id"], proposal["proposal_version"]).status,
        )
        self.assertEqual(1, len(self.app.revisions.store.inventory()))

        self._restart()
        self.assertEqual(
            ProposalStatus.APPROVING,
            self.app.proposal_repository.get(proposal["proposal_id"], proposal["proposal_version"]).status,
        )
        status, result = self.app.editor_proposal_approve(*args)
        self.assertEqual((200, "published"), (status, result["outcome"]))
        self.assertEqual(
            ProposalStatus.PUBLISHED,
            self.app.proposal_repository.get(proposal["proposal_id"], proposal["proposal_version"]).status,
        )

    def test_rejection_releases_claim_when_atomic_finalization_does_not_commit(self):
        for failure in (False, RuntimeError("finalization failed")):
            with self.subTest(failure=type(failure).__name__):
                self.setUp()
                method, args = self._request("reject")
                calls = 0

                def fail_once(*positional, **keywords):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        if isinstance(failure, BaseException):
                            raise failure
                        return failure
                    return True

                with mock.patch.object(self.app.workflow, "finalize_editor_rejection", create=True, side_effect=fail_once):
                    if failure is False:
                        with self.assertRaises(HTTPFailure):
                            getattr(self.app, method)(*deepcopy(args))
                    else:
                        with self.assertRaises(RuntimeError):
                            getattr(self.app, method)(*deepcopy(args))
                    status, result = getattr(self.app, method)(*deepcopy(args))
                self.assertEqual((200, "rejected"), (status, result["outcome"]))

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


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL editor recovery test is opt-in")
class PostgresEditorReceiptRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:16]
        self.connect = lambda: psycopg.connect(DATABASE_URL)
        self.repository = PostgresWorkflowRepository(self.connect)
        self.campaign_id = "campaign_" + suffix
        self.intent_id = "intent_" + suffix
        self.intent_token = "token_" + suffix
        self.parent_revision = "revision_parent_" + suffix
        self.revision_id = "revision_published_" + suffix
        self.proposal_id = "proposal_" + suffix
        self.campaign_key = "idem_campaign_" + suffix
        self.edit_key = "idem_edit_" + suffix
        self.reject_key = "idem_reject_" + suffix
        self.changes = (
            ExactTextChange(
                "change_" + suffix, "record_" + suffix, "d" * 64,
                "# Updated", ChangeKind.UPDATE, "npc",
            ),
        )
        self.change_digest = exact_diff_digest(self.changes)
        self.diff_digest = "a" * 64
        self.payload_digest = _payload_digest(self.changes)
        self.tree_digest = "c" * 64
        self.expected_workflow = 3

    def tearDown(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hosted_http_operation_receipt WHERE idempotency_key IN (%s,%s,%s)",
                (self.campaign_key, self.edit_key, self.reject_key),
            )
            cursor.execute(
                "DELETE FROM hosted_atlas_projection_checkpoint WHERE campaign_id=%s",
                (self.campaign_id,),
            )
            cursor.execute(
                "DELETE FROM hosted_proposal_audit WHERE proposal_id IN "
                "(SELECT proposal_id FROM hosted_proposal_version WHERE campaign_id=%s) "
                "OR proposal_id=%s",
                (self.campaign_id, self.proposal_id),
            )
            cursor.execute(
                "DELETE FROM hosted_proposal_version WHERE campaign_id=%s OR proposal_id=%s",
                (self.campaign_id, self.proposal_id),
            )
            cursor.execute("DELETE FROM hosted_editor_workflow WHERE campaign_id=%s", (self.campaign_id,))
            cursor.execute("DELETE FROM hosted_campaign_head WHERE campaign_id=%s", (self.campaign_id,))
            cursor.execute(
                "DELETE FROM hosted_publication_intent WHERE campaign_id=%s OR intent_id=%s",
                (self.campaign_id, self.intent_id),
            )

    def _app(self, directory):
        root = Path(directory) / "runtime"
        snapshots = Path(directory) / "snapshots"
        return SliceApplication(
            root, snapshot_root=snapshots, provider=SyntheticProvider(),
            receipts=PostgresHTTPRepository(self.connect),
            proposal_repository=PostgresProposalRepository(self.connect),
            workflow_repository=PostgresWorkflowRepository(self.connect),
            atlas_repository=PostgresAtlasProjectionRepository(self.connect),
        )

    def _campaign(self, app) -> str:
        payload = {
            "contract_name": "campaign_create_request", "contract_version": 2,
            "operation_request": {
                "contract_name": "operation_request", "contract_version": 2,
                "request_id": "request_campaign_" + self.campaign_id,
                "operation": "campaign_create", "idempotency_key": self.campaign_key,
                "payload_digest": "0" * 64, "expected_revision": None,
                "expected_workflow_version": None,
            },
            "input": {
                "campaign_id": self.campaign_id,
                "campaign_name": "Editor recovery",
                "adapter_id": "mothership",
            },
        }
        payload["operation_request"]["payload_digest"] = canonical_digest(
            request_digest_input(payload)
        )
        return app.create_campaign(payload)[1]["head_revision"]

    def _rejection_request(self, app, revision: str):
        revision = app.workflow.head(self.campaign_id)
        viewed = app.editor_record_read(
            self.campaign_id, revision, "campaign-main",
        )[1]
        candidate = deepcopy(viewed["record"])
        candidate["displayed_name"] = "Edited Campaign"
        candidate["content_digest"] = document_digest(candidate)
        workflow = app._editor_version(self.campaign_id)
        edit_operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_edit_" + self.campaign_id,
            "operation": "editor_record_edit", "idempotency_key": self.edit_key,
            "payload_digest": "0" * 64, "expected_revision": revision,
            "expected_editor_workflow_version": workflow,
            "subject_id": "campaign-main",
        }
        edit = {
            "contract_name": "editor_record_edit_request", "contract_version": 1,
            "operation_request": edit_operation,
            "binding": {
                "campaign_id": self.campaign_id,
                "base_revision": viewed["viewed_revision"],
                "record_id": "campaign-main",
                "record_digest": viewed["record"]["content_digest"],
                "expected_editor_workflow_version": workflow,
            },
            "candidate": candidate,
        }
        edit_operation["payload_digest"] = canonical_digest(request_digest_input(edit))
        _, proposal = app.editor_record_edit(
            self.campaign_id, revision, "campaign-main", edit,
        )
        payload = backend.EditorBackendTests._editor_approval_payload(self, proposal)
        for key in (
            "diff", "affected_record_count", "confirmed_change_ids",
            "confirmed_authority_change_ids", "confirmed_visibility_change_ids",
        ):
            payload.pop(key)
        payload["contract_name"] = "editor_proposal_rejection_request"
        payload["reason_code"] = "warden_rejected"
        payload["operation_request"] = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": "request_reject_" + self.campaign_id,
            "operation": "editor_proposal_reject", "idempotency_key": self.reject_key,
            "expected_revision": revision,
            "expected_editor_workflow_version": app._editor_version(self.campaign_id),
            "subject_id": proposal["proposal_id"], "intent_digest": payload["diff_digest"],
            "payload_digest": "0" * 64,
        }
        payload["operation_request"]["payload_digest"] = app._editor_payload_digest(payload)
        return proposal, payload

    def test_editor_rejection_recovers_after_receipt_storage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = self._app(directory)
            self.app = app
            revision = self._campaign(app)
            proposal, payload = self._rejection_request(app, revision)

            with mock.patch.object(app, "_store", side_effect=SystemExit("receipt storage failed")):
                with self.assertRaises(SystemExit):
                    app.editor_proposal_reject(
                        proposal["proposal_id"], proposal["proposal_version"], payload,
                    )

            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status,editor_metadata FROM hosted_proposal_version "
                    "WHERE proposal_id=%s AND version=1",
                    (proposal["proposal_id"],),
                )
                stored_status, stored_metadata = cursor.fetchone()
                self.assertEqual("rejected", stored_status)
                self.assertEqual("editor_proposal_view", stored_metadata["contract_name"])
                self.assertEqual("rejected", stored_metadata["core_proposal"]["proposal"]["status"])
                self.assertEqual(3, stored_metadata["editor_workflow_version"])
                cursor.execute(
                    "SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s",
                    (self.campaign_id,),
                )
                self.assertEqual((3,), cursor.fetchone())
                cursor.execute(
                    "SELECT status,event FROM hosted_proposal_audit "
                    "WHERE proposal_id=%s AND event='rejected'",
                    (proposal["proposal_id"],),
                )
                self.assertEqual([("rejected", "rejected")], cursor.fetchall())
                cursor.execute(
                    "SELECT state,http_status,response_body FROM hosted_http_operation_receipt "
                    "WHERE operation='editor_proposal_reject' AND idempotency_key=%s",
                    (self.reject_key,),
                )
                self.assertEqual(("pending", None, None), cursor.fetchone())

            restarted = self._app(directory)
            expected_response = {
                "contract_name": "editor_proposal_rejection_result",
                "contract_version": 1,
                "proposal": {
                    "proposal_id": proposal["proposal_id"],
                    "proposal_version": proposal["proposal_version"],
                },
                "outcome": "rejected",
                "editor_workflow_version": 3,
            }
            self.assertEqual(
                (200, expected_response),
                restarted.editor_proposal_reject(
                    proposal["proposal_id"], proposal["proposal_version"], deepcopy(payload),
                ),
            )
            self.assertEqual(
                (200, expected_response),
                restarted.editor_proposal_reject(
                    proposal["proposal_id"], proposal["proposal_version"], deepcopy(payload),
                ),
            )

            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status,editor_metadata FROM hosted_proposal_version "
                    "WHERE proposal_id=%s AND version=1",
                    (proposal["proposal_id"],),
                )
                stored_status, stored_metadata = cursor.fetchone()
                self.assertEqual("rejected", stored_status)
                self.assertEqual(3, stored_metadata["editor_workflow_version"])
                cursor.execute(
                    "SELECT count(*) FROM hosted_proposal_audit "
                    "WHERE proposal_id=%s AND event='rejected'",
                    (proposal["proposal_id"],),
                )
                self.assertEqual((1,), cursor.fetchone())
                cursor.execute(
                    "SELECT state,http_status,response_body FROM hosted_http_operation_receipt "
                    "WHERE operation='editor_proposal_reject' AND idempotency_key=%s",
                    (self.reject_key,),
                )
                receipt = cursor.fetchone()
                self.assertEqual("completed", receipt[0])
                self.assertEqual(200, receipt[1])
                self.assertEqual(expected_response, receipt[2])

    def _seed(self, *, intent_status=None, proposal_status="approving",
              head_revision=None, proposal_base=None) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hosted_editor_workflow(campaign_id,version) VALUES(%s,%s)",
                (self.campaign_id, self.expected_workflow),
            )
            if head_revision is not None:
                cursor.execute(
                    "INSERT INTO hosted_campaign_head(campaign_id,revision_id,ordinal) VALUES(%s,%s,%s)",
                    (self.campaign_id, head_revision, 8),
                )
            if intent_status is not None:
                cursor.execute(
                    "INSERT INTO hosted_publication_intent(intent_id,intent_token,kind,campaign_id,revision_id,parent_revision,ordinal,tree_digest,change_digest,status) VALUES(%s,%s,'approval',%s,%s,%s,8,%s,%s,%s)",
                    (self.intent_id, self.intent_token, self.campaign_id, self.revision_id,
                     self.parent_revision, self.tree_digest, self.change_digest, intent_status.value),
                )
            metadata = self._editor_metadata()
            changes = json.dumps([
                {
                    "change_id": change.change_id,
                    "subject_id": change.subject_id,
                    "expected_content_digest": change.expected_content_digest,
                    "replacement": change.replacement,
                    "change_kind": change.change_kind.value,
                    "record_type": change.record_type,
                }
                for change in self.changes
            ])
            cursor.execute(
                "INSERT INTO hosted_proposal_version(proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,editor_metadata) VALUES(%s,1,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)",
                (self.proposal_id, self.campaign_id, proposal_base or self.parent_revision,
                 changes, self.diff_digest, self.payload_digest, proposal_status, json.dumps(metadata)),
            )

    def _editor_metadata(self, status="needs_review") -> dict:
        return {
            "contract_name": "editor_proposal_view",
            "proposal_id": self.proposal_id,
            "proposal_version": 1,
            "campaign_id": self.campaign_id,
            "status": status,
        }

    def _intent(self, status=IntentStatus.FINALIZED, kind=PublicationKind.APPROVAL) -> PublicationIntent:
        return PublicationIntent(
            self.intent_id, self.intent_token, kind,
            self.campaign_id, self.revision_id, self.parent_revision, 8,
            self.tree_digest, self.change_digest, status,
        )

    def test_finalized_publication_recovery_repairs_editor_rows_once(self) -> None:
        self._seed(
            intent_status=IntentStatus.FINALIZED,
            head_revision=self.revision_id,
        )
        terminal_metadata = self._editor_metadata("published")

        self.assertTrue(self.repository.finalize_editor_publication(
            self._intent(), self.proposal_id, 1, self.expected_workflow, terminal_metadata,
        ))
        self.assertFalse(self.repository.finalize_editor_publication(
            self._intent(), self.proposal_id, 1, self.expected_workflow, terminal_metadata,
        ))

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,publication_intent_token,published_revision_id,result_digest,editor_metadata FROM hosted_proposal_version WHERE proposal_id=%s",
                (self.proposal_id,),
            )
            self.assertEqual(
                ("published", self.intent_token, self.revision_id, "c" * 64, terminal_metadata),
                cursor.fetchone(),
            )
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.expected_workflow + 1,), cursor.fetchone())
            cursor.execute("SELECT revision_id,ordinal FROM hosted_campaign_head WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.revision_id, 8), cursor.fetchone())
            cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s AND event='published'", (self.proposal_id,))
            self.assertEqual((1,), cursor.fetchone())

    def test_editor_publication_requires_approval_intent_and_terminal_binding(self) -> None:
        self._seed(
            intent_status=IntentStatus.FINALIZED,
            head_revision=self.revision_id,
        )

        with self.assertRaises(PublicationIntentError):
            self.repository.finalize_editor_publication(
                self._intent(kind=PublicationKind.CREATION), self.proposal_id, 1,
                self.expected_workflow, self._editor_metadata("published"),
            )
        with self.assertRaises(PublicationIntentError):
            self.repository.finalize_editor_publication(
                self._intent(), self.proposal_id, 1, self.expected_workflow,
                {**self._editor_metadata("published"), "campaign_id": "campaign_other"},
            )

    def test_finalized_publication_recovery_restores_missing_head(self) -> None:
        self._seed(intent_status=IntentStatus.FINALIZED)

        self.assertTrue(self.repository.finalize_editor_publication(
            self._intent(), self.proposal_id, 1, self.expected_workflow,
            self._editor_metadata("published"),
        ))
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT revision_id,ordinal FROM hosted_campaign_head WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.revision_id, 8), cursor.fetchone())

    def test_published_recovery_requires_one_matching_audit_row(self) -> None:
        self._seed(
            intent_status=IntentStatus.FINALIZED,
            head_revision=self.revision_id,
        )
        terminal_metadata = self._editor_metadata("published")
        self.assertTrue(self.repository.finalize_editor_publication(
            self._intent(), self.proposal_id, 1, self.expected_workflow, terminal_metadata,
        ))
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE hosted_proposal_audit SET result_digest=%s WHERE proposal_id=%s AND event='published'",
                ("e" * 64, self.proposal_id),
            )

        with self.assertRaises(PublicationIntentError):
            self.repository.finalize_editor_publication(
                self._intent(), self.proposal_id, 1, self.expected_workflow, terminal_metadata,
            )

    def test_finalized_publication_recovery_rejects_mismatched_head(self) -> None:
        self._seed(
            intent_status=IntentStatus.FINALIZED,
            head_revision="revision_other_" + uuid.uuid4().hex[:16],
        )

        with self.assertRaises(PublicationIntentError):
            self.repository.finalize_editor_publication(
                self._intent(), self.proposal_id, 1, self.expected_workflow,
                self._editor_metadata("published"),
            )
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM hosted_proposal_version WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual(("approving",), cursor.fetchone())
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.expected_workflow,), cursor.fetchone())
            cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual((0,), cursor.fetchone())

    def test_finalized_publication_recovery_rejects_mismatched_proposal(self) -> None:
        self._seed(
            intent_status=IntentStatus.FINALIZED,
            head_revision=self.revision_id,
            proposal_base="revision_other_" + uuid.uuid4().hex[:16],
        )

        with self.assertRaises(PublicationIntentError):
            self.repository.finalize_editor_publication(
                self._intent(), self.proposal_id, 1, self.expected_workflow,
                self._editor_metadata("published"),
            )
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM hosted_proposal_version WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual(("approving",), cursor.fetchone())
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.expected_workflow,), cursor.fetchone())
            cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual((0,), cursor.fetchone())

    def test_editor_rejection_rejects_stale_campaign_head_after_lock(self) -> None:
        self._seed(
            intent_status=None,
            proposal_status="draft",
            head_revision="revision_other_" + uuid.uuid4().hex[:16],
        )

        self.assertFalse(self.repository.finalize_editor_rejection(
            self.campaign_id, self.proposal_id, 1, self.expected_workflow, {},
        ))
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM hosted_proposal_version WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual(("draft",), cursor.fetchone())
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.expected_workflow,), cursor.fetchone())
            cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual((0,), cursor.fetchone())

    def test_editor_rejection_rechecks_head_after_waiting_for_campaign_lock(self) -> None:
        self._seed(
            intent_status=None,
            proposal_status="draft",
            head_revision=self.parent_revision,
        )
        holder = self.connect()
        rejection_ready = threading.Event()
        rejection_pid = {}
        outcome = {}

        def rejection_connect():
            connection = self.connect()
            rejection_pid["pid"] = connection.info.backend_pid
            rejection_ready.set()
            return connection

        race_repository = PostgresWorkflowRepository(rejection_connect)

        def reject():
            try:
                outcome["result"] = race_repository.finalize_editor_rejection(
                    self.campaign_id, self.proposal_id, 1,
                    self.expected_workflow, self._editor_metadata("rejected"),
                )
            except BaseException as error:  # pragma: no cover - preserves thread failure
                outcome["error"] = error

        thread = threading.Thread(target=reject)
        try:
            with holder.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (self.campaign_id,),
                )
            thread.start()
            self.assertTrue(rejection_ready.wait(5), "rejection connection did not start")

            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                with holder.cursor() as cursor:
                    cursor.execute(
                        "SELECT granted FROM pg_locks WHERE pid=%s AND locktype='advisory'",
                        (rejection_pid["pid"],),
                    )
                    waiting = any(not granted for (granted,) in cursor.fetchall())
                if waiting:
                    break
                time.sleep(0.01)
            self.assertTrue(waiting, "rejection did not wait for the campaign advisory lock")

            with holder.cursor() as cursor:
                cursor.execute(
                    "UPDATE hosted_campaign_head SET revision_id=%s,ordinal=%s WHERE campaign_id=%s",
                    (self.revision_id, 8, self.campaign_id),
                )
            holder.commit()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "rejection did not finish after lock release")
            self.assertNotIn("error", outcome)
            self.assertFalse(outcome["result"])
        finally:
            try:
                holder.rollback()
            finally:
                holder.close()
            if thread.is_alive():
                thread.join(5)

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status FROM hosted_proposal_version WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual(("draft",), cursor.fetchone())
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.expected_workflow,), cursor.fetchone())
            cursor.execute("SELECT revision_id FROM hosted_campaign_head WHERE campaign_id=%s", (self.campaign_id,))
            self.assertEqual((self.revision_id,), cursor.fetchone())
            cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (self.proposal_id,))
            self.assertEqual((0,), cursor.fetchone())
