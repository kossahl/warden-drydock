from __future__ import annotations

from .atlas_models import (
    ApprovedHistoryResult,
    AtlasHistoryEntry,
    AtlasNeighborhood,
    AtlasProjectionBundle,
    AtlasRecord,
    RecordLibraryResult,
    facet_counts,
)


def revision_ref(bundle: AtlasProjectionBundle) -> dict[str, object]:
    return {
        "revision_id": bundle.revision_id,
        "ordinal": bundle.ordinal,
        "tree_digest": bundle.tree_digest,
    }


def revision_binding(
    viewed: AtlasProjectionBundle, head: AtlasProjectionBundle
) -> dict[str, object]:
    if viewed.campaign_id != head.campaign_id:
        raise ValueError("unsafe_binding")
    return {
        "campaign_id": viewed.campaign_id,
        "viewed_revision": revision_ref(viewed),
        "head_revision": revision_ref(head),
    }


def raw_status(value) -> dict[str, object]:
    return {"classification": value.kind.value, "value": value.value}


def record_summary(record: AtlasRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "record_type": record.record_type,
        "name": record.name,
        "raw_status": raw_status(record.raw_status),
        "authority": record.authority.value,
        "summary": record.summary,
        "content_digest": record.content_digest,
    }


def facets_payload(result: RecordLibraryResult) -> dict[str, object]:
    return {
        "record_types": [item.__dict__ for item in result.type_facets],
        "authorities": [item.__dict__ for item in result.authority_facets],
        "statuses": [item.__dict__ for item in result.status_facets],
    }


def campaign_collection_contract(
    campaigns: tuple[
        tuple[AtlasProjectionBundle, AtlasProjectionBundle | None, str], ...
    ],
) -> dict[str, object]:
    return {
        "contract_name": "atlas_campaign_collection",
        "contract_version": 1,
        "campaigns": [
            {
                "campaign_id": head.campaign_id,
                "campaign_name": head.campaign_name,
                "adapter_id": head.adapter_id,
                "recovery_state": recovery_state,
                "head_revision": revision_ref(head),
                "projected_revision": revision_ref(projected)
                if projected is not None
                else None,
            }
            for head, projected, recovery_state in sorted(
                campaigns, key=lambda item: item[0].campaign_id
            )
        ],
    }


def overview_contract(
    viewed: AtlasProjectionBundle,
    head: AtlasProjectionBundle,
    *,
    approved_revision_count: int,
) -> dict[str, object]:
    if approved_revision_count < viewed.ordinal:
        raise ValueError("unsafe_binding")
    type_facets = facet_counts(item.record_type for item in viewed.records)
    authority_facets = facet_counts(item.authority.value for item in viewed.records)
    status_facets = facet_counts(
        item.raw_status.value
        if item.raw_status.kind.value == "known"
        else item.raw_status.kind.value
        for item in viewed.records
    )
    return {
        "contract_name": "atlas_overview",
        "contract_version": 1,
        "binding": revision_binding(viewed, head),
        "campaign_name": viewed.campaign_name,
        "adapter_id": viewed.adapter_id,
        "record_count": len(viewed.records),
        "edge_occurrence_count": len(viewed.edges),
        "approved_revision_count": approved_revision_count,
        "facets": {
            "record_types": [item.__dict__ for item in type_facets],
            "authorities": [item.__dict__ for item in authority_facets],
            "statuses": [item.__dict__ for item in status_facets],
        },
    }


def workflow_summary_contract(
    viewed: AtlasProjectionBundle,
    head: AtlasProjectionBundle,
    *,
    draft_generation_count: int,
    proposal_counts: dict[str, int],
    active_session: dict[str, object] | None = None,
) -> dict[str, object]:
    required = {"draft", "rejected", "conflict", "published", "quarantined"}
    if set(proposal_counts) != required or any(
        not isinstance(value, int) or value < 0 for value in proposal_counts.values()
    ):
        raise ValueError("unsafe_binding")
    if draft_generation_count < 0:
        raise ValueError("unsafe_binding")
    return {
        "contract_name": "atlas_workflow_summary",
        "contract_version": 1,
        "binding": revision_binding(viewed, head),
        "draft_generation_count": draft_generation_count,
        "proposal_counts": {key: proposal_counts[key] for key in sorted(required)},
        "active_session": active_session,
    }


