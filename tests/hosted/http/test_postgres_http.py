from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid

from warden_drydock.hosted.ai.repository import PostgresAIRepository
from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.repository import PostgresHTTPRepository, ReceiptConflict
from warden_drydock.hosted.proposals import PostgresProposalRepository
from warden_drydock.hosted.projections import PostgresAtlasProjectionRepository
from warden_drydock.hosted.revisions import PostgresWorkflowRepository


DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL HTTP receipt test is opt-in")
class PostgresHTTPReceiptIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = "idem_" + uuid.uuid4().hex[:20]
        self.connect = lambda: psycopg.connect(DATABASE_URL)
        self.repository = PostgresHTTPRepository(self.connect)

    def tearDown(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_http_operation_receipt WHERE idempotency_key=%s", (self.key,))

    def test_proposal_create_receipt_survives_repository_restart(self) -> None:
        response = {"contract_name": "proposal_view", "contract_version": 2}
        self.repository.store("proposal_create", self.key, "a" * 64, 201, response)
        restarted = PostgresHTTPRepository(self.connect)
        self.assertEqual((201, response), restarted.replay("proposal_create", self.key, "a" * 64))
        with self.assertRaises(ReceiptConflict):
            restarted.replay("proposal_create", self.key, "b" * 64)

    def test_missing_postgres_proposal_maps_to_contract_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = SliceApplication(
                Path(root), provider=SyntheticProvider(),
                proposal_repository=PostgresProposalRepository(self.connect),
            )
            with self.assertRaises(HTTPFailure) as caught:
                app.proposal_view("proposal_missing", 1)
        self.assertEqual(
            (404, "not_found", "proposal_not_found"),
            (caught.exception.status, caught.exception.payload["error"]["category"], caught.exception.payload["error"]["code"]),
        )

    def test_full_slice_persists_publication_and_replays_after_restart(self) -> None:
        suffix = uuid.uuid4().hex[:16]
        campaign_id, generation_id, proposal_id = (
            f"campaign_{suffix}", f"generation_{suffix}", f"proposal_{suffix}",
        )
        keys = [f"idem_{name}_{suffix}" for name in ("consent", "campaign", "proposal", "approval")]
        operation_ids = [f"request_{name}_{suffix}" for name in ("consent", "campaign", "proposal", "approval")]

        def operation(name, request_id, key, **extra):
            value = {"contract_name": "operation_request", "contract_version": 2,
                     "request_id": request_id, "operation": name, "idempotency_key": key,
                     "payload_digest": "0" * 64, "expected_revision": None,
                     "expected_workflow_version": None}
            value.update(extra)
            return value

        def bind(payload):
            target = payload.get("operation_request", payload)
            target["payload_digest"] = canonical_digest(request_digest_input(payload))
            return payload

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            snapshots = Path(directory) / "snapshots"
            receipts = PostgresHTTPRepository(self.connect)
            proposals = PostgresProposalRepository(self.connect)
            workflow = PostgresWorkflowRepository(self.connect)
            ai = PostgresAIRepository(self.connect)
            atlas = PostgresAtlasProjectionRepository(self.connect)
            app = SliceApplication(root, snapshot_root=snapshots, provider=SyntheticProvider(),
                                   receipts=receipts, proposal_repository=proposals,
                                   workflow_repository=workflow, ai_repository=ai,
                                   atlas_repository=atlas)
            readiness = app.provider_readiness()[1]
            consent = bind({"contract_name": "provider_consent_request", "contract_version": 2,
                "operation_request": operation("provider_consent", operation_ids[0], keys[0]),
                "input": {"explicit": True, "consent_identity_digest": readiness["consent_identity_digest"]}})
            app.provider_consent(consent)
            campaign = bind({"contract_name": "campaign_create_request", "contract_version": 2,
                "operation_request": operation("campaign_create", operation_ids[1], keys[1]),
                "input": {"campaign_id": campaign_id, "campaign_name": "PostgreSQL Slice", "adapter_id": "mothership"}})
            revision = app.create_campaign(campaign)[1]["head_revision"]
            bundle = atlas.get(campaign_id, revision)
            record = app.atlas_record_detail(
                campaign_id, "campaign-main", revision, bundle.ordinal, bundle.tree_digest
            )[1]["record"]
            ask = {"contract_name": "generation_start_request", "contract_version": 2,
                   "generation_id": generation_id, "campaign_id": campaign_id,
                   "source_revision": revision, "action": "ask", "prompt": "What is the campaign called?",
                   "context": {"scope": "record", "record_id": "campaign-main", "content_digest": record["content_digest"]}}
            app.start_generation(campaign_id, revision, ask)
            app.dispatch_generation(generation_id)
            generation = app.generation_view(generation_id)[1]
            proposal_request = bind({"contract_name": "proposal_create_request", "contract_version": 2,
                "request_id": operation_ids[2], "idempotency_key": keys[2], "payload_digest": "0" * 64,
                "generation_id": generation_id, "proposal_id": proposal_id, "campaign_id": campaign_id,
                "source_revision": revision, "base_revision": revision,
                "source_set_digest": generation["source_set_digest"],
                "terminal_draft_digest": generation["terminal_content_digest"], "subject_id": "campaign-main"})
            proposal = app.create_proposal(generation_id, proposal_request)[1]
            approval = bind({"contract_name": "proposal_approval_request", "contract_version": 2,
                "operation_request": operation("proposal_approve", operation_ids[3], keys[3],
                    expected_revision=revision, subject_id=proposal_id, intent_digest=proposal["diff_digest"]),
                "proposal_id": proposal_id, "proposal_version": 1, "source_revision": revision,
                "base_revision": revision, "expected_campaign_head": revision,
                "diff_digest": proposal["diff_digest"],
                "proposal_payload_digest": proposal["proposal_payload_digest"], "warden_confirmed": True})
            result = app.approve_proposal(proposal_id, 1, approval)[1]
            published = result["published_revision"]["revision_id"]
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT revision_id FROM hosted_campaign_head WHERE campaign_id=%s", (campaign_id,))
                self.assertEqual(published, cursor.fetchone()[0])
                cursor.execute("SELECT published_revision_id FROM hosted_proposal_version WHERE proposal_id=%s AND version=1", (proposal_id,))
                self.assertEqual(published, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s AND event='published'", (proposal_id,))
                self.assertEqual(1, cursor.fetchone()[0])
            self.assertEqual(2, len(app.revisions.store.inventory()))
            restarted = SliceApplication(root, snapshot_root=snapshots, provider=SyntheticProvider(),
                receipts=PostgresHTTPRepository(self.connect), proposal_repository=PostgresProposalRepository(self.connect),
                workflow_repository=PostgresWorkflowRepository(self.connect), ai_repository=PostgresAIRepository(self.connect),
                atlas_repository=PostgresAtlasProjectionRepository(self.connect))
            readback = restarted.generation_view(generation_id)[1]
            self.assertEqual(ask["context"], readback["context"])
            generation_replay = restarted.start_generation(campaign_id, revision, ask)
            self.assertEqual((200, False, ask["context"]), (
                generation_replay[0], generation_replay[2], generation_replay[1]["context"],
            ))
            replay = restarted.approve_proposal(proposal_id, 1, approval)[1]
            self.assertEqual((published, True), (replay["published_revision"]["revision_id"], replay["exact_replay"]))

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_atlas_projection_checkpoint WHERE campaign_id=%s", (campaign_id,))
            cursor.execute("DELETE FROM hosted_http_operation_receipt WHERE idempotency_key=ANY(%s)", (keys,))
            cursor.execute("DELETE FROM hosted_proposal_audit WHERE proposal_id=%s", (proposal_id,))
            cursor.execute("DELETE FROM hosted_proposal_version WHERE proposal_id=%s", (proposal_id,))
            cursor.execute("DELETE FROM hosted_ai_stream_event WHERE generation_id=%s", (generation_id,))
            cursor.execute("DELETE FROM hosted_ai_generation WHERE generation_id=%s", (generation_id,))
            cursor.execute("DELETE FROM hosted_campaign_head WHERE campaign_id=%s", (campaign_id,))
            cursor.execute("DELETE FROM hosted_publication_intent WHERE campaign_id=%s", (campaign_id,))


if __name__ == "__main__":
    unittest.main()
