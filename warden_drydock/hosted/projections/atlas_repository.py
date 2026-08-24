from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from typing import Iterator

from .atlas_models import (
    ApprovedHistoryQuery,
    ApprovedHistoryResult,
    AtlasEdge,
    AtlasHistoryChange,
    AtlasHistoryEntry,
    AtlasNeighborhood,
    AtlasProjectionBundle,
    AtlasRecord,
    Authority,
    HistoryChangeKind,
    NeighborhoodQuery,
    RawStatus,
    RecordLibraryQuery,
    RecordLibraryResult,
    StatusKind,
    decode_cursor,
    encode_cursor,
    facet_counts,
    require_domain_id,
    require_public_id,
)


def _status_facet(record: AtlasRecord) -> str:
    if record.raw_status.kind is StatusKind.MISSING:
        return "missing"
    if record.raw_status.kind is StatusKind.UNKNOWN:
        return "unknown"
    return record.raw_status.value or "missing"


class InMemoryAtlasProjectionRepository:
    def __init__(self) -> None:
        self.bundles: dict[tuple[str, str], AtlasProjectionBundle] = {}
        self.operational: dict[str, object] = {}

    def replace(self, bundle: AtlasProjectionBundle) -> None:
        self.bundles[(bundle.campaign_id, bundle.revision_id)] = bundle

    def get(self, campaign_id: str, revision_id: str) -> AtlasProjectionBundle:
        try:
            return self.bundles[(campaign_id, revision_id)]
        except KeyError as exc:
            raise KeyError("atlas_projection_not_found") from exc

    def list(self, campaign_id: str | None = None) -> tuple[AtlasProjectionBundle, ...]:
        values = self.bundles.values()
        if campaign_id is not None:
            values = (
                item for item in values if item.campaign_id == campaign_id
            )
        return tuple(
            sorted(values, key=lambda item: (item.campaign_id, item.ordinal))
        )


