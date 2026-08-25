from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from warden_drydock.hosted.operations import recover
from warden_drydock.hosted.revisions import (
    FileSnapshotStore,
    SnapshotManifest,
    canonicalize_tree,
)


class PendingPublicationRecoveryTests(unittest.TestCase):
    def test_operator_clears_verified_pending_candidate_without_advancing_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "record.md").write_text(
                "---\nid: campaign-main\n---\n# Campaign\n", encoding="utf-8"
            )
            files, tree_digest = canonicalize_tree(source)
            manifest = SnapshotManifest(
                "campaign_one", "revision_two", "revision_one", 2,
                tree_digest, files, "0.3.0", "1.0.0", "a" * 64,
                "b" * 64, "token_pending",
            )
            store = FileSnapshotStore(root / "snapshots")
            store.put_if_absent(source, manifest)
            rows = [(
                "campaign_one", "revision_two", "token_pending", "pending",
                "revision_one", "2", tree_digest, "b" * 64,
                "intent_pending",
            )]

            with mock.patch.object(
                recover, "_query",
                side_effect=(rows, [("campaign_one", "revision_one")]),
            ), mock.patch.object(recover.subprocess, "run") as run:
                recover._clear_pending_publications("postgresql://internal", store)

            self.assertEqual((), store.inventory())
            sql = run.call_args.kwargs["input"]
            self.assertIn("DELETE FROM hosted_atlas_projection_checkpoint", sql)
            self.assertIn("revision_id='revision_one'", sql)
            self.assertNotIn("UPDATE hosted_campaign_head", sql)
            self.assertNotIn("status='quarantined'", sql)

    def test_operator_rejects_pending_candidate_not_based_on_current_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileSnapshotStore(Path(directory))
            rows = [(
                "campaign_one", "revision_two", "token_pending", "pending",
                "revision_other", "2", "a" * 64, "b" * 64,
                "intent_pending",
            )]
            with mock.patch.object(
                recover, "_query",
                side_effect=(rows, [("campaign_one", "revision_one")]),
            ), mock.patch.object(recover.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    RuntimeError, "pending_publication_parent_is_not_current_head"
                ):
                    recover._clear_pending_publications(
                        "postgresql://internal", store
                    )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
