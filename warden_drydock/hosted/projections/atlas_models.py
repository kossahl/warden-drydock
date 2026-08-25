from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import base64
import hashlib
import hmac
import json
import re
from typing import Iterable


_DOMAIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PUBLIC_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
KNOWN_STATUSES = frozenset(
    {"idea", "draft", "review", "canon", "revealed", "archived", "accepted"}
)


def normalize_content(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def content_digest(value: str) -> str:
    return hashlib.sha256(normalize_content(value).encode("utf-8")).hexdigest()


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_domain_id(value: str, field: str) -> None:
    if not 1 <= len(value) <= 80 or _DOMAIN_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is not a safe domain identifier")


def require_public_id(value: str, field: str) -> None:
    if not 3 <= len(value) <= 80 or _PUBLIC_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is not a safe public identifier")


def require_digest(value: str, field: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} is not a lowercase SHA-256 digest")


class Authority(str, Enum):
    PREPARATION = "preparation"
    CANON = "canon"
    REVEALED = "revealed"


class StatusKind(str, Enum):
    KNOWN = "known"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RawStatus:
    kind: StatusKind
    value: str | None

    def __post_init__(self) -> None:
        if self.kind is StatusKind.MISSING:
            if self.value is not None:
                raise ValueError("missing raw status cannot contain a value")
            return
        if self.value is None or not 1 <= len(self.value) <= 80:
            raise ValueError("raw status value is missing or too long")
        if any(ord(character) < 32 for character in self.value):
            raise ValueError("raw status contains a control character")
        if self.kind is StatusKind.KNOWN and self.value not in KNOWN_STATUSES:
            raise ValueError("known raw status is not in the closed vocabulary")
        if self.kind is StatusKind.UNKNOWN and self.value in KNOWN_STATUSES:
            raise ValueError("unknown raw status uses a known value")

    @classmethod
    def from_value(cls, value: str | None) -> "RawStatus":
        if value is None or value == "":
            return cls(StatusKind.MISSING, None)
        return cls(
            StatusKind.KNOWN if value in KNOWN_STATUSES else StatusKind.UNKNOWN,
            value,
        )


def derive_authority(status: RawStatus) -> Authority:
    if status.kind is StatusKind.KNOWN and status.value == "canon":
        return Authority.CANON
    if status.kind is StatusKind.KNOWN and status.value == "revealed":
        return Authority.REVEALED
    return Authority.PREPARATION


@dataclass(frozen=True)
class AtlasRecord:
    record_id: str
    record_type: str
    name: str
    raw_status: RawStatus
    authority: Authority
    summary: str
    content: str
    content_digest: str

    def __post_init__(self) -> None:
        require_domain_id(self.record_id, "record_id")
        require_domain_id(self.record_type, "record_type")
        if not 1 <= len(self.name) <= 200:
            raise ValueError("record name length is invalid")
        if self.authority is not derive_authority(self.raw_status):
            raise ValueError("derived authority does not match raw status")
        normalized = normalize_content(self.content)
        if normalized != self.content or content_digest(normalized) != self.content_digest:
            raise ValueError("record content digest binding is invalid")


@dataclass(frozen=True)
class AtlasEdge:
    edge_id: str
    occurrence_order: int
    source_record_id: str
    target_record_id: str
    relationship: str
    state: str
    context: str

    def __post_init__(self) -> None:
        require_public_id(self.edge_id, "edge_id")
        require_domain_id(self.source_record_id, "source_record_id")
        require_domain_id(self.target_record_id, "target_record_id")
        require_domain_id(self.relationship, "relationship")
        require_domain_id(self.state, "state")
        if self.occurrence_order < 1 or not self.context:
            raise ValueError("edge occurrence is invalid")


def edge_id(revision_id: str, source_record_id: str, occurrence_order: int) -> str:
    require_public_id(revision_id, "revision_id")
    require_domain_id(source_record_id, "source_record_id")
    if occurrence_order < 1:
        raise ValueError("occurrence_order must be positive")
    digest = canonical_digest(
        {
            "occurrence_order": occurrence_order,
            "revision_id": revision_id,
            "source_record_id": source_record_id,
        }
    )
    return "edge_" + digest


class HistoryChangeKind(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    CONTENT_CHANGED = "content_changed"
    METADATA_CHANGED = "metadata_changed"
    AUTHORITY_TRANSITION = "authority_transition"


HISTORY_KIND_ORDER = {
    HistoryChangeKind.ADDED: 0,
    HistoryChangeKind.REMOVED: 1,
    HistoryChangeKind.CONTENT_CHANGED: 2,
    HistoryChangeKind.METADATA_CHANGED: 3,
    HistoryChangeKind.AUTHORITY_TRANSITION: 4,
}


@dataclass(frozen=True)
class AtlasHistoryChange:
    record_id: str
    change_kind: HistoryChangeKind
    link_revision_id: str
    before_content_digest: str | None
    after_content_digest: str | None
    before_status: RawStatus | None
    after_status: RawStatus | None
    from_authority: Authority | None
    to_authority: Authority | None

    def __post_init__(self) -> None:
        require_domain_id(self.record_id, "record_id")
        require_public_id(self.link_revision_id, "link_revision_id")
        for field, value in (
            ("before_content_digest", self.before_content_digest),
            ("after_content_digest", self.after_content_digest),
        ):
            if value is not None:
                require_digest(value, field)
        if self.change_kind is HistoryChangeKind.ADDED:
            if self.before_content_digest is not None or self.after_content_digest is None:
                raise ValueError("added history change has invalid content binding")
        elif self.change_kind is HistoryChangeKind.REMOVED:
            if self.before_content_digest is None or self.after_content_digest is not None:
                raise ValueError("removed history change has invalid content binding")
        elif self.before_content_digest is None or self.after_content_digest is None:
            raise ValueError("retained history change requires both content digests")
        if self.change_kind is HistoryChangeKind.AUTHORITY_TRANSITION and (
            self.from_authority is None
            or self.to_authority is None
            or self.from_authority is self.to_authority
        ):
            raise ValueError("authority transition is not visible")


@dataclass(frozen=True)
class AtlasHistoryEntry:
    revision_id: str
    parent_revision_id: str | None
    ordinal: int
    tree_digest: str
    change_digest: str
    changes: tuple[AtlasHistoryChange, ...]
    proposal_id: str | None = None
    proposal_version: int | None = None

    def __post_init__(self) -> None:
        require_public_id(self.revision_id, "revision_id")
        if self.parent_revision_id is not None:
            require_public_id(self.parent_revision_id, "parent_revision_id")
        require_digest(self.tree_digest, "tree_digest")
        require_digest(self.change_digest, "change_digest")
        if self.ordinal < 1:
            raise ValueError("history ordinal must be positive")
        if (self.proposal_id is None) != (self.proposal_version is None):
            raise ValueError("proposal provenance must be complete or absent")
        if self.proposal_id is not None:
            require_public_id(self.proposal_id, "proposal_id")
            if self.proposal_version is None or self.proposal_version < 1:
                raise ValueError("proposal version must be positive")
        expected = tuple(
            sorted(
                self.changes,
                key=lambda item: (item.record_id, HISTORY_KIND_ORDER[item.change_kind]),
            )
        )
        if self.changes != expected or len(
            {(item.record_id, item.change_kind) for item in self.changes}
        ) != len(self.changes):
            raise ValueError("history changes are not uniquely ordered")


@dataclass(frozen=True)
class AtlasProjectionBundle:
    campaign_id: str
    campaign_name: str
    adapter_id: str
    revision_id: str
    parent_revision_id: str | None
    ordinal: int
    tree_digest: str
    projection_version: int
    records: tuple[AtlasRecord, ...]
    edges: tuple[AtlasEdge, ...]
    history_entry: AtlasHistoryEntry
    projection_digest: str

    def __post_init__(self) -> None:
        require_public_id(self.campaign_id, "campaign_id")
        require_public_id(self.revision_id, "revision_id")
        if self.parent_revision_id is not None:
            require_public_id(self.parent_revision_id, "parent_revision_id")
        require_digest(self.tree_digest, "tree_digest")
        require_digest(self.projection_digest, "projection_digest")
        if self.projection_version < 1 or self.ordinal < 1:
            raise ValueError("projection version and ordinal must be positive")
        if not 1 <= len(self.campaign_name) <= 200 or self.adapter_id != "mothership":
            raise ValueError("projection campaign metadata is invalid")
        if (
            self.history_entry.revision_id != self.revision_id
            or self.history_entry.parent_revision_id != self.parent_revision_id
            or self.history_entry.ordinal != self.ordinal
            or self.history_entry.tree_digest != self.tree_digest
        ):
            raise ValueError("projection history identity is invalid")
        if tuple(sorted(self.records, key=lambda item: item.record_id)) != self.records:
            raise ValueError("projection records are not ordered")
        if len({item.record_id for item in self.records}) != len(self.records):
            raise ValueError("projection record identifiers are duplicated")
        if len({item.edge_id for item in self.edges}) != len(self.edges):
            raise ValueError("projection edge identifiers are duplicated")
        for item in self.edges:
            if item.edge_id != edge_id(
                self.revision_id, item.source_record_id, item.occurrence_order
            ):
                raise ValueError("projection edge identity is invalid")


@dataclass(frozen=True)
class RecordLibraryQuery:
    campaign_id: str
    revision_id: str
    tree_digest: str
    query: str = ""
    record_types: tuple[str, ...] = ()
    authorities: tuple[Authority, ...] = ()
    statuses: tuple[str, ...] = ()
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        require_public_id(self.campaign_id, "campaign_id")
        require_public_id(self.revision_id, "revision_id")
        require_digest(self.tree_digest, "tree_digest")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if len(self.query) > 4000 or (
            self.cursor is not None and len(self.cursor) > 4096
        ):
            raise ValueError("query or cursor is too long")
        for value in self.record_types:
            require_domain_id(value, "record_type")
        if len(set(self.record_types)) != len(self.record_types):
            raise ValueError("record type filters must be unique")
        if len(set(self.authorities)) != len(self.authorities):
            raise ValueError("authority filters must be unique")
        allowed_statuses = KNOWN_STATUSES | {"missing", "unknown"}
        if (
            len(set(self.statuses)) != len(self.statuses)
            or any(value not in allowed_statuses for value in self.statuses)
        ):
            raise ValueError("status filters are invalid")


@dataclass(frozen=True)
class FacetCount:
    value: str
    count: int


@dataclass(frozen=True)
class RecordLibraryResult:
    query: RecordLibraryQuery
    items: tuple[AtlasRecord, ...]
    total: int
    type_facets: tuple[FacetCount, ...]
    authority_facets: tuple[FacetCount, ...]
    status_facets: tuple[FacetCount, ...]
    next_cursor: str | None
    previous_cursor: str | None


@dataclass(frozen=True)
class NeighborhoodQuery:
    campaign_id: str
    revision_id: str
    tree_digest: str
    focus_record_id: str
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        require_public_id(self.campaign_id, "campaign_id")
        require_public_id(self.revision_id, "revision_id")
        require_digest(self.tree_digest, "tree_digest")
        require_domain_id(self.focus_record_id, "focus_record_id")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if self.cursor is not None and len(self.cursor) > 4096:
            raise ValueError("cursor is too long")


@dataclass(frozen=True)
class AtlasNeighborhood:
    query: NeighborhoodQuery
    focus: AtlasRecord
    neighbors: tuple[AtlasRecord, ...]
    edges: tuple[AtlasEdge, ...]
    total_edges: int
    next_cursor: str | None
    previous_cursor: str | None


@dataclass(frozen=True)
class ApprovedHistoryQuery:
    campaign_id: str
    revision_id: str
    tree_digest: str
    subject_record_id: str | None = None
    limit: int = 50
    cursor: str | None = None
    direction: str = "forward"

    def __post_init__(self) -> None:
        require_public_id(self.campaign_id, "campaign_id")
        require_public_id(self.revision_id, "revision_id")
        require_digest(self.tree_digest, "tree_digest")
        if self.subject_record_id is not None:
            require_domain_id(self.subject_record_id, "subject_record_id")
        if not 1 <= self.limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if self.cursor is not None and len(self.cursor) > 4096:
            raise ValueError("cursor is too long")
        if self.direction not in {"forward", "backward"}:
            raise ValueError("history direction is invalid")


@dataclass(frozen=True)
class ApprovedHistoryResult:
    query: ApprovedHistoryQuery
    entries: tuple[AtlasHistoryEntry, ...]
    total: int
    next_cursor: str | None
    previous_cursor: str | None


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    if not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid_cursor_binding")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except Exception as exc:
        raise ValueError("invalid_cursor_binding") from exc


def encode_cursor(binding: dict[str, object]) -> str:
    canonical = json.dumps(
        binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    envelope = {
        "binding": binding,
        "digest": hashlib.sha256(canonical).hexdigest(),
        "version": 2,
    }
    return _base64url_encode(
        json.dumps(
            envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )


def decode_cursor(value: str) -> dict[str, object]:
    try:
        envelope = json.loads(_base64url_decode(value).decode("utf-8"))
        if set(envelope) != {"binding", "digest", "version"}:
            raise ValueError
        binding = envelope["binding"]
        if envelope["version"] != 2 or not isinstance(binding, dict):
            raise ValueError
        canonical = json.dumps(
            binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if not hmac.compare_digest(
            envelope["digest"], hashlib.sha256(canonical).hexdigest()
        ):
            raise ValueError
        return binding
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid_cursor_binding") from exc


def facet_counts(values: Iterable[str]) -> tuple[FacetCount, ...]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return tuple(FacetCount(value, counts[value]) for value in sorted(counts))
