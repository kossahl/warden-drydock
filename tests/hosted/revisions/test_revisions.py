from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from warden_drydock.hosted.operations.migrate import migration_body, migration_files
from warden_drydock.hosted.projections import InMemoryProjectionRepository, ProjectionRebuilder
from warden_drydock.hosted.revisions import (
    FileSnapshotStore, InMemoryWorkflowRepository, PublicationIntent,
    PublicationIntentError, PublicationKind, RevisionService,
    SnapshotIntegrityError, SnapshotLineageError, StaleHeadError, canonicalize_tree,
)
from ._migration_raw import (
    TRANSACTION_ENDS,
    TRANSACTION_STARTS,
    assert_no_outer_transaction_wrapper_raw,
    migration_boundaries,
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
    def test_reconciliation_rejects_missing_snapshot_without_finalizing(self) -> None:
        intent = self.intent()
        self.repository.add_intent(intent)
        files, digest = canonicalize_tree(self.source)
        from warden_drydock.hosted.revisions.models import SnapshotManifest

        manifest = SnapshotManifest(
            "campaign_one", "revision_one", None, 1, digest, files,
            "0.2.0", "1.0.0", "b" * 64, DIGEST, intent.intent_token,
        )
        with self.assertRaises(FileNotFoundError):
            self.service.reconcile_manifest(manifest)
        self.assertIsNone(self.repository.head("campaign_one"))
        self.assertEqual("pending", self.repository.intents[intent.intent_id].status.value)

    def test_reconciliation_rejects_corrupt_stored_snapshot(self) -> None:
        intent = self.intent()
        self.repository.add_intent(intent)
        files, digest = canonicalize_tree(self.source)
        from warden_drydock.hosted.revisions.models import SnapshotManifest

        manifest = SnapshotManifest(
            "campaign_one", "revision_one", None, 1, digest, files,
            "0.2.0", "1.0.0", "b" * 64, DIGEST, intent.intent_token,
        )
        self.store.put_if_absent(self.source, manifest)
        stored = (
            self.store.snapshots / digest / manifest.campaign_id
            / manifest.revision_id / "tree" / "record.md"
        )
        stored.write_bytes(b"corrupt\n")
        with self.assertRaises(SnapshotIntegrityError):
            self.service.reconcile_manifest(manifest)
        self.assertIsNone(self.repository.head("campaign_one"))

    def test_reconciliation_rejects_manifest_different_from_stored(self) -> None:
        intent = self.intent()
        self.repository.add_intent(intent)
        files, digest = canonicalize_tree(self.source)
        from dataclasses import replace
        from warden_drydock.hosted.revisions.models import SnapshotManifest

        stored = SnapshotManifest(
            "campaign_one", "revision_one", None, 1, digest, files,
            "0.2.0", "1.0.0", "b" * 64, DIGEST, intent.intent_token,
        )
        self.store.put_if_absent(self.source, stored)
        supplied = replace(stored, framework_version="0.2.1")
        with self.assertRaises(SnapshotIntegrityError):
            self.service.reconcile_manifest(supplied)
        self.assertIsNone(self.repository.head("campaign_one"))

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

    def test_head_advance_between_stage_and_swap_rejects_stale_projection(self) -> None:
        first = self.publish()
        projections = InMemoryProjectionRepository()
        real_stage = projections.stage

        def stage_then_advance(bundle):
            real_stage(bundle)
            (self.source / "record.md").write_text(
                "---\nid: record-one\n---\n# Two\n", encoding="utf-8"
            )
            self.publish(
                self.intent(
                    intent_id="intent_two", token="token_two",
                    revision="revision_two", parent=first.revision_id,
                    ordinal=2,
                )
            )

        with mock.patch.object(projections, "stage", stage_then_advance):
            with self.assertRaises(ValueError):
                ProjectionRebuilder(
                    self.store, projections, self.repository
                ).rebuild(first)
        self.assertEqual("revision_two", self.repository.head("campaign_one"))
        self.assertEqual({}, projections.active)

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
        path = (
            Path(__file__).parents[3]
            / "warden_drydock"
            / "hosted"
            / "migrations"
            / "0001_revision_projection.sql"
        )
        raw = path.read_text(encoding="utf-8")
        self.assertEqual(path, migration_files(path.parent)[0])
        statements = _split_statements(migration_body(path))
        for statement in statements:
            self.assertTrue(
                statement.startswith("CREATE TABLE")
                or statement.startswith("CREATE INDEX"),
                statement,
            )
        tables = [
            _parse_create_table(statement)
            for statement in statements
            if statement.startswith("CREATE TABLE")
        ]
        indexes = [
            _parse_create_index(statement)
            for statement in statements
            if statement.startswith("CREATE INDEX")
        ]
        names = [table["name"] for table in tables]
        self.assertEqual(
            [
                "hosted_publication_intent",
                "hosted_campaign_head",
                "hosted_projection_checkpoint",
                "hosted_projection_shadow_checkpoint",
                "hosted_projection_record",
                "hosted_projection_shadow_record",
            ],
            names,
        )
        rebuildable = {name for name in names if name.startswith("hosted_projection_")}
        self.assertEqual(
            {"hosted_publication_intent", "hosted_campaign_head"},
            set(names) - rebuildable,
        )
        intent, head, checkpoint, shadow_checkpoint, record, shadow_record = tables

        intent_columns = intent["columns"]
        self.assertEqual(
            {
                "intent_id", "intent_token", "kind", "campaign_id", "revision_id",
                "parent_revision", "ordinal", "tree_digest", "change_digest", "status",
            },
            set(intent_columns),
        )
        self.assertEqual("text", intent_columns["intent_id"]["type"])
        self.assertTrue(intent_columns["intent_id"]["primary_key"])
        self.assertEqual("text", intent_columns["intent_token"]["type"])
        self.assertTrue(intent_columns["intent_token"]["not_null"])
        self.assertIn("UNIQUE (intent_token)", intent["constraints"])
        self.assertEqual("text", intent_columns["kind"]["type"])
        self.assertEqual(
            "CHECK (kind IN ('creation', 'approval'))",
            intent_columns["kind"]["check"],
        )
        self.assertEqual("char(64)", intent_columns["tree_digest"]["type"])
        self.assertTrue(intent_columns["tree_digest"]["not_null"])
        self.assertEqual("char(64)", intent_columns["change_digest"]["type"])
        self.assertEqual("text", intent_columns["status"]["type"])
        self.assertTrue(intent_columns["status"]["not_null"])
        self.assertEqual(
            "CHECK (status IN ('pending', 'finalized', 'quarantined'))",
            intent_columns["status"]["check"],
        )
        self.assertEqual("integer", intent_columns["ordinal"]["type"])
        self.assertTrue(intent_columns["ordinal"]["not_null"])
        self.assertEqual("CHECK (ordinal > 0)", intent_columns["ordinal"]["check"])
        self.assertEqual("text", intent_columns["campaign_id"]["type"])
        self.assertTrue(intent_columns["campaign_id"]["not_null"])
        self.assertEqual("text", intent_columns["revision_id"]["type"])
        self.assertTrue(intent_columns["revision_id"]["not_null"])
        self.assertEqual("text", intent_columns["parent_revision"]["type"])
        self.assertFalse(intent_columns["parent_revision"]["not_null"])

        head_columns = head["columns"]
        self.assertEqual(
            {"campaign_id", "revision_id", "ordinal"}, set(head_columns)
        )
        self.assertEqual("text", head_columns["campaign_id"]["type"])
        self.assertTrue(head_columns["campaign_id"]["primary_key"])
        self.assertEqual("text", head_columns["revision_id"]["type"])
        self.assertTrue(head_columns["revision_id"]["not_null"])
        self.assertTrue(head_columns["revision_id"]["unique"])
        self.assertEqual("integer", head_columns["ordinal"]["type"])
        self.assertTrue(head_columns["ordinal"]["not_null"])
        self.assertEqual("CHECK (ordinal > 0)", head_columns["ordinal"]["check"])

        checkpoint_columns = checkpoint["columns"]
        self.assertEqual(
            {
                "campaign_id", "revision_id", "projection_version",
                "record_count", "projection_digest",
            },
            set(checkpoint_columns),
        )
        self.assertEqual("text", checkpoint_columns["campaign_id"]["type"])
        self.assertTrue(checkpoint_columns["campaign_id"]["primary_key"])
        self.assertEqual("text", checkpoint_columns["revision_id"]["type"])
        self.assertTrue(checkpoint_columns["revision_id"]["not_null"])
        self.assertEqual("integer", checkpoint_columns["projection_version"]["type"])
        self.assertTrue(checkpoint_columns["projection_version"]["not_null"])
        self.assertEqual("integer", checkpoint_columns["record_count"]["type"])
        self.assertTrue(checkpoint_columns["record_count"]["not_null"])
        self.assertEqual("char(64)", checkpoint_columns["projection_digest"]["type"])
        self.assertTrue(checkpoint_columns["projection_digest"]["not_null"])

        record_columns = record["columns"]
        self.assertEqual(
            {"campaign_id", "revision_id", "record_id", "relative_path", "body_digest"},
            set(record_columns),
        )
        self.assertEqual("text", record_columns["campaign_id"]["type"])
        self.assertTrue(record_columns["campaign_id"]["not_null"])
        self.assertEqual("text", record_columns["revision_id"]["type"])
        self.assertTrue(record_columns["revision_id"]["not_null"])
        self.assertEqual("text", record_columns["record_id"]["type"])
        self.assertTrue(record_columns["record_id"]["not_null"])
        self.assertEqual("text", record_columns["relative_path"]["type"])
        self.assertTrue(record_columns["relative_path"]["not_null"])
        self.assertEqual("char(64)", record_columns["body_digest"]["type"])
        self.assertIn("PRIMARY KEY(campaign_id, record_id)", record["constraints"])

        self.assertEqual(
            "LIKE hosted_projection_checkpoint INCLUDING ALL",
            shadow_checkpoint["like_body"],
        )
        self.assertEqual("hosted_projection_checkpoint", shadow_checkpoint["like_base"])
        self.assertEqual(
            "LIKE hosted_projection_record INCLUDING ALL",
            shadow_record["like_body"],
        )
        self.assertEqual("hosted_projection_record", shadow_record["like_base"])

        self.assertEqual(1, len(indexes))
        self.assertEqual(
            ["hosted_publication_intent_token_idx"],
            [index["name"] for index in indexes],
        )
        self.assertEqual("hosted_publication_intent", indexes[0]["table"])
        self.assertEqual("intent_token", indexes[0]["columns"])
        intent_statement = next(
            statement
            for statement in statements
            if statement.startswith("CREATE TABLE hosted_publication_intent")
        )
        index_statement = next(
            statement
            for statement in statements
            if statement.startswith("CREATE INDEX")
        )
        self.assertLess(
            statements.index(intent_statement), statements.index(index_statement)
        )
        self.assertEqual(
            "CREATE INDEX hosted_publication_intent_token_idx "
            "ON hosted_publication_intent(intent_token)",
            index_statement,
        )
        self.assertEqual(
            " ".join("CREATE INDEX hosted_publication_intent_token_idx "
                     "ON hosted_publication_intent(intent_token)".split()),
            " ".join(index_statement.split()),
        )
        self.assertNotIn("UNIQUE", index_statement)
        self.assertNotIn("provider_secret", raw)


class MigrationRawWrapperTests(unittest.TestCase):
    """Regression coverage for the raw outer-transaction wrapper rule.

    The rule is exercised against controlled fixture files rather than the
    real migrations, because on this branch's base (before PR #153 merges)
    ``0001_revision_projection.sql`` and ``0003_ai_live_backend.sql`` still
    carry ``BEGIN;``/``COMMIT;`` wrappers. The check reads raw file content:
    ``migration_body()`` strips wrappers, so parsing the body would never
    see them.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temporary_dir = Path(self.temporary.name)

    def write_migration(self, name: str, content: str) -> Path:
        path = self.temporary_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_wrapped_migration_is_rejected_with_path_and_token(self) -> None:
        path = self.write_migration(
            "sample.sql",
            "BEGIN;\n\nCREATE TABLE sample (id text);\n\nCOMMIT;\n",
        )
        with self.assertRaises(AssertionError) as caught:
            assert_no_outer_transaction_wrapper_raw(path)
        self.assertIn(str(path), str(caught.exception))
        self.assertIn("BEGIN;", str(caught.exception))

    def test_every_transaction_start_token_is_rejected(self) -> None:
        for token in sorted(TRANSACTION_STARTS):
            label = token.replace(" ", "_").replace(";", "")
            path = self.write_migration(
                f"start_{label}.sql",
                f"{token}\n\nCREATE TABLE sample (id text);\nCOMMIT;\n",
            )
            with self.assertRaises(AssertionError) as caught:
                assert_no_outer_transaction_wrapper_raw(path)
            self.assertIn(token, str(caught.exception))

    def test_every_transaction_end_token_is_rejected(self) -> None:
        for token in sorted(TRANSACTION_ENDS):
            label = token.replace(" ", "_").replace(";", "")
            path = self.write_migration(
                f"end_{label}.sql",
                f"CREATE TABLE sample (id text);\n{token}\n",
            )
            with self.assertRaises(AssertionError) as caught:
                assert_no_outer_transaction_wrapper_raw(path)
            self.assertIn(token, str(caught.exception))

    def test_shared_line_wrappers_are_rejected(self) -> None:
        both = self.write_migration(
            "shared_both.sql",
            "BEGIN; CREATE TABLE sample (id text); COMMIT;\n",
        )
        with self.assertRaises(AssertionError) as caught:
            assert_no_outer_transaction_wrapper_raw(both)
        self.assertIn("BEGIN;", str(caught.exception))
        trailing = self.write_migration(
            "shared_end.sql",
            "CREATE TABLE sample (id text); COMMIT;\n",
        )
        with self.assertRaises(AssertionError) as caught:
            assert_no_outer_transaction_wrapper_raw(trailing)
        self.assertIn("COMMIT;", str(caught.exception))

    def test_comment_lines_do_not_hide_wrappers(self) -> None:
        path = self.write_migration(
            "commented.sql",
            "-- generated header\n\nBEGIN; -- open\n\nCREATE TABLE sample (id text);\n"
            "-- middle comment\n\nCOMMIT; -- close\n",
        )
        with self.assertRaises(AssertionError) as caught:
            assert_no_outer_transaction_wrapper_raw(path)
        self.assertIn("BEGIN;", str(caught.exception))

    def test_wrapper_free_migration_passes(self) -> None:
        path = self.write_migration(
            "clean.sql",
            "CREATE TABLE sample (id text);\nCREATE INDEX sample_idx ON sample(id);\n",
        )
        assert_no_outer_transaction_wrapper_raw(path)
        first, last = migration_boundaries(path)
        self.assertNotIn(first, TRANSACTION_STARTS)
        self.assertNotIn(last, TRANSACTION_ENDS)
        self.assertEqual("CREATE TABLE SAMPLE (ID TEXT);", first)

    def test_empty_and_comment_only_files_have_no_boundaries(self) -> None:
        empty = self.write_migration("empty.sql", "")
        assert_no_outer_transaction_wrapper_raw(empty)
        self.assertEqual(("", ""), migration_boundaries(empty))
        comments = self.write_migration(
            "comments.sql", "-- only comments\n-- still none\n"
        )
        assert_no_outer_transaction_wrapper_raw(comments)
        self.assertEqual(("", ""), migration_boundaries(comments))

    def test_migration_files_scan_applies_raw_rule_per_file(self) -> None:
        wrapped = self.write_migration(
            "0001_wrapped.sql",
            "BEGIN;\n\nCREATE TABLE sample (id text);\nCOMMIT;\n",
        )
        clean = self.write_migration(
            "0002_clean.sql",
            "CREATE TABLE sample (id text);\n",
        )
        self.assertEqual([wrapped, clean], migration_files(self.temporary_dir))
        with self.assertRaises(AssertionError):
            assert_no_outer_transaction_wrapper_raw(wrapped)
        assert_no_outer_transaction_wrapper_raw(clean)


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_quote = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            if in_quote and index + 1 < len(sql) and sql[index + 1] == "'":
                current.append("''")
                index += 2
                continue
            in_quote = not in_quote
            current.append(char)
        elif (
            char == "-"
            and not in_quote
            and index + 1 < len(sql)
            and sql[index + 1] == "-"
        ):
            while index < len(sql) and sql[index] != "\n":
                index += 1
        elif char == ";" and not in_quote:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _matching_group(text: str, opener: str) -> str | None:
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_quote = False
    index = start
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_quote and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            in_quote = not in_quote
        elif char == "(" and not in_quote:
            depth += 1
        elif char == ")" and not in_quote:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
        index += 1
    raise AssertionError(f"unbalanced parentheses in {text!r}")


def _split_top_level_definitions(text: str) -> list[str]:
    entries: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_quote and index + 1 < len(text) and text[index + 1] == "'":
                current.append("''")
                index += 2
                continue
            in_quote = not in_quote
            current.append(char)
        elif char == "(" and not in_quote:
            depth += 1
            current.append(char)
        elif char == ")" and not in_quote:
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0 and not in_quote:
            entries.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        entries.append(tail)
    return entries


_CONSTRAINT_MARKERS = (" NOT NULL", " PRIMARY KEY", " UNIQUE", " CHECK", " REFERENCES")


def _column_definition(entry: str) -> tuple[str, dict]:
    name, _, rest = entry.partition(" ")
    positions = [rest.find(marker) for marker in _CONSTRAINT_MARKERS if marker in rest]
    column_type = rest[: min(positions)] if positions else rest
    return name, {
        "type": column_type.strip(),
        "not_null": "NOT NULL" in rest,
        "unique": "UNIQUE" in rest,
        "primary_key": "PRIMARY KEY" in rest,
        "check": _matching_group(rest, "CHECK"),
    }


def _parse_create_table(statement: str) -> dict:
    name = statement[len("CREATE TABLE") :].strip().split()[0]
    body = _matching_group(statement, "(")[1:-1].strip()
    if body.startswith("LIKE "):
        return {
            "name": name,
            "like_body": body,
            "like_base": body.split()[1],
            "columns": {},
            "constraints": [],
        }
    columns: dict[str, dict] = {}
    constraints: list[str] = []
    for entry in _split_top_level_definitions(body):
        if entry.startswith(("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY", "CONSTRAINT")):
            constraints.append(entry)
        else:
            column_name, definition = _column_definition(entry)
            columns[column_name] = definition
    return {
        "name": name,
        "like_body": None,
        "like_base": None,
        "columns": columns,
        "constraints": constraints,
    }


def _parse_create_index(statement: str) -> dict:
    head = statement[len("CREATE INDEX") : statement.find("(")].strip()
    parts = head.split()
    return {
        "name": parts[0],
        "table": parts[parts.index("ON") + 1],
        "columns": _matching_group(statement, "(")[1:-1],
    }


if __name__ == "__main__":
    unittest.main()
