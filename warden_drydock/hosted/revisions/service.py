from __future__ import annotations

from pathlib import Path
from typing import Callable

from .canonical import canonicalize_tree
from .models import (
    IntentStatus,
    PublicationIntent,
    PublicationIntentError,
    SnapshotIntegrityError,
    SnapshotLineageError,
    SnapshotManifest,
    StaleHeadError,
)
from .store import FileSnapshotStore


class RevisionService:
    def __init__(self, store: FileSnapshotStore, repository) -> None:
        self.store = store
        self.repository = repository

    def publish(
        self, source: Path, intent: PublicationIntent, *, framework_version: str,
        adapter_version: str, validation_contract_digest: str,
        before_finalize: Callable[[SnapshotManifest], object] | None = None,
        rollback: Callable[[SnapshotManifest], object] | None = None,
    ) -> SnapshotManifest:
        files, tree_digest = canonicalize_tree(source)
        if tree_digest != intent.tree_digest:
            raise SnapshotIntegrityError("publication intent tree digest mismatch")
        manifest = SnapshotManifest(
            campaign_id=intent.campaign_id, revision_id=intent.revision_id,
            parent_revision=intent.parent_revision, ordinal=intent.ordinal,
            tree_digest=tree_digest, files=files, framework_version=framework_version,
            adapter_version=adapter_version,
            validation_contract_digest=validation_contract_digest,
            change_digest=intent.change_digest,
            publication_intent_token=intent.intent_token,
        )
        self.repository.add_intent(intent)
        self.store.put_if_absent(source, manifest)
        try:
            self.reconcile_manifest(manifest, before_finalize=before_finalize)
        except Exception:
            if rollback is not None:
                try:
                    rollback(manifest)
                except Exception:
                    pass
            raise
        return manifest

    def reconcile_manifest(
        self, manifest: SnapshotManifest, *,
        before_finalize: Callable[[SnapshotManifest], object] | None = None,
    ) -> bool:
        stored_manifest = self.store.verify(
            manifest.tree_digest, manifest.campaign_id, manifest.revision_id
        )
        if stored_manifest != manifest:
            raise SnapshotIntegrityError(
                "stored snapshot manifest does not match reconciliation input"
            )
        matches = self.repository.matching_intents(manifest.publication_intent_token)
        exact = tuple(intent for intent in matches if self._matches(intent, manifest))
        if len(matches) != 1 or len(exact) != 1:
            self.store.quarantine_snapshot(
                manifest.tree_digest,
                manifest.campaign_id,
                manifest.revision_id,
                "missing or ambiguous publication intent",
            )
            for intent in matches:
                if intent.status is not IntentStatus.FINALIZED:
                    self.repository.quarantine_intent(intent.intent_id)
            raise PublicationIntentError("snapshot publication intent is not uniquely matched")
        if before_finalize is not None:
            try:
                before_finalize(manifest)
            except Exception:
                self.store.quarantine_snapshot(
                    manifest.tree_digest,
                    manifest.campaign_id,
                    manifest.revision_id,
                    "publication preparation failed",
                )
                self.repository.quarantine_intent(exact[0].intent_id)
                raise
        try:
            return self.repository.finalize_head(exact[0])
        except (PublicationIntentError, StaleHeadError) as exc:
            reason = (
                "stale campaign head"
                if isinstance(exc, StaleHeadError)
                else "publication intent changed during finalization"
            )
            self.store.quarantine_snapshot(
                manifest.tree_digest,
                manifest.campaign_id,
                manifest.revision_id,
                reason,
            )
            self.repository.quarantine_intent(exact[0].intent_id)
            raise

    @staticmethod
    def _matches(intent: PublicationIntent, manifest: SnapshotManifest) -> bool:
        return (
            intent.campaign_id == manifest.campaign_id
            and intent.revision_id == manifest.revision_id
            and intent.parent_revision == manifest.parent_revision
            and intent.ordinal == manifest.ordinal
            and intent.tree_digest == manifest.tree_digest
            and intent.change_digest == manifest.change_digest
        )

    def verify_linear_inventory(self) -> tuple[SnapshotManifest, ...]:
        manifests = self.store.inventory()
        by_campaign: dict[str, list[SnapshotManifest]] = {}
        for manifest in manifests:
            if not self.repository.publication_eligible(manifest):
                raise PublicationIntentError(
                    "snapshot is not eligible for lineage or projection use"
                )
            by_campaign.setdefault(manifest.campaign_id, []).append(manifest)
        verified: list[SnapshotManifest] = []
        for campaign in sorted(by_campaign):
            ordered = sorted(by_campaign[campaign], key=lambda item: item.ordinal)
            expected_parent = None
            for expected_ordinal, manifest in enumerate(ordered, 1):
                if manifest.ordinal != expected_ordinal or manifest.parent_revision != expected_parent:
                    raise SnapshotLineageError("snapshot inventory is not a unique linear lineage")
                expected_parent = manifest.revision_id
                verified.append(manifest)
        return tuple(verified)
