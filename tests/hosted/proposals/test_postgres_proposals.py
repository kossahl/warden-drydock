from __future__ import annotations

import os
import threading
import unittest
import uuid

from warden_drydock.hosted.engine.models import ExactTextChange, Status
from warden_drydock.hosted.proposals import PostgresProposalRepository
from warden_drydock.hosted.proposals.service import ProposalService, ProposalStatus
from warden_drydock.hosted.revisions.models import FileHash, SnapshotManifest


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
            publish=lambda item, _: self.publish_calls.append(item.proposal_id) or self.manifest(item),
            verify_publication=lambda value: value,
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

    @staticmethod
    def manifest(item, revision="revision_two"):
        return SnapshotManifest(item.campaign_id, revision, item.base_revision, 2,
            "b" * 64, (FileHash("record.md", "c" * 64),), "0.3.0", "1.0.0",
            "d" * 64, item.diff_digest, "token_publish")

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
        self.assertEqual(len(results), results.count("conflict") + 1)

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
        resolved = [value for value in versions
                    if value.status in (ProposalStatus.PUBLISHED, ProposalStatus.REJECTED)]
        self.assertEqual(1, len(resolved), "exactly one operation wins across all versions")
        published_for_item = [call for call in self.publish_calls if call == item.proposal_id]
        self.assertEqual(1 if resolved[0].status is ProposalStatus.PUBLISHED else 0,
                         len(published_for_item),
                         "publication count is 0 or 1 in lockstep with the winner")

    def test_transaction_rollback_and_restart_readback(self):
        item = self.draft("rollback")
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE OR REPLACE FUNCTION drydock_test_fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced audit rollback'; END $$")
            cursor.execute("CREATE TRIGGER drydock_test_fail_audit BEFORE INSERT ON hosted_proposal_audit FOR EACH ROW EXECUTE FUNCTION drydock_test_fail_audit()")
        try:
            with self.assertRaisesRegex(
                psycopg.errors.RaiseException, "forced audit rollback"
            ):
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
        published = self.service.reconcile(quarantined, self.manifest(item, "revision_reconciled"))
        self.assertEqual(ProposalStatus.PUBLISHED, published.status)
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT publication_intent_token, published_revision_id, result_digest FROM hosted_proposal_version WHERE proposal_id=%s AND version=1", (item.proposal_id,))
            self.assertEqual(("token_publish", "revision_reconciled", "b" * 64), cursor.fetchone())
        restarted = PostgresProposalRepository(self.connect)
        self.assertEqual(ProposalStatus.PUBLISHED, restarted.get(item.proposal_id, 1).status)
        published_events = [row for row in restarted.audit(item.proposal_id) if row[4] == "published"]
        self.assertEqual(1, len(published_events))
        self.assertEqual(("token_publish", "revision_reconciled", "b" * 64), published_events[0][5:8])

    def test_reconciliation_rejects_unverified_or_mismatched_manifest(self):
        item = self.draft("bad_reconcile")
        quarantined = self.repository.replace_status(self.repository.claim(item), ProposalStatus.QUARANTINED)
        with self.assertRaisesRegex(ValueError, "not a verified snapshot manifest"):
            self.service.reconcile(quarantined, "private/path")
        wrong = SnapshotManifest("campaign_other", "revision_other", item.base_revision, 2,
            "b" * 64, (FileHash("record.md", "c" * 64),), "0.3.0", "1.0.0",
            "d" * 64, item.diff_digest, "token_publish")
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.service.reconcile(quarantined, wrong)
        self.assertEqual(ProposalStatus.QUARANTINED, self.repository.get(item.proposal_id, 1).status)

    def test_unsafe_identifiers_never_reach_postgres_or_audit(self):
        unsafe_id = r"C:\private\campaign.md"
        with self.assertRaisesRegex(ValueError, r"proposal_id is not a safe public identifier"):
            self.service.draft(unsafe_id, "campaign_one", "revision_one",
                (ExactTextChange("change_one", "record_one", "a" * 64, "private"),))
        # The rejected id must never be written to any proposal table: both the
        # audit trail and the version/head table must stay empty for this run,
        # whether a leaking repository stores the id under the safe test prefix
        # or verbatim under the rejected Windows-style path itself.
        with self.connect() as connection, connection.cursor() as cursor:
            for proposal_table in ("hosted_proposal_audit", "hosted_proposal_version"):
                cursor.execute(
                    f"SELECT count(*) FROM {proposal_table} WHERE proposal_id LIKE %s OR proposal_id = %s",
                    (self.prefix + "%", unsafe_id),
                )
                self.assertEqual((0,), cursor.fetchone(), proposal_table)
