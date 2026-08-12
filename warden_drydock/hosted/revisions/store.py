from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from .canonical import MANIFEST_NAME, canonicalize_tree, decode_manifest, encode_manifest
from .models import SnapshotIntegrityError, SnapshotManifest


class FileSnapshotStore:
    """Content-addressed immutable full-tree store; it has no head authority."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.snapshots = self.root / "snapshots"
        self.quarantine = self.root / "quarantine"
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.quarantine.mkdir(parents=True, exist_ok=True)

    def put_if_absent(self, source: Path, manifest: SnapshotManifest) -> Path:
        files, digest = canonicalize_tree(source)
        if files != manifest.files or digest != manifest.tree_digest:
            raise SnapshotIntegrityError("published tree does not match manifest")
        digest_root = self.snapshots / digest
        digest_root.mkdir(exist_ok=True)
        target = digest_root / manifest.campaign_id / manifest.revision_id
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            existing = self.verify(digest, manifest.campaign_id, manifest.revision_id)
            if existing != manifest:
                raise SnapshotIntegrityError("content address has conflicting manifest")
            return target
        temporary = Path(tempfile.mkdtemp(prefix="publish-", dir=self.root))
        try:
            tree = temporary / "tree"
            shutil.copytree(source, tree)
            copied_files, copied_digest = canonicalize_tree(tree)
            if copied_files != manifest.files or copied_digest != manifest.tree_digest:
                raise SnapshotIntegrityError(
                    "copied snapshot tree does not match publication manifest"
                )
            (temporary / MANIFEST_NAME).write_bytes(encode_manifest(manifest))
            temporary.replace(target)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return target

    def verify(self, tree_digest: str, campaign_id: str, revision_id: str) -> SnapshotManifest:
        target = self.snapshots / tree_digest / campaign_id / revision_id
        manifest = decode_manifest((target / MANIFEST_NAME).read_bytes())
        files, digest = canonicalize_tree(target / "tree")
        if digest != tree_digest or digest != manifest.tree_digest or files != manifest.files:
            raise SnapshotIntegrityError("snapshot hash verification failed")
        return manifest

    def inventory(self) -> tuple[SnapshotManifest, ...]:
        return tuple(
            self.verify(digest_path.name, campaign_path.name, revision_path.name)
            for digest_path in sorted(self.snapshots.iterdir())
            if digest_path.is_dir()
            for campaign_path in sorted(digest_path.iterdir())
            if campaign_path.is_dir()
            for revision_path in sorted(campaign_path.iterdir())
            if revision_path.is_dir()
        )

    def quarantine_snapshot(
        self, tree_digest: str, campaign_id: str, revision_id: str, reason: str
    ) -> None:
        source = self.snapshots / tree_digest / campaign_id / revision_id
        target = self.quarantine / tree_digest / campaign_id / revision_id
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            (target / "quarantine-reason.txt").write_text(reason + "\n", encoding="utf-8")
