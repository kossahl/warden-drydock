from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re


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
        normalized = self.text.replace("\r\n", "\n").replace("\r", "\n")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
    focus_record_id: str | None = None
    focus_content_digest: str | None = None

    def __post_init__(self) -> None:
        if (self.focus_record_id is None) != (self.focus_content_digest is None):
            raise ValueError("generation focus binding must be complete or absent")
        if self.focus_record_id is not None:
            if (
                not 1 <= len(self.focus_record_id) <= 80
                or re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.focus_record_id) is None
                or re.fullmatch(r"[a-f0-9]{64}", self.focus_content_digest or "")
                is None
            ):
                raise ValueError("generation focus binding is invalid")


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
    record_id: str | None = None


@dataclass(frozen=True)
class LiveEndBarrier:
    """Exact operation set/watermark persisted when a live session ends (Decision A).

    ``ready_for_proposal`` is True only when every required operation id has been
    acknowledged on the session-local persisted receipt set.
    """

    end_operation_id: str
    end_device_id: str
    required_operation_ids: tuple[tuple[str, str], ...]
    ready_for_proposal: bool


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
    end_barrier: LiveEndBarrier | None = None
    # Persisted monotonic creation marker (P1, Option C). Assigned at INSERT time for
    # every session from the first supported deployment (the ordering migration fails
    # closed if rows existed before it), so the ordering is always unambiguous.
    session_seq: int = 0


def live_operation_digest(
    *,
    campaign_id: str,
    base_revision: str,
    session_id: str,
    controller_id: str,
    controller_epoch: int,
    workflow_version: int,
    event_type: str,
    event_id: str | None,
    device_id: str,
    operation_id: str,
    device_order: int | None,
    text: str,
    record_id: str | None = None,
    required_operation_ids: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """Canonical digest binding ALL live mutation state (Blocker F3.1).

    Idempotency must not be reachable by replaying an identical operation id across
    a different session, device, controller epoch, workflow version, or payload.
    The end barrier additionally binds the exact required operation set so a
    changed barrier is never treated as an exact replay.
    """
    binding: dict[str, object] = {
        "campaign_id": campaign_id,
        "base_revision": base_revision,
        "session_id": session_id,
        "controller_id": controller_id,
        "controller_epoch": controller_epoch,
        "workflow_version": workflow_version,
        "event_type": event_type,
        "event_id": event_id,
        "device_id": device_id,
        "operation_id": operation_id,
        "device_order": device_order,
        "text": text,
    }
    if record_id is not None:
        binding["record_id"] = record_id
    if required_operation_ids is not None:
        binding["required_operation_ids"] = tuple(sorted(required_operation_ids))
    return canonical_digest(binding)
