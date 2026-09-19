from __future__ import annotations

import os
import threading
import unittest
import uuid
from dataclasses import replace

from warden_drydock.hosted.engine.models import ExactTextChange, Status
from warden_drydock.hosted.proposals import PostgresProposalRepository
from warden_drydock.hosted.proposals.service import ProposalService, ProposalStatus, ProposalVersion
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
            cursor.execute("DELETE FROM hosted_editor_workflow WHERE campaign_id LIKE %s", (self.prefix + "%",))

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

    def editor_item(self, suffix, campaign_id, *, version=1, workflow_version=2,
                    correction_of=None, status=ProposalStatus.DRAFT):
        metadata = {"editor_workflow_version": workflow_version}
        if correction_of is not None:
            metadata["correction_of"] = correction_of
        return ProposalVersion(
            self.prefix + suffix, version, campaign_id, "revision_one",
            (ExactTextChange("change_one", "record_one", "a" * 64, "# Two"),),
            "a" * 64, "b" * 64, status=status, editor_metadata=metadata,
        )

    def test_stale_initial_editor_operations_do_not_create_workflow_or_proposals(self):
        campaign_id = self.prefix + "_campaign"
        item = self.editor_item("_stale", campaign_id, workflow_version=3)

        self.assertFalse(self.repository.add_editor(item, campaign_id, 2))
        self.assertFalse(self.repository.advance_editor(campaign_id, 2))
        self.assertIsNone(self.repository.get(item.proposal_id, item.version))
        self.assertEqual(1, self.repository.editor_workflow_version(campaign_id))
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM hosted_editor_workflow WHERE campaign_id=%s", (campaign_id,))
            self.assertEqual((0,), cursor.fetchone())

    def test_editor_binding_mismatches_leave_workflow_and_proposals_unchanged(self):
        campaign_id = self.prefix + "_campaign"
        other_campaign_id = self.prefix + "_other_campaign"
        mismatched = self.editor_item("_mismatched", campaign_id)

        self.assertFalse(self.repository.add_editor(mismatched, other_campaign_id, 1))
        self.assertIsNone(self.repository.get(mismatched.proposal_id, mismatched.version))
        self.assertEqual(1, self.repository.editor_workflow_version(other_campaign_id))

        bound = self.editor_item("_bound", campaign_id)
        self.assertTrue(self.repository.add_editor(bound, campaign_id, 1))
        published = self.editor_item("_published", campaign_id, workflow_version=3, status=ProposalStatus.PUBLISHED)
        self.assertTrue(self.repository.add_editor(published, campaign_id, 2))
        self.assertEqual(ProposalStatus.DRAFT, self.repository.get(published.proposal_id, 1).status)
        stale = self.editor_item("_stale_binding", campaign_id, workflow_version=2)
        self.assertFalse(self.repository.add_editor(stale, campaign_id, 3))
        self.assertIsNone(self.repository.get(stale.proposal_id, stale.version))
        self.assertEqual(3, self.repository.editor_workflow_version(campaign_id))
        self.assertEqual((bound, replace(published, status=ProposalStatus.DRAFT)), self.repository.editor_proposals())

    def test_empty_editor_correction_leaves_workflow_and_proposals_unchanged(self):
        campaign_id = self.prefix + "_campaign"
        prior = self.editor_item("_empty_correction", campaign_id)
        self.assertTrue(self.repository.add_editor(prior, campaign_id, 1))

        successor = self.editor_item(
            "_empty_correction", campaign_id, version=2, workflow_version=3,
            correction_of={},
        )
        self.assertFalse(self.repository.add_editor(successor, campaign_id, 2))
        self.assertEqual(ProposalStatus.DRAFT, self.repository.get(prior.proposal_id, 1).status)
        self.assertIsNone(self.repository.get(successor.proposal_id, 2))
        self.assertEqual(2, self.repository.editor_workflow_version(campaign_id))
        self.assertEqual((prior,), self.repository.editor_proposals())

    def test_editor_corrections_require_same_editor_lineage(self):
        campaign_id = self.prefix + "_campaign"
        unrelated = self.editor_item("_unrelated", campaign_id)
        self.assertTrue(self.repository.add_editor(unrelated, campaign_id, 1))
        target = self.editor_item("_target", campaign_id, workflow_version=3)
        self.assertTrue(self.repository.add_editor(target, campaign_id, 2))

        wrong_proposal = self.editor_item(
            "_target", campaign_id, version=2, workflow_version=4,
            correction_of={"proposal_id": unrelated.proposal_id, "proposal_version": 1},
        )
        self.assertFalse(self.repository.add_editor(wrong_proposal, campaign_id, 3))
        self.assertEqual(ProposalStatus.DRAFT, self.repository.get(unrelated.proposal_id, 1).status)
        self.assertEqual(3, self.repository.editor_workflow_version(campaign_id))

        plain = ProposalVersion(
            self.prefix + "_plain", 1, campaign_id, "revision_one",
            (ExactTextChange("change_plain", "record_one", "a" * 64, "# Two"),),
            "a" * 64, "b" * 64,
        )
        self.repository.add(plain)
        non_editor_prior = self.editor_item(
            "_target", campaign_id, version=2, workflow_version=4,
            correction_of={"proposal_id": plain.proposal_id, "proposal_version": 1},
        )
        self.assertFalse(self.repository.add_editor(non_editor_prior, campaign_id, 3))
        self.assertEqual(ProposalStatus.DRAFT, self.repository.get(plain.proposal_id, 1).status)
        self.assertEqual(3, self.repository.editor_workflow_version(campaign_id))

        gap = self.editor_item(
            "_target", campaign_id, version=3, workflow_version=4,
            correction_of={"proposal_id": target.proposal_id, "proposal_version": 1},
        )
        with self.assertRaisesRegex(ValueError, "proposal_version_conflict"):
            self.repository.add_editor(gap, campaign_id, 3)
        self.assertEqual(3, self.repository.editor_workflow_version(campaign_id))

        valid = self.editor_item(
            "_target", campaign_id, version=2, workflow_version=4,
            correction_of={"proposal_id": target.proposal_id, "proposal_version": 1},
        )
        self.assertTrue(self.repository.add_editor(valid, campaign_id, 3))
        self.assertEqual(ProposalStatus.REJECTED, self.repository.get(target.proposal_id, 1).status)
        self.assertEqual(ProposalStatus.DRAFT, self.repository.get(valid.proposal_id, 2).status)
        self.assertEqual(4, self.repository.editor_workflow_version(campaign_id))

    def test_editor_metadata_survives_correction_save_and_restart(self):
        campaign_id = self.prefix + "_campaign"
        item = self.editor_item("_correct", campaign_id)
        self.assertTrue(self.repository.add_editor(item, campaign_id, 1))
        corrected = self.repository.correct(
            item,
            (ExactTextChange("change_two", "record_one", "a" * 64, "# Three"),),
            item.base_revision,
        )
        self.assertEqual(item.editor_metadata, corrected.editor_metadata)

        restarted = PostgresProposalRepository(self.connect)
        self.assertEqual(corrected, restarted.get(corrected.proposal_id, corrected.version))
        self.assertEqual(
            (item.editor_metadata, corrected.editor_metadata),
            tuple(value.editor_metadata for value in restarted.editor_proposals()),
        )

        saved_metadata = {"editor_workflow_version": 2, "saved": "yes"}
        saved = restarted.save_editor_metadata(
            corrected.proposal_id, corrected.version, saved_metadata, "revision_two",
        )
        self.assertEqual(saved_metadata, saved.editor_metadata)
        self.assertEqual("revision_two", saved.published_revision_id)
        self.assertEqual(saved, PostgresProposalRepository(self.connect).get(saved.proposal_id, saved.version))

    def test_editor_proposals_read_back_in_stable_order(self):
        campaign_id = self.prefix + "_campaign"
        items = (
            self.editor_item("_z", campaign_id, workflow_version=2),
            self.editor_item("_a", campaign_id, workflow_version=2),
            self.editor_item("_z", campaign_id, version=2, workflow_version=3),
            self.editor_item("_a", campaign_id, version=2, workflow_version=3),
        )
        for item in (items[0], items[1], items[2], items[3]):
            self.repository.add(item)

        expected = tuple(sorted(items, key=lambda item: (item.proposal_id, item.version)))
        self.assertEqual(expected, self.repository.editor_proposals())
        self.assertEqual(expected, PostgresProposalRepository(self.connect).editor_proposals())

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