class PostgresAtlasProjectionRepository:
    """Transactional PostgreSQL storage for rebuildable immutable revisions."""

    def __init__(self, connect) -> None:
        self._connect = connect

    @contextmanager
    def _transaction(self) -> Iterator[object]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def replace(self, bundle: AtlasProjectionBundle) -> None:
        with self._transaction() as connection, connection.cursor() as cursor:
            lock_key = f"{bundle.campaign_id}:{bundle.revision_id}"
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 6))",
                (lock_key,),
            )
            cursor.execute(
                "DELETE FROM hosted_atlas_projection_checkpoint "
                "WHERE campaign_id=%s AND revision_id=%s",
                (bundle.campaign_id, bundle.revision_id),
            )
            cursor.execute(
                "INSERT INTO hosted_atlas_projection_checkpoint("
                "campaign_id,revision_id,parent_revision_id,ordinal,tree_digest,"
                "campaign_name,adapter_id,projection_version,record_count,edge_count,"
                "history_change_count,projection_digest) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    bundle.campaign_id,
                    bundle.revision_id,
                    bundle.parent_revision_id,
                    bundle.ordinal,
                    bundle.tree_digest,
                    bundle.campaign_name,
                    bundle.adapter_id,
                    bundle.projection_version,
                    len(bundle.records),
                    len(bundle.edges),
                    len(bundle.history_entry.changes),
                    bundle.projection_digest,
                ),
            )
            for record in bundle.records:
                cursor.execute(
                    "INSERT INTO hosted_atlas_record("
                    "campaign_id,revision_id,record_id,record_type,name,raw_status_kind,"
                    "raw_status_value,authority,summary,normalized_content,content_digest) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        bundle.campaign_id,
                        bundle.revision_id,
                        record.record_id,
                        record.record_type,
                        record.name,
                        record.raw_status.kind.value,
                        record.raw_status.value,
                        record.authority.value,
                        record.summary,
                        record.content,
                        record.content_digest,
                    ),
                )
            for edge in bundle.edges:
                cursor.execute(
                    "INSERT INTO hosted_atlas_edge("
                    "campaign_id,revision_id,edge_id,occurrence_order,source_record_id,"
                    "target_record_id,relationship,state,context) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        bundle.campaign_id,
                        bundle.revision_id,
                        edge.edge_id,
                        edge.occurrence_order,
                        edge.source_record_id,
                        edge.target_record_id,
                        edge.relationship,
                        edge.state,
                        edge.context,
                    ),
                )
            history = bundle.history_entry
            cursor.execute(
                "INSERT INTO hosted_atlas_history_entry("
                "campaign_id,revision_id,parent_revision_id,ordinal,tree_digest,"
                "change_digest,proposal_id,proposal_version) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    bundle.campaign_id,
                    history.revision_id,
                    history.parent_revision_id,
                    history.ordinal,
                    history.tree_digest,
                    history.change_digest,
                    history.proposal_id,
                    history.proposal_version,
                ),
            )
            for change_order, change in enumerate(history.changes, 1):
                cursor.execute(
                    "INSERT INTO hosted_atlas_history_change("
                    "campaign_id,revision_id,change_order,record_id,change_kind,"
                    "link_revision_id,before_content_digest,after_content_digest,"
                    "before_status_kind,before_status_value,after_status_kind,"
                    "after_status_value,from_authority,to_authority) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        bundle.campaign_id,
                        bundle.revision_id,
                        change_order,
                        change.record_id,
                        change.change_kind.value,
                        change.link_revision_id,
                        change.before_content_digest,
                        change.after_content_digest,
                        change.before_status.kind.value
                        if change.before_status
                        else None,
                        change.before_status.value if change.before_status else None,
                        change.after_status.kind.value
                        if change.after_status
                        else None,
                        change.after_status.value if change.after_status else None,
                        change.from_authority.value
                        if change.from_authority
                        else None,
                        change.to_authority.value if change.to_authority else None,
                    ),
                )
            cursor.execute(
                "SELECT "
                "(SELECT count(*) FROM hosted_atlas_record WHERE campaign_id=%s AND revision_id=%s),"
                "(SELECT count(*) FROM hosted_atlas_edge WHERE campaign_id=%s AND revision_id=%s),"
                "(SELECT count(*) FROM hosted_atlas_history_change WHERE campaign_id=%s AND revision_id=%s),"
                "projection_digest FROM hosted_atlas_projection_checkpoint "
                "WHERE campaign_id=%s AND revision_id=%s",
                (
                    bundle.campaign_id,
                    bundle.revision_id,
                    bundle.campaign_id,
                    bundle.revision_id,
                    bundle.campaign_id,
                    bundle.revision_id,
                    bundle.campaign_id,
                    bundle.revision_id,
                ),
            )
            row = cursor.fetchone()
            expected = (
                len(bundle.records),
                len(bundle.edges),
                len(history.changes),
                bundle.projection_digest,
            )
            if row != expected:
                raise ValueError("persisted Atlas projection verification failed")
            cursor.execute(
                "SELECT record_id,record_type,name,raw_status_kind,raw_status_value,"
                "authority,summary,normalized_content,content_digest "
                "FROM hosted_atlas_record WHERE campaign_id=%s AND revision_id=%s "
                "ORDER BY record_id",
                (bundle.campaign_id, bundle.revision_id),
            )
            persisted_records = tuple(cursor.fetchall())
            expected_records = tuple(
                (
                    item.record_id,
                    item.record_type,
                    item.name,
                    item.raw_status.kind.value,
                    item.raw_status.value,
                    item.authority.value,
                    item.summary,
                    item.content,
                    item.content_digest,
                )
                for item in bundle.records
            )
            cursor.execute(
                "SELECT edge_id,occurrence_order,source_record_id,target_record_id,"
                "relationship,state,context FROM hosted_atlas_edge "
                "WHERE campaign_id=%s AND revision_id=%s "
                "ORDER BY source_record_id,occurrence_order,edge_id",
                (bundle.campaign_id, bundle.revision_id),
            )
            persisted_edges = tuple(cursor.fetchall())
            expected_edges = tuple(
                (
                    item.edge_id,
                    item.occurrence_order,
                    item.source_record_id,
                    item.target_record_id,
                    item.relationship,
                    item.state,
                    item.context,
                )
                for item in bundle.edges
            )
            if persisted_records != expected_records or persisted_edges != expected_edges:
                raise ValueError("persisted Atlas projection content mismatch")

    @staticmethod
    def _raw_status(kind: str | None, value: str | None) -> RawStatus | None:
        return RawStatus(StatusKind(kind), value) if kind is not None else None

    def get(self, campaign_id: str, revision_id: str) -> AtlasProjectionBundle:
        require_public_id(campaign_id, "campaign_id")
        require_public_id(revision_id, "revision_id")
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT parent_revision_id,ordinal,tree_digest,campaign_name,adapter_id,"
                "projection_version,projection_digest FROM hosted_atlas_projection_checkpoint "
                "WHERE campaign_id=%s AND revision_id=%s",
                (campaign_id, revision_id),
            )
            checkpoint = cursor.fetchone()
            if checkpoint is None:
                raise KeyError("atlas_projection_not_found")
            cursor.execute(
                "SELECT record_id,record_type,name,raw_status_kind,raw_status_value,"
                "authority,summary,normalized_content,content_digest "
                "FROM hosted_atlas_record WHERE campaign_id=%s AND revision_id=%s "
                "ORDER BY record_id",
                (campaign_id, revision_id),
            )
            records = tuple(
                AtlasRecord(
                    row[0],
                    row[1],
                    row[2],
                    RawStatus(StatusKind(row[3]), row[4]),
                    Authority(row[5]),
                    row[6],
                    row[7],
                    row[8],
                )
                for row in cursor.fetchall()
            )
            cursor.execute(
                "SELECT edge_id,occurrence_order,source_record_id,target_record_id,"
                "relationship,state,context FROM hosted_atlas_edge "
                "WHERE campaign_id=%s AND revision_id=%s "
                "ORDER BY source_record_id,occurrence_order,edge_id",
                (campaign_id, revision_id),
            )
            edges = tuple(AtlasEdge(*row) for row in cursor.fetchall())
            cursor.execute(
                "SELECT parent_revision_id,ordinal,tree_digest,change_digest,"
                "proposal_id,proposal_version FROM hosted_atlas_history_entry "
                "WHERE campaign_id=%s AND revision_id=%s",
                (campaign_id, revision_id),
            )
            history_row = cursor.fetchone()
            if history_row is None:
                raise ValueError("Atlas history entry is missing")
            cursor.execute(
                "SELECT record_id,change_kind,link_revision_id,before_content_digest,"
                "after_content_digest,before_status_kind,before_status_value,"
                "after_status_kind,after_status_value,from_authority,to_authority "
                "FROM hosted_atlas_history_change WHERE campaign_id=%s AND revision_id=%s "
                "ORDER BY change_order",
                (campaign_id, revision_id),
            )
            changes = tuple(
                AtlasHistoryChange(
                    record_id=row[0],
                    change_kind=HistoryChangeKind(row[1]),
                    link_revision_id=row[2],
                    before_content_digest=row[3],
                    after_content_digest=row[4],
                    before_status=self._raw_status(row[5], row[6]),
                    after_status=self._raw_status(row[7], row[8]),
                    from_authority=Authority(row[9]) if row[9] else None,
                    to_authority=Authority(row[10]) if row[10] else None,
                )
                for row in cursor.fetchall()
            )
        history = AtlasHistoryEntry(
            revision_id,
            history_row[0],
            history_row[1],
            history_row[2],
            history_row[3],
            changes,
            history_row[4],
            history_row[5],
        )
        return AtlasProjectionBundle(
            campaign_id,
            checkpoint[3],
            checkpoint[4],
            revision_id,
            checkpoint[0],
            checkpoint[1],
            checkpoint[2],
            checkpoint[5],
            records,
            edges,
            history,
            checkpoint[6],
        )

    def list(self, campaign_id: str | None = None) -> tuple[AtlasProjectionBundle, ...]:
        if campaign_id is not None:
            require_public_id(campaign_id, "campaign_id")
        with self._transaction() as connection, connection.cursor() as cursor:
            if campaign_id is None:
                cursor.execute(
                    "SELECT campaign_id,revision_id FROM hosted_atlas_projection_checkpoint "
                    "ORDER BY campaign_id,ordinal"
                )
            else:
                cursor.execute(
                    "SELECT campaign_id,revision_id FROM hosted_atlas_projection_checkpoint "
                    "WHERE campaign_id=%s ORDER BY ordinal",
                    (campaign_id,),
                )
            identities = tuple(cursor.fetchall())
        return tuple(self.get(*identity) for identity in identities)

    def proposal_provenance(
        self, campaign_id: str, revision_id: str
    ) -> tuple[str, int] | None:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT proposal_id,version FROM hosted_proposal_version "
                "WHERE campaign_id=%s AND published_revision_id=%s AND status='published' "
                "ORDER BY proposal_id,version",
                (campaign_id, revision_id),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise ValueError("published revision proposal provenance is ambiguous")
        return tuple(rows[0]) if rows else None


