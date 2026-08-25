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
        quarantined = (
            self.quarantine
            / digest
            / manifest.campaign_id
            / manifest.revision_id
        )
        if quarantined.exists():
            raise SnapshotIntegrityError(
                "snapshot identity has an existing quarantine tombstone"
            )
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
        if (
            manifest.campaign_id != campaign_id
            or manifest.revision_id != revision_id
        ):
            raise SnapshotIntegrityError(
                "snapshot storage identity does not match manifest identity"
            )
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

    def campaign_inventory(self, campaign_id: str) -> tuple[SnapshotManifest, ...]:
        manifests: list[SnapshotManifest] = []
        for digest_path in sorted(self.snapshots.iterdir()):
            campaign_path = digest_path / campaign_id
            if not digest_path.is_dir() or not campaign_path.is_dir():
                continue
            for revision_path in sorted(campaign_path.iterdir()):
                if revision_path.is_dir():
                    manifests.append(
                        self.verify(
                            digest_path.name, campaign_id, revision_path.name
                        )
                    )
        return tuple(manifests)

    def quarantine_snapshot(
        self, tree_digest: str, campaign_id: str, revision_id: str, reason: str
    ) -> None:
        source = self.snapshots / tree_digest / campaign_id / revision_id
        target = self.quarantine / tree_digest / campaign_id / revision_id
        if source.exists() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            (target / "quarantine-reason.txt").write_text(reason + "\n", encoding="utf-8")
        elif source.exists():
            shutil.rmtree(source)

    def discard_unpublished_snapshot(self, manifest: SnapshotManifest) -> None:
        """Remove one verified, non-canonical publication candidate for exact retry."""
        if self.verify(
            manifest.tree_digest, manifest.campaign_id, manifest.revision_id
        ) != manifest:
            raise SnapshotIntegrityError("unpublished snapshot binding mismatch")
        target = (
            self.snapshots / manifest.tree_digest / manifest.campaign_id
            / manifest.revision_id
        )
        shutil.rmtree(target)
        for parent in (target.parent, target.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
