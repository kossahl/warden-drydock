from __future__ import annotations

import os
import threading
import unittest
import uuid

from warden_drydock.hosted.engine.models import ExactTextChange, Status
from warden_drydock.hosted.proposals import PostgresProposalRepository
from warden_drydock.hosted.proposals.service import ProposalService, ProposalStatus


DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL proposal test is opt-in")
class PostgresProposalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.prefix = "proposal_" + uuid.uuid4().hex[:16]
        self.connect = lambda: psycopg.connect(DATABASE_URL)
        self.repository = PostgresProposalRepository(self.connect)
        self.publish_calls = []
        self.service = ProposalService(
            self.repository,
            head=lambda _: "revision_one",
            stage=lambda _: type("Stage", (), {"status": Status.STAGED})(),
            publish=lambda item, _: self.publish_calls.append(item.proposal_id) or "revision_two",
        )

    def tearDown(self):
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_proposal_audit WHERE proposal_id LIKE %s", (self.prefix + "%",))
            cursor.execute("DELETE FROM hosted_proposal_version WHERE proposal_id LIKE %s", (self.prefix + "%",))

    def draft(self, suffix="one"):
        return self.service.draft(
            self.prefix + suffix, "campaign_one", "revision_one",
            (ExactTextChange("change_one", "record_one", "a" * 64, "# Two"),),
        )

    @staticmethod
    def binding(item):
        return dict(diff_digest=item.diff_digest, base_revision=item.base_revision,
                    payload_digest=item.payload_digest)

    def race(self, *operations):
        barrier = threading.Barrier(len(operations))
        results = []
        def run(operation):
            barrier.wait()
            try:
                results.append(operation())
            except ValueError:
                results.append("conflict")
        threads = [threading.Thread(target=run, args=(operation,)) for operation in operations]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        return results

    def test_competing_approvals_publish_once_and_exact_retry_is_idempotent(self):
        item = self.draft()
        results = self.race(*(
            (lambda: self.service.approve(item, **self.binding(item))) for _ in range(8)
        ))
        self.assertEqual(1, len(self.publish_calls))
        self.assertEqual(ProposalStatus.PUBLISHED, self.repository.get(item.proposal_id, 1).status)
        self.assertEqual(ProposalStatus.PUBLISHED, self.service.approve(item, **self.binding(item)).status)
        self.assertEqual(1, len(self.publish_calls))
        self.assertEqual(8, len(results))

    def test_approve_reject_and_approve_correct_have_single_winner(self):
        item = self.draft("reject")
        self.race(lambda: self.service.approve(item, **self.binding(item)),
                  lambda: self.service.reject(item))
        current = self.repository.get(item.proposal_id, 1)
        self.assertIn(current.status, (ProposalStatus.PUBLISHED, ProposalStatus.REJECTED))
        self.assertFalse(current.status is ProposalStatus.REJECTED and item.proposal_id in self.publish_calls)

        item = self.draft("correct")
        self.race(lambda: self.service.approve(item, **self.binding(item)),
                  lambda: self.service.correct(item, (ExactTextChange("change_two", "record_one", "a" * 64, "# Three"),)))
        versions = self.repository.versions(item.proposal_id)
        self.assertIn(tuple(value.status for value in versions),
                      ((ProposalStatus.PUBLISHED,), (ProposalStatus.REJECTED, ProposalStatus.DRAFT)))

    def test_transaction_rollback_and_restart_readback(self):
        item = self.draft("rollback")
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE OR REPLACE FUNCTION drydock_test_fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced audit rollback'; END $$")
            cursor.execute("CREATE TRIGGER drydock_test_fail_audit BEFORE INSERT ON hosted_proposal_audit FOR EACH ROW EXECUTE FUNCTION drydock_test_fail_audit()")
        try:
            with self.assertRaises(Exception):
                self.repository.reject(item)
            self.assertEqual(ProposalStatus.DRAFT, self.repository.get(item.proposal_id, 1).status)
        finally:
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER IF EXISTS drydock_test_fail_audit ON hosted_proposal_audit")
                cursor.execute("DROP FUNCTION IF EXISTS drydock_test_fail_audit()")
        restarted = PostgresProposalRepository(self.connect)
        readback = restarted.get(item.proposal_id, 1)
        self.assertEqual((item.diff_digest, item.payload_digest, item.changes),
                         (readback.diff_digest, readback.payload_digest, readback.changes))

    def test_publication_linkage_and_reconciliation_survive_restart(self):
        item = self.draft("reconcile")
        claimed = self.repository.claim(item)
        quarantined = self.repository.replace_status(claimed, ProposalStatus.QUARANTINED)
        published = self.service.reconcile(quarantined, "revision_reconciled")
        self.assertEqual(ProposalStatus.PUBLISHED, published.status)
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT published_revision_id FROM hosted_proposal_version WHERE proposal_id=%s AND version=1", (item.proposal_id,))
            self.assertEqual(("revision_reconciled",), cursor.fetchone())
        self.assertEqual(ProposalStatus.PUBLISHED, PostgresProposalRepository(self.connect).get(item.proposal_id, 1).status)
