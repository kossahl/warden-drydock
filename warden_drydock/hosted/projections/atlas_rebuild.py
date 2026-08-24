from __future__ import annotations

from dataclasses import asdict
import json

from warden_drydock.standalone import _section_lines, frontmatter, parse_connections
from warden_drydock.hosted.revisions.models import (
    IntentStatus,
    SnapshotLineageError,
    SnapshotManifest,
)

from .atlas_models import (
    HISTORY_KIND_ORDER,
    AtlasEdge,
    AtlasHistoryChange,
    AtlasHistoryEntry,
    AtlasProjectionBundle,
    AtlasRecord,
    HistoryChangeKind,
    RawStatus,
    canonical_digest,
    content_digest,
    derive_authority,
    edge_id,
    normalize_content,
    require_public_id,
)


class AtlasProjectionRebuilder:
    """Builds immutable-revision Atlas rows from verified snapshot authority."""

    def __init__(
        self,
        store,
        repository,
        workflow_repository,
        *,
        proposal_provenance=None,
        projection_version: int = 1,
    ) -> None:
        self.store = store
        self.repository = repository
        self.workflow_repository = workflow_repository
        self.proposal_provenance = proposal_provenance or (lambda _campaign, _revision: None)
        self.projection_version = projection_version

    def _lineage_through(
        self, target: SnapshotManifest, *, allow_pending_target: bool = False
    ) -> tuple[SnapshotManifest, ...]:
        verified_target = self.store.verify(
            target.tree_digest, target.campaign_id, target.revision_id
        )
        if verified_target != target:
            raise SnapshotLineageError("projection target manifest binding mismatch")
        inventory = sorted(
            self.store.campaign_inventory(target.campaign_id),
            key=lambda item: (item.ordinal, item.revision_id),
        )
        through_target = tuple(item for item in inventory if item.ordinal <= target.ordinal)
        if target not in through_target:
            raise SnapshotLineageError("projection target is absent from inventory")
        expected_parent = None
        seen: set[str] = set()
        for expected_ordinal, manifest in enumerate(through_target, 1):
            if (
                manifest.ordinal != expected_ordinal
                or manifest.parent_revision != expected_parent
                or manifest.revision_id in seen
            ):
                raise SnapshotLineageError(
                    "projection inventory is not a unique linear lineage through target"
                )
            eligible = self.workflow_repository.publication_eligible(manifest)
            if not eligible and allow_pending_target and manifest == target:
                matches = self.workflow_repository.matching_intents(
                    manifest.publication_intent_token
                )
                eligible = (
                    len(matches) == 1
                    and matches[0].status is IntentStatus.PENDING
                    and matches[0].campaign_id == manifest.campaign_id
                    and matches[0].revision_id == manifest.revision_id
                    and matches[0].parent_revision == manifest.parent_revision
                    and matches[0].ordinal == manifest.ordinal
                    and matches[0].tree_digest == manifest.tree_digest
                    and matches[0].change_digest == manifest.change_digest
                )
            if not eligible:
                raise SnapshotLineageError(
                    "projection inventory contains an ineligible revision"
                )
            seen.add(manifest.revision_id)
            expected_parent = manifest.revision_id
        if through_target[-1] != target:
            raise SnapshotLineageError("projection target ordinal is ambiguous")
        return through_target

    def _tree(self, manifest: SnapshotManifest):
        return (
            self.store.snapshots
            / manifest.tree_digest
            / manifest.campaign_id
            / manifest.revision_id
            / "tree"
        )

    @staticmethod
    def _summary(content: str) -> str:
        lines = [
            line.strip()
            for _, line in _section_lines(content, "Summary")
            if line.strip() and not line.lstrip().startswith("<!--")
        ]
        return " ".join(lines) or "No summary recorded."

    def _parse_records(
        self, manifest: SnapshotManifest
    ) -> tuple[str, str, tuple[AtlasRecord, ...], tuple[AtlasEdge, ...]]:
        tree = self._tree(manifest)
        campaign_name = manifest.campaign_id
        adapter_id = "mothership"
        campaign_metadata = tree / ".drydock.json"
        if campaign_metadata.is_file():
            try:
                parsed = json.loads(
                    normalize_content(
                        campaign_metadata.read_bytes().decode("utf-8")
                    )
                )
                campaign_name = parsed.get("campaign_name", campaign_name)
                adapter_id = parsed.get("adapter", adapter_id)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                raise ValueError("snapshot campaign metadata is invalid") from exc
        if not isinstance(campaign_name, str) or not 1 <= len(campaign_name) <= 200:
            raise ValueError("snapshot campaign name is invalid")
        if adapter_id != "mothership":
            raise ValueError("snapshot adapter identity is unsupported")

        records: list[AtlasRecord] = []
        record_content: dict[str, tuple[str, object]] = {}
        for file_hash in manifest.files:
            relative = file_hash.relative_path
            if not relative.endswith(".md"):
                continue
            parts = relative.split("/")
            if parts[0] in {"templates", "docs", "00-drydock"}:
                continue
            try:
                content = normalize_content((tree / relative).read_bytes().decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ValueError("snapshot Markdown is not UTF-8") from exc
            metadata = frontmatter(content)
            record_id = metadata.get("id")
            if not record_id:
                continue
            if record_id in record_content:
                raise ValueError("snapshot contains duplicate record identifiers")
            status = RawStatus.from_value(metadata.get("status"))
            record = AtlasRecord(
                record_id=record_id,
                record_type=metadata.get("type", "unknown"),
                name=metadata.get("name") or record_id,
                raw_status=status,
                authority=derive_authority(status),
                summary=self._summary(content),
                content=content,
                content_digest=content_digest(content),
            )
            records.append(record)
            record_content[record_id] = (content, tree / relative)
        records.sort(key=lambda item: item.record_id)

        edges: list[AtlasEdge] = []
        known_records = {item.record_id for item in records}
        for record in records:
            content, path = record_content[record.record_id]
            parsed, errors = parse_connections(
                content, source_id=record.record_id, path=path
            )
            if errors:
                raise ValueError("snapshot contains malformed connection syntax")
            for occurrence_order, connection in enumerate(parsed, 1):
                if connection.target_id not in known_records:
                    raise ValueError("snapshot connection target is missing")
                edges.append(
                    AtlasEdge(
                        edge_id=edge_id(
                            manifest.revision_id,
                            record.record_id,
                            occurrence_order,
                        ),
                        occurrence_order=occurrence_order,
                        source_record_id=record.record_id,
                        target_record_id=connection.target_id,
                        relationship=connection.relationship,
                        state=connection.state,
                        context=connection.context,
                    )
                )
        edges.sort(
            key=lambda item: (
                item.source_record_id,
                item.occurrence_order,
                item.edge_id,
            )
        )
        return campaign_name, adapter_id, tuple(records), tuple(edges)

    @staticmethod
    def _history_changes(
        manifest: SnapshotManifest,
        records: tuple[AtlasRecord, ...],
        parent_records: tuple[AtlasRecord, ...],
    ) -> tuple[AtlasHistoryChange, ...]:
        current = {item.record_id: item for item in records}
        previous = {item.record_id: item for item in parent_records}
        changes: list[AtlasHistoryChange] = []
        for record_id in sorted(current.keys() | previous.keys()):
            before = previous.get(record_id)
            after = current.get(record_id)
            if before is None and after is not None:
                changes.append(
                    AtlasHistoryChange(
                        record_id,
                        HistoryChangeKind.ADDED,
                        manifest.revision_id,
                        None,
                        after.content_digest,
                        None,
                        after.raw_status,
                        None,
                        after.authority,
                    )
                )
                continue
            if before is not None and after is None:
                if manifest.parent_revision is None:
                    raise SnapshotLineageError("removed record has no parent revision")
                changes.append(
                    AtlasHistoryChange(
                        record_id,
                        HistoryChangeKind.REMOVED,
                        manifest.parent_revision,
                        before.content_digest,
                        None,
                        before.raw_status,
                        None,
                        before.authority,
                        None,
                    )
                )
                continue
            if before is None or after is None:
                continue
            common = dict(
                record_id=record_id,
                link_revision_id=manifest.revision_id,
                before_content_digest=before.content_digest,
                after_content_digest=after.content_digest,
                before_status=before.raw_status,
                after_status=after.raw_status,
                from_authority=before.authority,
                to_authority=after.authority,
            )
            if before.content_digest != after.content_digest:
                changes.append(
                    AtlasHistoryChange(
                        change_kind=HistoryChangeKind.CONTENT_CHANGED, **common
                    )
                )
            if (
                before.record_type,
                before.name,
                before.raw_status,
            ) != (
                after.record_type,
                after.name,
                after.raw_status,
            ):
                changes.append(
                    AtlasHistoryChange(
                        change_kind=HistoryChangeKind.METADATA_CHANGED, **common
                    )
                )
            if before.authority is not after.authority:
                changes.append(
                    AtlasHistoryChange(
                        change_kind=HistoryChangeKind.AUTHORITY_TRANSITION,
                        **common,
                    )
                )
        changes.sort(
            key=lambda item: (item.record_id, HISTORY_KIND_ORDER[item.change_kind])
        )
        return tuple(changes)

    @staticmethod
    def _bundle_digest_payload(
        campaign_id: str,
        revision_id: str,
        tree_digest: str,
        records: tuple[AtlasRecord, ...],
        edges: tuple[AtlasEdge, ...],
        history: AtlasHistoryEntry,
    ) -> dict[str, object]:
        return {
            "campaign_id": campaign_id,
            "edges": [asdict(item) for item in edges],
            "history": asdict(history),
            "records": [asdict(item) for item in records],
            "revision_id": revision_id,
            "tree_digest": tree_digest,
        }

    def build(
        self, manifest: SnapshotManifest, *, allow_pending_target: bool = False
    ) -> AtlasProjectionBundle:
        lineage = self._lineage_through(
            manifest, allow_pending_target=allow_pending_target
        )
        campaign_name, adapter_id, records, edges = self._parse_records(manifest)
        parent_records: tuple[AtlasRecord, ...] = ()
        if len(lineage) > 1:
            _, _, parent_records, _ = self._parse_records(lineage[-2])
        provenance = self.proposal_provenance(
            manifest.campaign_id, manifest.revision_id
        )
        proposal_id = proposal_version = None
        if provenance is not None:
            proposal_id, proposal_version = provenance
            require_public_id(proposal_id, "proposal_id")
            if not isinstance(proposal_version, int) or proposal_version < 1:
                raise ValueError("proposal provenance version is invalid")
        history = AtlasHistoryEntry(
            revision_id=manifest.revision_id,
            parent_revision_id=manifest.parent_revision,
            ordinal=manifest.ordinal,
            tree_digest=manifest.tree_digest,
            change_digest=manifest.change_digest,
            changes=self._history_changes(manifest, records, parent_records),
            proposal_id=proposal_id,
            proposal_version=proposal_version,
        )
        digest = canonical_digest(
            self._bundle_digest_payload(
                manifest.campaign_id,
                manifest.revision_id,
                manifest.tree_digest,
                records,
                edges,
                history,
            )
        )
        return AtlasProjectionBundle(
            campaign_id=manifest.campaign_id,
            campaign_name=campaign_name,
            adapter_id=adapter_id,
            revision_id=manifest.revision_id,
            parent_revision_id=manifest.parent_revision,
            ordinal=manifest.ordinal,
            tree_digest=manifest.tree_digest,
            projection_version=self.projection_version,
            records=records,
            edges=edges,
            history_entry=history,
            projection_digest=digest,
        )

    def rebuild(self, manifest: SnapshotManifest) -> AtlasProjectionBundle:
        bundle = self.build(manifest)
        self.repository.replace(bundle)
        return bundle

    def rebuild_pending(self, manifest: SnapshotManifest) -> AtlasProjectionBundle:
        """Persist a candidate projection before the publication head CAS."""
        bundle = self.build(manifest, allow_pending_target=True)
        self.repository.replace(bundle)
        return bundle

    def rebuild_inventory(self, campaign_id: str) -> tuple[AtlasProjectionBundle, ...]:
        manifests = sorted(
            self.store.campaign_inventory(campaign_id),
            key=lambda item: (item.ordinal, item.revision_id),
        )
        return tuple(self.rebuild(manifest) for manifest in manifests)