def record_library_contract(
    result: RecordLibraryResult,
    viewed: AtlasProjectionBundle,
    head: AtlasProjectionBundle,
) -> dict[str, object]:
    query = result.query
    return {
        "contract_name": "atlas_record_library_result",
        "contract_version": 1,
        "binding": revision_binding(viewed, head),
        "normalized_query": query.query.casefold(),
        "filters": {
            "record_types": sorted(set(query.record_types)),
            "authorities": sorted({item.value for item in query.authorities}),
            "statuses": sorted(set(query.statuses)),
        },
        "limit": query.limit,
        "sort": "record_id",
        "total": result.total,
        "items": [record_summary(item) for item in result.items],
        "facets": facets_payload(result),
        "next_cursor": result.next_cursor,
        "previous_cursor": result.previous_cursor,
    }


def record_detail_contract(
    record: AtlasRecord,
    viewed: AtlasProjectionBundle,
    head: AtlasProjectionBundle,
) -> dict[str, object]:
    payload = record_summary(record)
    payload["content"] = record.content
    return {
        "contract_name": "atlas_record_detail",
        "contract_version": 1,
        "binding": revision_binding(viewed, head),
        "record": payload,
    }


def neighborhood_contract(
    value: AtlasNeighborhood,
    viewed: AtlasProjectionBundle,
    head: AtlasProjectionBundle,
) -> dict[str, object]:
    return {
        "contract_name": "atlas_depth_1_neighborhood",
        "contract_version": 1,
        "binding": revision_binding(viewed, head),
        "depth": 1,
        "limit": value.query.limit,
        "sort": "source_occurrence_edge",
        "focus": record_summary(value.focus),
        "neighbors": [record_summary(item) for item in value.neighbors],
        "edges": [
            {
                "edge_id": item.edge_id,
                "occurrence_order": item.occurrence_order,
                "source_record_id": item.source_record_id,
                "target_record_id": item.target_record_id,
                "relationship": item.relationship,
                "state": item.state,
                "context": item.context,
            }
            for item in value.edges
        ],
        "total_edges": value.total_edges,
        "next_cursor": value.next_cursor,
        "previous_cursor": value.previous_cursor,
    }


def _history_change(item) -> dict[str, object]:
    return {
        "record_id": item.record_id,
        "change_kind": item.change_kind.value,
        "link_revision_id": item.link_revision_id,
        "before_content_digest": item.before_content_digest,
        "after_content_digest": item.after_content_digest,
        "before_status": raw_status(item.before_status)
        if item.before_status
        else None,
        "after_status": raw_status(item.after_status) if item.after_status else None,
        "from_authority": item.from_authority.value
        if item.from_authority
        else None,
        "to_authority": item.to_authority.value if item.to_authority else None,
    }


def _history_entry(
    item: AtlasHistoryEntry, bundle_by_revision: dict[str, AtlasProjectionBundle]
) -> dict[str, object]:
    revision = bundle_by_revision[item.revision_id]
    affected: dict[str, str] = {}
    for change in item.changes:
        affected.setdefault(change.record_id, change.link_revision_id)
    return {
        "revision": revision_ref(revision),
        "parent_revision_id": item.parent_revision_id,
        "change_digest": item.change_digest,
        "affected_records": [
            {"record_id": record_id, "link_revision_id": affected[record_id]}
            for record_id in sorted(affected)
        ],
        "changes": [_history_change(change) for change in item.changes],
        "proposal_id": item.proposal_id,
        "proposal_version": item.proposal_version,
    }


def approved_history_contract(
    result: ApprovedHistoryResult,
    viewed: AtlasProjectionBundle,
    head: AtlasProjectionBundle,
    bundle_by_revision: dict[str, AtlasProjectionBundle],
) -> dict[str, object]:
    return {
        "contract_name": "atlas_approved_history_collection",
        "contract_version": 1,
        "binding": revision_binding(viewed, head),
        "subject_record_id": result.query.subject_record_id,
        "limit": result.query.limit,
        "sort": "revision_ordinal",
        "total": result.total,
        "entries": [
            _history_entry(item, bundle_by_revision) for item in result.entries
        ],
        "next_cursor": result.next_cursor,
        "previous_cursor": result.previous_cursor,
    }


def contextual_generation_contract(
    bundle: AtlasProjectionBundle,
    *,
    session_id: str | None = None,
    focus_record_id: str | None = None,
    focus_content_digest: str | None = None,
) -> dict[str, object]:
    if (focus_record_id is None) != (focus_content_digest is None):
        raise ValueError("unsafe_binding")
    return {
        "contract_name": "atlas_contextual_generation",
        "contract_version": 1,
        "campaign_id": bundle.campaign_id,
        "source_revision": revision_ref(bundle),
        "session_id": session_id,
        "focus_record_id": focus_record_id,
        "focus_content_digest": focus_content_digest,
    }
