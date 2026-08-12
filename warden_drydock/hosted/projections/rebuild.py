from __future__ import annotations

import hashlib
import json
import re

from warden_drydock.hosted.revisions.models import ProjectionBundle
from warden_drydock.hosted.revisions.models import SnapshotLineageError


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
        self._verify_campaign_lineage(manifest)
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

    def _verify_campaign_lineage(self, target) -> None:
        campaign = sorted(
            (
                manifest
                for manifest in self.store.inventory()
                if manifest.campaign_id == target.campaign_id
            ),
            key=lambda manifest: (manifest.ordinal, manifest.revision_id),
        )
        if target not in campaign:
            raise SnapshotLineageError("projection target is absent from inventory")
        expected_parent = None
        seen_revisions: set[str] = set()
        for expected_ordinal, manifest in enumerate(campaign, 1):
            if (
                manifest.ordinal != expected_ordinal
                or manifest.parent_revision != expected_parent
                or manifest.revision_id in seen_revisions
            ):
                raise SnapshotLineageError(
                    "projection inventory is not a unique linear lineage"
                )
            if (
                manifest.manifest_version != target.manifest_version
                or manifest.framework_version != target.framework_version
                or manifest.adapter_version != target.adapter_version
                or manifest.validation_contract_digest
                != target.validation_contract_digest
            ):
                raise SnapshotLineageError(
                    "projection inventory has incompatible manifest bindings"
                )
            if not self.workflow_repository.publication_eligible(manifest):
                raise SnapshotLineageError(
                    "projection inventory contains an ineligible revision"
                )
            seen_revisions.add(manifest.revision_id)
            expected_parent = manifest.revision_id

    def rebuild(self, manifest) -> ProjectionBundle:
        bundle = self.build(manifest)
        self.repository.stage(bundle)
        self.repository.swap(bundle.campaign_id, bundle.projection_digest)
        return bundle
