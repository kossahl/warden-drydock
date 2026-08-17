from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Action(str, Enum):
    ASK = "ask"
    CHECK = "check"
    GENERATE = "generate"


class CaptureType(str, Enum):
    CONFIRMED_FACT = "confirmed_fact"
    UNRESOLVED_QUESTION = "unresolved_question"


@dataclass(frozen=True)
class ProviderConsent:
    credential_revision_fingerprint: str
    adapter_version: str
    endpoint_id: str
    region: str
    storage_mode: str
    retrieval_policy_version: int
    notice_digest: str
    current: bool


@dataclass(frozen=True)
class SourceExcerpt:
    source_id: str
    authority: str
    text: str
    order: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceEnvelope:
    campaign_id: str
    revision_id: str
    excerpts: tuple[SourceExcerpt, ...]
    session_id: str | None = None
    retrieval_policy_version: int = 1

    @property
    def source_set_digest(self) -> str:
        return canonical_digest({
            "campaign_id": self.campaign_id,
            "revision_id": self.revision_id,
            "retrieval_policy_version": self.retrieval_policy_version,
            "session_id": self.session_id,
            "sources": [{
                "authority": item.authority,
                "digest": item.digest,
                "order": item.order,
                "source_id": item.source_id,
            } for item in self.excerpts],
        })


@dataclass(frozen=True)
class GenerationRequest:
    generation_id: str
    campaign_id: str
    revision_id: str
    action: Action
    prompt: str
    envelope: SourceEnvelope


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    event_type: str
    draft_fragment: str | None = None
    retryable: bool | None = None


@dataclass
class GenerationRecord:
    request: GenerationRequest
    events: list[StreamEvent] = field(default_factory=list)
    terminal_status: str | None = None
    terminal_content: str = ""


@dataclass(frozen=True)
class Capture:
    event_id: str
    device_id: str
    operation_id: str
    device_order: int
    capture_type: CaptureType
    text: str
    payload_digest: str


@dataclass
class LiveSession:
    session_id: str
    campaign_id: str
    base_revision: str
    reported_head_revision: str
    workflow_version: int = 1
    controller_epoch: int = 1
    controller_id: str = ""
    mode: str = "active"
    captures: list[Capture] = field(default_factory=list)
    receipts: dict[tuple[str, str], str] = field(default_factory=dict)
