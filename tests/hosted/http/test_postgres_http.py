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
        with self.assertRaisesRegex(ReceiptConflict, "idempotency_digest_conflict"):
            restarted.replay("proposal_create", self.key, "b" * 64)
        self.assertEqual((201, response), restarted.replay("proposal_create", self.key, "a" * 64))

    def test_missing_postgres_proposal_maps_to_contract_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            app = SliceApplication(
                Path(root), provider=SyntheticProvider(),
                proposal_repository=PostgresProposalRepository(self.connect),
            )
            with self.assertRaises(HTTPFailure) as caught:
                app.proposal_view("proposal_missing", 1)
        self.assertEqual(
            (404, "not_found", "proposal_not_found", "proposal_not_found"),
            (caught.exception.status, caught.exception.payload["error"]["category"],
             caught.exception.payload["error"]["code"], str(caught.exception)),
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
            generation_collection = app.atlas_generation_collection(
                campaign_id, revision, bundle.ordinal, bundle.tree_digest,
                actions=("ask",), statuses=("complete",), record_id="campaign-main",
                limit=50, cursor=None,
            )[1]
            proposal_collection = app.atlas_proposal_collection(
                campaign_id, revision, bundle.ordinal, bundle.tree_digest,
                statuses=("draft",), record_id="campaign-main",
                limit=50, cursor=None,
            )[1]
            self.assertEqual([generation_id], [item["generation_id"] for item in generation_collection["items"]])
            self.assertEqual([(proposal_id, 1)], [
                (item["proposal_id"], item["proposal_version"])
                for item in proposal_collection["items"]
            ])
            approval = bind({"contract_name": "proposal_approval_request", "contract_version": 2,
                "operation_request": operation("proposal_approve", operation_ids[3], keys[3],
                    expected_revision=revision, subject_id=proposal_id, intent_digest=proposal["diff_digest"]),
                "proposal_id": proposal_id, "proposal_version": 1, "source_revision": revision,
                "base_revision": revision, "expected_campaign_head": revision,
                "diff_digest": proposal["diff_digest"],
                "proposal_payload_digest": proposal["proposal_payload_digest"], "warden_confirmed": True})
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s AND event='published'",
                    (proposal_id,),
                )
                published_events_before = cursor.fetchone()[0]
            result = app.approve_proposal(proposal_id, 1, approval)[1]
            published = result["published_revision"]["revision_id"]
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT revision_id FROM hosted_campaign_head WHERE campaign_id=%s", (campaign_id,))
                self.assertEqual(published, cursor.fetchone()[0])
                cursor.execute("SELECT published_revision_id FROM hosted_proposal_version WHERE proposal_id=%s AND version=1", (proposal_id,))
                self.assertEqual(published, cursor.fetchone()[0])
                cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s AND event='published'", (proposal_id,))
                self.assertEqual(published_events_before + 1, cursor.fetchone()[0])
            self.assertEqual(2, len(app.revisions.store.inventory()))
            restarted = SliceApplication(root, snapshot_root=snapshots, provider=SyntheticProvider(),
                receipts=PostgresHTTPRepository(self.connect), proposal_repository=PostgresProposalRepository(self.connect),
                workflow_repository=PostgresWorkflowRepository(self.connect), ai_repository=PostgresAIRepository(self.connect),
                atlas_repository=PostgresAtlasProjectionRepository(self.connect))
            readback = restarted.generation_view(generation_id)[1]
            self.assertEqual(ask["context"], readback["context"])
            restarted_bundle = restarted.atlas_repository.get(campaign_id, revision)
            restarted_proposals = restarted.atlas_proposal_collection(
                campaign_id, revision, restarted_bundle.ordinal, restarted_bundle.tree_digest,
                statuses=("published",), record_id=None, limit=50, cursor=None,
            )[1]
            self.assertEqual((proposal_id, published), (
                restarted_proposals["items"][0]["proposal_id"],
                restarted_proposals["items"][0]["published_revision_id"],
            ))
            generation_replay = restarted.start_generation(campaign_id, revision, ask)
            self.assertEqual((200, False, ask["context"]), (
                generation_replay[0], generation_replay[2], generation_replay[1]["context"],
            ))
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (proposal_id,))
                audit_before_replay = cursor.fetchone()[0]
            replay = restarted.approve_proposal(proposal_id, 1, approval)[1]
            self.assertEqual((published, True), (replay["published_revision"]["revision_id"], replay["exact_replay"]))
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (proposal_id,))
                self.assertEqual(audit_before_replay, cursor.fetchone()[0])
            for stored_status in ("approved", "quarantined"):
                with self.connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (proposal_id,))
                    audit_before_override = cursor.fetchone()[0]
                with self.connect() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE hosted_proposal_version SET status=%s WHERE proposal_id=%s AND version=1",
                        (stored_status, proposal_id),
                    )
                read_only = restarted.atlas_proposal_collection(
                    campaign_id, revision, restarted_bundle.ordinal, restarted_bundle.tree_digest,
                    statuses=("published",), record_id=None, limit=50, cursor=None,
                )[1]
                self.assertEqual(("published", published), (
                    read_only["items"][0]["status"],
                    read_only["items"][0]["published_revision_id"],
                ))
                with self.connect() as connection, connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT status,published_revision_id FROM hosted_proposal_version "
                        "WHERE proposal_id=%s AND version=1",
                        (proposal_id,),
                    )
                    self.assertEqual((stored_status, published), cursor.fetchone())
                    cursor.execute("SELECT count(*) FROM hosted_proposal_audit WHERE proposal_id=%s", (proposal_id,))
                    self.assertEqual(audit_before_override, cursor.fetchone()[0])
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE hosted_proposal_version SET status='published' WHERE proposal_id=%s AND version=1",
                    (proposal_id,),
                )
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT request_digest,source_set_digest FROM hosted_ai_generation "
                    "WHERE generation_id=%s", (generation_id,),
                )
                stored_digests = cursor.fetchone()
            for column in ("request_digest", "source_set_digest"):
                with self.subTest(corrupted=column):
                    with self.connect() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE hosted_ai_generation SET {column}=%s WHERE generation_id=%s",
                            ("f" * 64, generation_id),
                        )
                    with self.assertRaisesRegex(ValueError, "unsafe_binding"):
                        PostgresAIRepository(self.connect).get_generation(generation_id)
                    with self.connect() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE hosted_ai_generation SET request_digest=%s,source_set_digest=%s "
                            "WHERE generation_id=%s",
                            (*stored_digests, generation_id),
                        )

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_atlas_projection_checkpoint WHERE campaign_id=%s", (campaign_id,))
            cursor.execute("DELETE FROM hosted_http_operation_receipt WHERE idempotency_key=ANY(%s)", (keys,))
            cursor.execute("DELETE FROM hosted_proposal_audit WHERE proposal_id=%s", (proposal_id,))
            cursor.execute("DELETE FROM hosted_proposal_version WHERE proposal_id=%s", (proposal_id,))
            cursor.execute("DELETE FROM hosted_ai_stream_event WHERE generation_id=%s", (generation_id,))
            cursor.execute("DELETE FROM hosted_ai_generation WHERE generation_id=%s", (generation_id,))
            cursor.execute("DELETE FROM hosted_campaign_head WHERE campaign_id=%s", (campaign_id,))
            cursor.execute("DELETE FROM hosted_publication_intent WHERE campaign_id=%s", (campaign_id,))


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL live-session test is opt-in")
class PostgresLiveSessionIntegrationTests(unittest.TestCase):
    """PostgreSQL-backed live lifecycle: start, capture (with provenance), end
    barrier, restart/recovery, multiple ended-session selection, concurrent start,
    and concurrent identical exact-replay (P2-E)."""

    def setUp(self) -> None:
        self.connect = lambda: psycopg.connect(DATABASE_URL)
        self.suffix = uuid.uuid4().hex[:16]
        self.campaign_id = f"campaign_{self.suffix}"

    def tearDown(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_live_receipt WHERE session_id IN (SELECT session_id FROM hosted_live_session WHERE campaign_id=%s)", (self.campaign_id,))
            cursor.execute("DELETE FROM hosted_live_capture WHERE session_id IN (SELECT session_id FROM hosted_live_session WHERE campaign_id=%s)", (self.campaign_id,))
            cursor.execute("DELETE FROM hosted_live_session WHERE campaign_id=%s", (self.campaign_id,))
            cursor.execute("DELETE FROM hosted_atlas_projection_checkpoint WHERE campaign_id=%s", (self.campaign_id,))
            cursor.execute("DELETE FROM hosted_http_operation_receipt WHERE operation LIKE %s AND idempotency_key LIKE %s", (r"live\_%", f"idem_{self.suffix}%"))
            cursor.execute("DELETE FROM hosted_campaign_head WHERE campaign_id=%s", (self.campaign_id,))
            cursor.execute("DELETE FROM hosted_publication_intent WHERE campaign_id=%s", (self.campaign_id,))

    @staticmethod
    def operation(name, request_id, key, **extra):
        value = {"contract_name": "operation_request", "contract_version": 2,
                 "request_id": request_id, "operation": name, "idempotency_key": key,
                 "payload_digest": "0" * 64, "expected_revision": None,
                 "expected_workflow_version": None}
        value.update(extra)
        return value

    @staticmethod
    def bind(payload):
        Payload = payload.get("operation_request", payload)
        Payload["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def _app(self, directory):
        root = Path(directory) / "runtime"
        snapshots = Path(directory) / "snapshots"
        return root, snapshots, SliceApplication(
            root, snapshot_root=snapshots, provider=SyntheticProvider(),
            receipts=PostgresHTTPRepository(self.connect),
            proposal_repository=PostgresProposalRepository(self.connect),
            workflow_repository=PostgresWorkflowRepository(self.connect),
            ai_repository=PostgresAIRepository(self.connect),
            atlas_repository=PostgresAtlasProjectionRepository(self.connect),
        )

    def _ready_campaign(self, app):
        readiness = app.provider_readiness()[1]
        consent = self.bind({"contract_name": "provider_consent_request", "contract_version": 2,
            "operation_request": self.operation("provider_consent", f"request_consent_{self.suffix}", f"idem_consent_{self.suffix}"),
            "input": {"explicit": True, "consent_identity_digest": readiness["consent_identity_digest"]}})
        app.provider_consent(consent)
        campaign = self.bind({"contract_name": "campaign_create_request", "contract_version": 2,
            "operation_request": self.operation("campaign_create", f"request_campaign_{self.suffix}", f"idem_campaign_{self.suffix}"),
            "input": {"campaign_id": self.campaign_id, "campaign_name": "Live PG", "adapter_id": "mothership"}})
        return app.create_campaign(campaign)[1]["head_revision"]

    def _start_payload(self, head_revision, session_id, controller_id="controller_alpha"):
        return self.bind({"contract_name": "live_start_request", "contract_version": 2,
            "operation_request": self.operation("live_start", f"request_start_{session_id}", f"idem_start_{session_id}_{self.suffix}"),
            "campaign_id": self.campaign_id, "session_id": session_id,
            "head_revision": head_revision, "controller_id": controller_id})

    def _capture_payload(self, session_id, *, event_id="event_fact", operation_id="operation_fact", device_id="device_one", device_order=1, controller_id="controller_alpha", ewv=1, text="Door opened", key_suffix="cap0"):
        return self.bind({"contract_name": "live_capture_request", "contract_version": 2,
            "operation_request": self.operation("live_capture", f"request_cap_{session_id}_{key_suffix}", f"idem_cap_{session_id}_{key_suffix}_{self.suffix}", expected_workflow_version=ewv),
            "campaign_id": self.campaign_id, "session_id": session_id,
            "controller_id": controller_id, "controller_epoch": 1,
            "event_id": event_id, "device_id": device_id, "operation_id": operation_id,
            "device_order": device_order, "capture_type": "confirmed_fact", "text": text, "record_id": "record-door"})

    def test_multi_device_barrier_identity_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, app = self._app(directory); head = self._ready_campaign(app)
            app.live_start(self.campaign_id, self._start_payload(head, "pair"))
            app.live_capture(self.campaign_id, self._capture_payload("pair", ewv=1, key_suffix="a"))
            app.live_capture(self.campaign_id, self._capture_payload("pair", device_id="device_two", event_id="event_two", ewv=2, key_suffix="b"))
            app.live_end(self.campaign_id, self._end_payload("pair", required_operation_ids=[("device_one", "operation_fact"), ("device_two", "operation_fact")], ewv=3))
            _, _, restarted = self._app(directory); _, view = restarted.live_read(self.campaign_id)
            barrier = view["end_barrier"]
            self.assertEqual("device_one", barrier["end_device_id"])
            self.assertEqual(2, len(barrier["required_operation_ids"]))
            self.assertEqual(2, len(barrier["acknowledged_operation_ids"]))

    def test_stale_device_order_rejected_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, app = self._app(directory); head = self._ready_campaign(app)
            app.live_start(self.campaign_id, self._start_payload(head, "orders"))
            app.live_capture(self.campaign_id, self._capture_payload("orders", device_order=2))
            _, _, restarted = self._app(directory)
            for order in (2, 1):
                with self.assertRaises(HTTPFailure) as ctx:
                    restarted.live_capture(self.campaign_id, self._capture_payload("orders", event_id=f"event_{order}", operation_id=f"op_{order}", device_order=order, ewv=2, key_suffix=f"bad{order}"))
                self.assertEqual(422, ctx.exception.status)

    def _end_payload(self, session_id, *, required_operation_ids, ewv, controller_id="controller_alpha"):
        return self.bind({"contract_name": "live_end_request", "contract_version": 2,
            "operation_request": self.operation("live_end", f"request_end_{session_id}", f"idem_end_{session_id}_{self.suffix}", expected_workflow_version=ewv),
            "campaign_id": self.campaign_id, "session_id": session_id,
            "controller_id": controller_id, "controller_epoch": 1,
            "device_id": "device_one", "operation_id": "operation_end",
            "required_operation_ids": [{"device_id": d, "operation_id": o} for d, o in required_operation_ids]})

    def test_live_lifecycle_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, app = self._app(directory)
            head = self._ready_campaign(app)
            app.live_start(self.campaign_id, self._start_payload(head, "session_alpha"))
            status, result = app.live_capture(self.campaign_id, self._capture_payload("session_alpha"))
            self.assertEqual((200, "accepted"), (status, result["outcome"]))
            self.assertEqual("record-door", result["session"]["events"][0]["record_id"])
            app.live_end(self.campaign_id, self._end_payload("session_alpha", required_operation_ids=[("device_one", "operation_fact")], ewv=2))
            # Restart against the same PostgreSQL.
            _, _, restarted = self._app(directory)
            status, view = restarted.live_read(self.campaign_id)
            self.assertEqual(200, status)
            self.assertEqual("ended_review_pending", view["mode"])
            self.assertEqual(["record-door"], [item["record_id"] for item in view["events"]])
            self.assertEqual([{"device_id":"device_one","operation_id":"operation_fact"}], view["end_barrier"]["required_operation_ids"])
            self.assertEqual([{"device_id":"device_one","operation_id":"operation_fact"}], view["end_barrier"]["acknowledged_operation_ids"])
            self.assertTrue(view["end_barrier"]["ready_for_proposal"])

    def test_multiple_ended_sessions_select_most_recent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, app = self._app(directory)
            head = self._ready_campaign(app)
            app.live_start(self.campaign_id, self._start_payload(head, "session_one"))
            app.live_end(self.campaign_id, self._end_payload("session_one", required_operation_ids=[], ewv=1))
            app.live_start(self.campaign_id, self._start_payload(head, "session_two"))
            app.live_end(self.campaign_id, self._end_payload("session_two", required_operation_ids=[], ewv=1))
            app.live_start(self.campaign_id, self._start_payload(head, "session_three"))
            app.live_end(self.campaign_id, self._end_payload("session_three", required_operation_ids=[], ewv=1))
            _, view = app.live_read(self.campaign_id)
            self.assertEqual("session_three", view["session_id"])
            self.assertEqual("ended_review_pending", view["mode"])

    def test_concurrent_identical_captures_replay_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, app = self._app(directory)
            head = self._ready_campaign(app)
            app.live_start(self.campaign_id, self._start_payload(head, "session_conc"))
            from concurrent.futures import ThreadPoolExecutor

            def attempt(_):
                connect = lambda: psycopg.connect(DATABASE_URL)
                capture_payload = {"contract_name": "live_capture_request", "contract_version": 2,
                    "operation_request": self.operation("live_capture", f"request_conc_{self.suffix}", f"idem_conc_{self.suffix}", expected_workflow_version=1),
                    "campaign_id": self.campaign_id, "session_id": "session_conc",
                    "controller_id": "controller_alpha", "controller_epoch": 1,
                    "event_id": "event_conc", "device_id": "device_one", "operation_id": "operation_conc",
                    "device_order": 1, "capture_type": "confirmed_fact", "text": "Concurrent", "record_id": None}
                target = capture_payload["operation_request"]
                target["payload_digest"] = canonical_digest(request_digest_input(capture_payload))
                local_app = SliceApplication(
                    Path(directory) / "runtime", snapshot_root=Path(directory) / "snapshots",
                    provider=SyntheticProvider(), receipts=PostgresHTTPRepository(connect),
                    proposal_repository=PostgresProposalRepository(connect),
                    workflow_repository=PostgresWorkflowRepository(connect),
                    ai_repository=PostgresAIRepository(connect),
                    atlas_repository=PostgresAtlasProjectionRepository(connect),
                )
                s, r = local_app.live_capture(self.campaign_id, capture_payload)
                return r["outcome"]

            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(attempt, range(4)))
            self.assertEqual(1, outcomes.count("accepted"))
            self.assertEqual(3, outcomes.count("exact_replay"))

    def test_concurrent_start_yields_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, app = self._app(directory)
            head = self._ready_campaign(app)
            from concurrent.futures import ThreadPoolExecutor

            def attempt(index):
                connect = lambda: psycopg.connect(DATABASE_URL)
                session_id = f"session_concstart_{index}_{self.suffix[-6:]}"
                start_payload = {"contract_name": "live_start_request", "contract_version": 2,
                    "operation_request": self.operation("live_start", f"request_cs_{index}_{self.suffix}", f"idem_cs_{index}_{self.suffix}"),
                    "campaign_id": self.campaign_id, "session_id": session_id,
                    "head_revision": head, "controller_id": "controller_alpha"}
                target = start_payload["operation_request"]
                target["payload_digest"] = canonical_digest(request_digest_input(start_payload))
                local_app = SliceApplication(
                    Path(directory) / "runtime", snapshot_root=Path(directory) / "snapshots",
                    provider=SyntheticProvider(), receipts=PostgresHTTPRepository(connect),
                    proposal_repository=PostgresProposalRepository(connect),
                    workflow_repository=PostgresWorkflowRepository(connect),
                    ai_repository=PostgresAIRepository(connect),
                    atlas_repository=PostgresAtlasProjectionRepository(connect),
                )
                try:
                    status, _ = local_app.live_start(self.campaign_id, start_payload)
                    return status
                except HTTPFailure as exc:
                    return exc.status

            with ThreadPoolExecutor(max_workers=4) as pool:
                outcomes = list(pool.map(attempt, range(4)))
            self.assertEqual(1, outcomes.count(201))
            # The remaining concurrent starts conflict as active_session_conflict.
            self.assertEqual(3, outcomes.count(409))
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM hosted_live_session WHERE campaign_id=%s AND mode='active'", (self.campaign_id,))
                self.assertEqual(1, cursor.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
