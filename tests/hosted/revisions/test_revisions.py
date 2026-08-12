from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from warden_drydock.hosted.projections import InMemoryProjectionRepository, ProjectionRebuilder
from warden_drydock.hosted.revisions import (
    FileSnapshotStore, InMemoryWorkflowRepository, PublicationIntent,
    PublicationIntentError, PublicationKind, RevisionService,
    SnapshotIntegrityError, SnapshotLineageError, StaleHeadError, canonicalize_tree,
)


DIGEST = "a" * 64


class RevisionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "record.md").write_bytes(
            b"---\nid: record-one\n---\n# One\n"
        )
        (self.source / "config.json").write_bytes(b"{}\n")
        self.store = FileSnapshotStore(root / "store")
        self.repository = InMemoryWorkflowRepository()
        self.service = RevisionService(self.store, self.repository)

    def intent(self, *, intent_id="intent_one", token="token_one", revision="revision_one", parent=None, ordinal=1, tree_digest=None):
        return PublicationIntent(
            intent_id, token, PublicationKind.CREATION, "campaign_one", revision,
            parent, ordinal, tree_digest or canonicalize_tree(self.source)[1], DIGEST,
        )

    def publish(self, intent=None):
        return self.service.publish(
            self.source, intent or self.intent(), framework_version="0.2.0",
            adapter_version="1.0.0", validation_contract_digest="b" * 64,
        )


class CanonicalizationTests(RevisionFixture):
    def test_golden_canonicalization_is_sorted_and_stable(self) -> None:
        files, digest = canonicalize_tree(self.source)
        self.assertEqual(("config.json", "record.md"), tuple(item.relative_path for item in files))
        self.assertEqual(
            "89914a610bf5e5133cdb6c3d4bd3e93a6d42d9cd1f7816ee986db18103be46b0",
            digest,
        )
        self.assertEqual((files, digest), canonicalize_tree(self.source))

    def test_symlink_and_non_regular_entries_fail_closed(self) -> None:
        marker = self.source / "record.md"
        original = Path.is_symlink
        with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=lambda value: value == marker or original(value)):
            with self.assertRaises(SnapshotIntegrityError):
                canonicalize_tree(self.source)

    def test_hash_mismatch_does_not_publish(self) -> None:
        with self.assertRaises(SnapshotIntegrityError):
            self.publish(self.intent(tree_digest="0" * 64))
        self.assertEqual((), tuple(self.store.snapshots.iterdir()))

    def test_source_mutation_during_copy_never_finalizes_a_head(self) -> None:
        import warden_drydock.hosted.revisions.store as store_module

        real_copytree = store_module.shutil.copytree

        def mutate_after_copy(source, target):
            result = real_copytree(source, target)
            (Path(target) / "record.md").write_bytes(b"changed after hash\n")
            return result

        with mock.patch.object(store_module.shutil, "copytree", mutate_after_copy):
            with self.assertRaises(SnapshotIntegrityError):
                self.publish()
        self.assertIsNone(self.repository.head("campaign_one"))

    def test_unsafe_manifest_identifiers_and_paths_fail_before_store_access(self) -> None:
        with self.assertRaises(ValueError):
            self.intent(revision="../private")
        from warden_drydock.hosted.revisions.models import FileHash
        with self.assertRaises(ValueError):
            FileHash("../private.md", "a" * 64)


