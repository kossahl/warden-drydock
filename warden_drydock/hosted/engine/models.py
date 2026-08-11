from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Iterable


_OPAQUE_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _is_public_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 3 <= len(value) <= 80
        and _OPAQUE_ID.fullmatch(value) is not None
    )


@dataclass(frozen=True)
class WorkspaceHandle:
    """An opaque server-issued reference; its value is never interpreted as a path."""

    value: str

    def __post_init__(self) -> None:
        if not _is_public_id(self.value):
            raise ValueError("workspace handle is not a valid opaque identifier")


class Stage(str, Enum):
    INITIALIZE = "initialize"
    INDEX = "index"
    CONTEXT = "context"
    VALIDATE = "validate"
    RETRIEVE = "retrieve"
    STAGE = "stage"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Status(str, Enum):
    STAGED = "staged"
    INVALID = "invalid"
    FAILED = "failed"


class RetrievalKind(str, Enum):
    FIND = "find"
    SHOW = "show"
    RELATED = "related"
    BACKLINKS = "backlinks"
    HISTORY = "history"


class ChangeKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    stage: Stage
    subject_id: str


@dataclass(frozen=True)
class InitializeRequest:
    command_id: str
    workspace_handle: WorkspaceHandle
    campaign_name: str
    adapter: str = "mothership"


@dataclass(frozen=True)
class WorkspaceRequest:
    command_id: str
    workspace_handle: WorkspaceHandle


@dataclass(frozen=True)
class ContextRequest:
    command_id: str
    workspace_handle: WorkspaceHandle
    focus_id: str | None = None
    depth: int = 1
    max_records: int = 20


@dataclass(frozen=True)
class RetrievalRequest:
    command_id: str
    workspace_handle: WorkspaceHandle
    kind: RetrievalKind
    subject_id: str
    depth: int = 1


@dataclass(frozen=True)
class ExactTextChange:
    change_id: str
    subject_id: str
    expected_content_digest: str | None
    replacement: str
    change_kind: ChangeKind = ChangeKind.UPDATE
    record_type: str | None = None


@dataclass(frozen=True)
class StageExactDiffRequest:
    command_id: str
    workspace_handle: WorkspaceHandle
    diff_digest: str
    changes: tuple[ExactTextChange, ...]


@dataclass(frozen=True)
class EngineResult:
    command_id: str
    command: str
    snapshot_handle: WorkspaceHandle
    staged_handle: WorkspaceHandle
    input_digest: str
    result_digest: str | None
    status: Status
    findings: tuple[Finding, ...] = ()
    artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievedRecord:
    subject_id: str
    record_type: str
    name: str
    status: str
    content: str | None = None


@dataclass(frozen=True)
class RetrievedConnection:
    subject_id: str
    target_id: str
    relationship: str
    state: str
    context: str


@dataclass(frozen=True)
class RetrievalResult:
    result: EngineResult
    records: tuple[RetrievedRecord, ...] = ()
    connections: tuple[RetrievedConnection, ...] = ()


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def exact_diff_digest(changes: Iterable[ExactTextChange]) -> str:
    encoded = [
        {
            "change_id": change.change_id,
            "change_kind": change.change_kind.value,
            "expected_content_digest": change.expected_content_digest,
            "record_type": change.record_type,
            "replacement_digest": content_digest(change.replacement),
            "subject_id": change.subject_id,
        }
        for change in changes
    ]
    payload = json.dumps(encoded, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
