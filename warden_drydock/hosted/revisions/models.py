from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
import re


_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _require_id(value: str, field: str) -> None:
    if not 3 <= len(value) <= 80 or _ID.fullmatch(value) is None:
        raise ValueError(f"{field} is not a safe public identifier")


def _require_digest(value: str, field: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} is not a lowercase SHA-256 digest")


class RevisionError(RuntimeError):
    """Base class for closed revision failures."""


class SnapshotIntegrityError(RevisionError):
    pass


class SnapshotLineageError(RevisionError):
    pass


class StaleHeadError(RevisionError):
    pass


class PublicationIntentError(RevisionError):
    pass


class PublicationKind(str, Enum):
    CREATION = "creation"
    APPROVAL = "approval"


class IntentStatus(str, Enum):
    PENDING = "pending"
    FINALIZED = "finalized"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class FileHash:
    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        parsed = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or parsed.is_absolute()
            or ".." in parsed.parts
            or "\\" in self.relative_path
            or ":" in parsed.parts[0]
        ):
            raise ValueError("relative_path is unsafe")
        _require_digest(self.sha256, "sha256")


@dataclass(frozen=True)
class SnapshotManifest:
    campaign_id: str
    revision_id: str
    parent_revision: str | None
    ordinal: int
    tree_digest: str
    files: tuple[FileHash, ...]
    framework_version: str
    adapter_version: str
    validation_contract_digest: str
    change_digest: str
    publication_intent_token: str
    manifest_version: int = 1

    def __post_init__(self) -> None:
        for field, value in (
            ("campaign_id", self.campaign_id),
            ("revision_id", self.revision_id),
            ("publication_intent_token", self.publication_intent_token),
        ):
            _require_id(value, field)
        if self.parent_revision is not None:
            _require_id(self.parent_revision, "parent_revision")
        for field, value in (
            ("tree_digest", self.tree_digest),
            ("validation_contract_digest", self.validation_contract_digest),
            ("change_digest", self.change_digest),
        ):
            _require_digest(value, field)
        if self.manifest_version != 1 or self.ordinal < 1:
            raise ValueError("manifest version or ordinal is invalid")
        if not self.files or tuple(sorted(self.files, key=lambda item: item.relative_path)) != self.files:
            raise ValueError("manifest files must be non-empty and sorted")
        if len({item.relative_path for item in self.files}) != len(self.files):
            raise ValueError("manifest file paths must be unique")


@dataclass(frozen=True)
class PublicationIntent:
    intent_id: str
    intent_token: str
    kind: PublicationKind
    campaign_id: str
    revision_id: str
    parent_revision: str | None
    ordinal: int
    tree_digest: str
    change_digest: str
    status: IntentStatus = IntentStatus.PENDING

    def __post_init__(self) -> None:
        for field, value in (
            ("intent_id", self.intent_id), ("intent_token", self.intent_token),
            ("campaign_id", self.campaign_id), ("revision_id", self.revision_id),
        ):
            _require_id(value, field)
        if self.parent_revision is not None:
            _require_id(self.parent_revision, "parent_revision")
        _require_digest(self.tree_digest, "tree_digest")
        _require_digest(self.change_digest, "change_digest")
        if self.ordinal < 1:
            raise ValueError("ordinal must be positive")


@dataclass(frozen=True)
class ProjectionBundle:
    campaign_id: str
    revision_id: str
    projection_version: int
    record_count: int
    projection_digest: str
    records: tuple[tuple[str, str, str], ...]