class PublicationTests(RevisionFixture):
    def test_matching_intent_finalizes_exactly_once(self) -> None:
        manifest = self.publish()
        self.assertEqual("revision_one", self.repository.head("campaign_one"))
        self.assertFalse(self.service.reconcile_manifest(manifest))
        self.assertEqual([("intent_one", "finalized")], self.repository.audit)
        self.assertEqual(1, len(tuple(self.store.snapshots.iterdir())))

    def test_creation_and_approval_intents_share_exact_reconciliation_rules(self) -> None:
        first = self.publish()
        (self.source / "record.md").write_text("---\nid: record-one\n---\n# Approved\n", encoding="utf-8")
        approval = PublicationIntent(
            "intent_approval", "token_approval", PublicationKind.APPROVAL,
            "campaign_one", "revision_two", first.revision_id, 2,
            canonicalize_tree(self.source)[1], "c" * 64,
        )
        second = self.service.publish(
            self.source, approval, framework_version="0.2.0",
            adapter_version="1.0.0", validation_contract_digest="b" * 64,
        )
        self.assertEqual("revision_two", self.repository.head("campaign_one"))
        self.assertEqual("c" * 64, second.change_digest)

    def test_missing_intent_quarantines_published_snapshot(self) -> None:
        intent = self.intent()
        files, digest = canonicalize_tree(self.source)
        from warden_drydock.hosted.revisions.models import SnapshotManifest
        manifest = SnapshotManifest("campaign_one", "revision_one", None, 1, digest, files, "0.2.0", "1.0.0", "b" * 64, DIGEST, intent.intent_token)
        self.store.put_if_absent(self.source, manifest)
        with self.assertRaises(PublicationIntentError):
            self.service.reconcile_manifest(manifest)
        self.assertFalse((self.store.snapshots / digest / manifest.campaign_id / manifest.revision_id).exists())
        self.assertTrue((self.store.quarantine / digest / manifest.campaign_id / manifest.revision_id).exists())
        with self.assertRaises(SnapshotIntegrityError):
            self.store.put_if_absent(self.source, manifest)
        self.assertFalse(
            (self.store.snapshots / digest / manifest.campaign_id / manifest.revision_id).exists()
        )

    def test_conflicting_and_ambiguous_intents_quarantine(self) -> None:
        intent = self.intent()
        self.repository.add_intent(intent)
        second = self.intent(intent_id="intent_two", revision="revision_two")
        self.repository.intents[second.intent_id] = second
        files, digest = canonicalize_tree(self.source)
        from warden_drydock.hosted.revisions.models import SnapshotManifest
        manifest = SnapshotManifest("campaign_one", "revision_one", None, 1, digest, files, "0.2.0", "1.0.0", "b" * 64, DIGEST, intent.intent_token)
        self.store.put_if_absent(self.source, manifest)
        with self.assertRaises(PublicationIntentError):
            self.service.reconcile_manifest(manifest)
        self.assertEqual(2, len([row for row in self.repository.audit if row[1] == "quarantined"]))

    def test_duplicate_token_is_rejected_without_poisoning_finalized_head(self) -> None:
        first = self.publish()
        duplicate = self.intent(
            intent_id="intent_duplicate", revision="revision_duplicate"
        )
        with self.assertRaises(PublicationIntentError):
            self.repository.add_intent(duplicate)
        self.assertEqual((first,), self.service.verify_linear_inventory())
        self.assertEqual("revision_one", self.repository.head("campaign_one"))

    def test_stale_head_never_merges_and_is_quarantined(self) -> None:
        self.publish()
        (self.source / "record.md").write_text("---\nid: record-one\n---\n# Two\n", encoding="utf-8")
        stale = self.intent(intent_id="intent_stale", token="token_stale", revision="revision_two", parent=None, ordinal=1)
        with self.assertRaises(StaleHeadError):
            self.publish(stale)
        self.assertEqual("revision_one", self.repository.head("campaign_one"))
        self.assertEqual("quarantined", self.repository.intents["intent_stale"].status.value)

    def test_fault_after_snapshot_publication_reconciles_without_duplicate_head(self) -> None:
        intent = self.intent()
        original = self.repository.finalize_head
        with mock.patch.object(self.repository, "finalize_head", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.publish(intent)
        manifest = self.store.verify(
            intent.tree_digest, intent.campaign_id, intent.revision_id
        )
        self.assertTrue(self.service.reconcile_manifest(manifest))
        self.assertFalse(self.service.reconcile_manifest(manifest))
        self.assertEqual("revision_one", self.repository.head("campaign_one"))
        self.assertEqual(1, len([row for row in self.repository.audit if row[1] == "finalized"]))


class LineageAndProjectionTests(RevisionFixture):
    def test_rebuild_rejects_finalized_non_head_revision(self) -> None:
        first = self.publish()
        (self.source / "record.md").write_text(
            "---\nid: record-one\n---\n# Two\n", encoding="utf-8"
        )
        self.publish(
            self.intent(
                intent_id="intent_two", token="token_two",
                revision="revision_two", parent=first.revision_id, ordinal=2,
            )
        )
        with self.assertRaises(SnapshotLineageError):
            ProjectionRebuilder(
                self.store, InMemoryProjectionRepository(), self.repository
            ).rebuild(first)

    def test_verify_rejects_manifest_stored_under_another_identity(self) -> None:
        import shutil

        manifest = self.publish()
        source = (
            self.store.snapshots / manifest.tree_digest / manifest.campaign_id
            / manifest.revision_id
        )
        target = (
            self.store.snapshots / manifest.tree_digest / "campaign_other"
            / "revision_other"
        )
        target.parent.mkdir(parents=True)
        shutil.copytree(source, target)
        with self.assertRaises(SnapshotIntegrityError):
            self.store.verify(
                manifest.tree_digest, "campaign_other", "revision_other"
            )

    def test_unrelated_corrupt_campaign_does_not_block_rebuild(self) -> None:
        manifest = self.publish()
        corrupt = (
            self.store.snapshots / ("f" * 64) / "campaign_other"
            / "revision_other"
        )
        corrupt.mkdir(parents=True)
        (corrupt / "snapshot-manifest-v1.json").write_text(
            "not-json", encoding="utf-8"
        )
        result = ProjectionRebuilder(
            self.store, InMemoryProjectionRepository(), self.repository
        ).rebuild(manifest)
        self.assertEqual(manifest.revision_id, result.revision_id)

    def test_rebuild_rejects_child_when_parent_snapshot_is_missing(self) -> None:
        first = self.publish()
        (self.source / "record.md").write_text(
            "---\nid: record-one\n---\n# Two\n", encoding="utf-8"
        )
        second = self.publish(
            self.intent(
                intent_id="intent_two", token="token_two",
                revision="revision_two", parent=first.revision_id, ordinal=2,
            )
        )
        parent_path = (
            self.store.snapshots / first.tree_digest / first.campaign_id
            / first.revision_id
        )
        import shutil
        shutil.rmtree(parent_path)
        with self.assertRaises(SnapshotLineageError):
            ProjectionRebuilder(
                self.store, InMemoryProjectionRepository(), self.repository
            ).rebuild(second)

    def test_orphan_snapshot_is_quarantined_before_lineage_or_projection(self) -> None:
        intent = self.intent()
        files, digest = canonicalize_tree(self.source)
        from warden_drydock.hosted.revisions.models import SnapshotManifest

        manifest = SnapshotManifest(
            "campaign_one", "revision_orphan", None, 1, digest, files,
            "0.2.0", "1.0.0", "b" * 64, DIGEST, intent.intent_token,
        )
        self.store.put_if_absent(self.source, manifest)
        projections = InMemoryProjectionRepository()
        with self.assertRaises(SnapshotLineageError):
            ProjectionRebuilder(
                self.store, projections, self.repository
            ).rebuild(manifest)
        self.assertEqual({}, projections.active)
        with self.assertRaises(PublicationIntentError):
            self.service.verify_linear_inventory()

    def test_fork_or_ordinal_conflict_fails_closed(self) -> None:
        first = self.publish()
        (self.source / "record.md").write_text("---\nid: record-one\n---\n# Two\n", encoding="utf-8")
        second = self.publish(self.intent(intent_id="intent_two", token="token_two", revision="revision_two", parent=first.revision_id, ordinal=2))
        self.assertEqual((first, second), self.service.verify_linear_inventory())
        (self.source / "record.md").write_text("---\nid: record-one\n---\n# Fork\n", encoding="utf-8")
        files, digest = canonicalize_tree(self.source)
        from warden_drydock.hosted.revisions.models import SnapshotManifest
        fork = SnapshotManifest("campaign_one", "revision_fork", first.revision_id, 2, digest, files, "0.2.0", "1.0.0", "b" * 64, DIGEST, "token_fork")
        self.store.put_if_absent(self.source, fork)
        with self.assertRaises((PublicationIntentError, SnapshotLineageError)):
            self.service.verify_linear_inventory()

    def test_rebuild_is_deterministic_and_preserves_operational_state(self) -> None:
        manifest = self.publish()
        projections = InMemoryProjectionRepository()
        projections.operational["workflow"] = {"status": "needs_review"}
        rebuilder = ProjectionRebuilder(self.store, projections, self.repository)
        first = rebuilder.rebuild(manifest)
        second = rebuilder.rebuild(manifest)
        self.assertEqual(first, second)
        self.assertEqual(first, projections.active["campaign_one"])
        self.assertEqual(first, projections.active_checkpoint["campaign_one"])
        self.assertEqual({}, projections.shadow_checkpoint)
        self.assertEqual({"status": "needs_review"}, projections.operational["workflow"])
        self.assertEqual(1, first.record_count)

    def test_corrupt_snapshot_blocks_rebuild_before_shadow_mutation(self) -> None:
        manifest = self.publish()
        target = (
            self.store.snapshots
            / manifest.tree_digest
            / manifest.campaign_id
            / manifest.revision_id
            / "tree"
            / "record.md"
        )
        target.write_text("corrupt", encoding="utf-8")
        projections = InMemoryProjectionRepository()
        projections.operational["audit"] = ("safe",)
        with self.assertRaises(SnapshotIntegrityError):
            ProjectionRebuilder(
                self.store, projections, self.repository
            ).rebuild(manifest)
        self.assertEqual({}, projections.shadow)
        self.assertEqual({}, projections.active)
        self.assertEqual(("safe",), projections.operational["audit"])


class MigrationContractTests(unittest.TestCase):
    def test_postgres_migration_separates_operational_and_rebuildable_tables(self) -> None:
        migration = (
            Path(__file__).parents[3]
            / "warden_drydock"
            / "hosted"
            / "migrations"
            / "0001_revision_projection.sql"
        ).read_text(encoding="utf-8")
        for table in (
            "hosted_publication_intent", "hosted_campaign_head",
            "hosted_projection_checkpoint", "hosted_projection_record",
            "hosted_projection_shadow_checkpoint",
            "hosted_projection_shadow_record",
        ):
            self.assertIn(f"CREATE TABLE {table}", migration)
        self.assertIn("CHECK (status IN ('pending', 'finalized', 'quarantined'))", migration)
        self.assertNotIn("provider_secret", migration)


if __name__ == "__main__":
    unittest.main()