class AtlasQueryService:
    """Transport-facing deterministic reads over one immutable revision."""

    def __init__(self, repository) -> None:
        self.repository = repository

    @staticmethod
    def _record_binding(
        query: RecordLibraryQuery, direction: str, boundary: str
    ) -> dict[str, object]:
        return {
            "authorities": sorted({item.value for item in query.authorities}),
            "boundary_record_id": boundary,
            "campaign_id": query.campaign_id,
            "direction": direction,
            "kind": "record_library",
            "limit": query.limit,
            "normalized_query": query.query.casefold(),
            "record_types": sorted(set(query.record_types)),
            "revision_id": query.revision_id,
            "sort": "record_id",
            "statuses": sorted(set(query.statuses)),
            "tree_digest": query.tree_digest,
        }

    def record_library(self, query: RecordLibraryQuery) -> RecordLibraryResult:
        bundle = self.repository.get(query.campaign_id, query.revision_id)
        if bundle.tree_digest != query.tree_digest:
            raise ValueError("invalid_cursor_binding")
        normalized_query = query.query.casefold()
        type_filters = set(query.record_types)
        authority_filters = {item.value for item in query.authorities}
        status_filters = set(query.statuses)
        records = tuple(sorted(bundle.records, key=lambda item: item.record_id))
        filtered = tuple(
            record
            for record in records
            if (
                not normalized_query
                or any(
                    normalized_query in value.casefold()
                    for value in (
                        record.record_id, record.name, record.summary, record.content
                    )
                )
            )
            and (not type_filters or record.record_type in type_filters)
            and (not authority_filters or record.authority.value in authority_filters)
            and (not status_filters or _status_facet(record) in status_filters)
        )
        start, end = 0, min(query.limit, len(filtered))
        if query.cursor is not None:
            binding = decode_cursor(query.cursor)
            direction = binding.get("direction")
            boundary = binding.get("boundary_record_id")
            if not isinstance(boundary, str) or direction not in {"forward", "backward"}:
                raise ValueError("invalid_cursor_binding")
            expected = self._record_binding(query, direction, boundary)
            if binding != expected:
                raise ValueError("invalid_cursor_binding")
            identifiers = [item.record_id for item in filtered]
            try:
                boundary_index = identifiers.index(boundary)
            except ValueError as exc:
                raise ValueError("invalid_cursor_binding") from exc
            if direction == "forward":
                start = boundary_index + 1
                end = min(start + query.limit, len(filtered))
            else:
                end = boundary_index
                start = max(0, end - query.limit)
        items = filtered[start:end]
        next_cursor = (
            encode_cursor(self._record_binding(query, "forward", items[-1].record_id))
            if items and end < len(filtered)
            else None
        )
        previous_cursor = (
            encode_cursor(self._record_binding(query, "backward", items[0].record_id))
            if items and start > 0
            else None
        )
        return RecordLibraryResult(
            query=replace(query, cursor=None),
            items=items,
            total=len(filtered),
            type_facets=facet_counts(item.record_type for item in records),
            authority_facets=facet_counts(item.authority.value for item in records),
            status_facets=facet_counts(_status_facet(item) for item in records),
            next_cursor=next_cursor,
            previous_cursor=previous_cursor,
        )

    def record_detail(
        self, campaign_id: str, revision_id: str, record_id: str
    ) -> AtlasRecord:
        require_domain_id(record_id, "record_id")
        bundle = self.repository.get(campaign_id, revision_id)
        try:
            return next(item for item in bundle.records if item.record_id == record_id)
        except StopIteration as exc:
            raise KeyError("atlas_record_not_found") from exc

    @staticmethod
    def _edge_binding(
        bundle: AtlasProjectionBundle,
        focus_record_id: str,
        limit: int,
        direction: str,
        boundary: str,
    ) -> dict[str, object]:
        return {
            "boundary_edge_id": boundary,
            "campaign_id": bundle.campaign_id,
            "direction": direction,
            "focus_record_id": focus_record_id,
            "kind": "depth_1_neighborhood",
            "limit": limit,
            "revision_id": bundle.revision_id,
            "sort": "source_occurrence_edge",
            "tree_digest": bundle.tree_digest,
        }

    def neighborhood(self, query: NeighborhoodQuery) -> AtlasNeighborhood:
        focus = self.record_detail(
            query.campaign_id, query.revision_id, query.focus_record_id
        )
        bundle = self.repository.get(query.campaign_id, query.revision_id)
        if bundle.tree_digest != query.tree_digest:
            raise ValueError("invalid_cursor_binding")
        all_edges = tuple(
            item
            for item in bundle.edges
            if query.focus_record_id in {item.source_record_id, item.target_record_id}
        )
        start, end = 0, min(query.limit, len(all_edges))
        if query.cursor is not None:
            binding = decode_cursor(query.cursor)
            direction = binding.get("direction")
            boundary = binding.get("boundary_edge_id")
            if not isinstance(boundary, str) or direction not in {"forward", "backward"}:
                raise ValueError("invalid_cursor_binding")
            if binding != self._edge_binding(
                bundle, query.focus_record_id, query.limit, direction, boundary
            ):
                raise ValueError("invalid_cursor_binding")
            edge_ids = [item.edge_id for item in all_edges]
            try:
                index = edge_ids.index(boundary)
            except ValueError as exc:
                raise ValueError("invalid_cursor_binding") from exc
            if direction == "forward":
                start, end = index + 1, min(index + 1 + query.limit, len(all_edges))
            else:
                start, end = max(0, index - query.limit), index
        edges = all_edges[start:end]
        neighbor_ids = sorted(
            {
                edge.target_record_id
                if edge.source_record_id == query.focus_record_id
                else edge.source_record_id
                for edge in edges
            }
        )
        by_id = {item.record_id: item for item in bundle.records}
        return AtlasNeighborhood(
            query=replace(query, cursor=None),
            focus=focus,
            neighbors=tuple(by_id[item] for item in neighbor_ids),
            edges=edges,
            total_edges=len(all_edges),
            next_cursor=(
                encode_cursor(
                    self._edge_binding(
                        bundle,
                        query.focus_record_id,
                        query.limit,
                        "forward",
                        edges[-1].edge_id,
                    )
                )
                if edges and end < len(all_edges)
                else None
            ),
            previous_cursor=(
                encode_cursor(
                    self._edge_binding(
                        bundle,
                        query.focus_record_id,
                        query.limit,
                        "backward",
                        edges[0].edge_id,
                    )
                )
                if edges and start > 0
                else None
            ),
        )

    @staticmethod
    def _history_binding(
        query: ApprovedHistoryQuery, direction: str, boundary: int
    ) -> dict[str, object]:
        return {
            "boundary_ordinal": boundary,
            "campaign_id": query.campaign_id,
            "direction": direction,
            "kind": "approved_history",
            "limit": query.limit,
            "revision_id": query.revision_id,
            "sort": "revision_ordinal",
            "subject_record_id": query.subject_record_id,
            "tree_digest": query.tree_digest,
        }

    def approved_history(
        self, query: ApprovedHistoryQuery
    ) -> ApprovedHistoryResult:
        target = self.repository.get(query.campaign_id, query.revision_id)
        if target.tree_digest != query.tree_digest:
            raise ValueError("invalid_cursor_binding")
        entries = []
        for bundle in self.repository.list(query.campaign_id):
            if bundle.ordinal > target.ordinal:
                continue
            entry = bundle.history_entry
            if query.subject_record_id is not None:
                changes = tuple(
                    item
                    for item in entry.changes
                    if item.record_id == query.subject_record_id
                )
                if not changes:
                    continue
                entry = replace(entry, changes=changes)
            entries.append(entry)
        filtered = tuple(sorted(entries, key=lambda item: item.ordinal))
        start, end = 0, min(query.limit, len(filtered))
        if query.cursor is not None:
            binding = decode_cursor(query.cursor)
            direction = binding.get("direction")
            boundary = binding.get("boundary_ordinal")
            if (
                not isinstance(boundary, int)
                or isinstance(boundary, bool)
                or direction not in {"forward", "backward"}
            ):
                raise ValueError("invalid_cursor_binding")
            if binding != self._history_binding(query, direction, boundary):
                raise ValueError("invalid_cursor_binding")
            ordinals = [item.ordinal for item in filtered]
            try:
                index = ordinals.index(boundary)
            except ValueError as exc:
                raise ValueError("invalid_cursor_binding") from exc
            if direction == "forward":
                start, end = index + 1, min(index + 1 + query.limit, len(filtered))
            else:
                start, end = max(0, index - query.limit), index
        page = filtered[start:end]
        return ApprovedHistoryResult(
            query=replace(query, cursor=None),
            entries=page,
            total=len(filtered),
            next_cursor=(
                encode_cursor(
                    self._history_binding(query, "forward", page[-1].ordinal)
                )
                if page and end < len(filtered)
                else None
            ),
            previous_cursor=(
                encode_cursor(
                    self._history_binding(query, "backward", page[0].ordinal)
                )
                if page and start > 0
                else None
            ),
        )
