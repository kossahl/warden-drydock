from __future__ import annotations

import hashlib
import json
import re

from warden_drydock.hosted.revisions.models import ProjectionBundle


_ID = re.compile(r"(?m)^id:\s*[\"']?([a-z][a-z0-9-]*)[\"']?\s*$")


class ProjectionRebuilder:
    """Verifies snapshots first, then atomically swaps deterministic shadows."""

    def __init__(
        self, store, repository, workflow_repository, projection_version: int = 1
    ) -> None:
        self.store = store
        self.repository = repository
        self.workflow_repository = workflow_repository
        self.projection_version = projection_version

    def build(self, manifest) -> ProjectionBundle:
        verified = self.store.verify(
            manifest.tree_digest, manifest.campaign_id, manifest.revision_id
        )
        if not self.workflow_repository.publication_eligible(verified):
            raise ValueError("snapshot is not eligible for projection rebuild")
        records: list[tuple[str, str, str]] = []
        tree = (
            self.store.snapshots
            / verified.tree_digest
            / verified.campaign_id
            / verified.revision_id
            / "tree"
        )
        for file_hash in verified.files:
            if not file_hash.relative_path.endswith(".md"):
                continue
            body = (tree / file_hash.relative_path).read_text(encoding="utf-8")
            match = _ID.search(body)
            if match:
                records.append((match.group(1), file_hash.relative_path, file_hash.sha256))
        records.sort()
        canonical = json.dumps(records, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return ProjectionBundle(
            verified.campaign_id, verified.revision_id, self.projection_version,
            len(records), hashlib.sha256(canonical).hexdigest(), tuple(records),
        )

    def rebuild(self, manifest) -> ProjectionBundle:
        bundle = self.build(manifest)
        self.repository.stage(bundle)
        self.repository.swap(bundle.campaign_id, bundle.projection_digest)
        return bundle
