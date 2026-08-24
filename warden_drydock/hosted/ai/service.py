from __future__ import annotations

from .models import Action, GenerationRecord, GenerationRequest, ProviderConsent, StreamEvent, canonical_digest
from .provider import ProviderUnavailable


class ConsentRequired(PermissionError):
    pass


class GroundedAIService:
    def __init__(self, repository, selector, provider, source_loader) -> None:
        self.repository = repository
        self.selector = selector
        self.provider = provider
        self.source_loader = source_loader
        self._enabled = True
        self.endpoint_id = "responses_api"
        self.region = "provider_default"
        self.storage_mode = "no_training"
        self.retrieval_policy_version = 1
        self.notice = "minimal deterministic excerpts"

    def record_consent(self, *, explicit: bool, notice: str = "minimal deterministic excerpts", endpoint_id: str = "responses_api", region: str = "provider_default") -> ProviderConsent:
        if not explicit:
            raise ConsentRequired("explicit data-transfer consent is required")
        if not self.provider.verify():
            raise ConsentRequired("provider credential configuration is required before consent")
        self.notice = notice
        self.endpoint_id = endpoint_id
        self.region = region
        consent = self._current_consent_identity()
        self.repository.set_consent(consent)
        return consent

    def _current_consent_identity(self) -> ProviderConsent:
        return ProviderConsent(
            credential_revision_fingerprint=self.provider.credential_revision_fingerprint(),
            adapter_version=self.provider.adapter_version,
            endpoint_id=self.endpoint_id,
            region=self.region,
            storage_mode=self.storage_mode,
            retrieval_policy_version=self.retrieval_policy_version,
            notice_digest=canonical_digest({"notice": self.notice}),
            current=True,
        )

    def disable(self) -> None:
        self._enabled = False

    def start(self, generation_id: str, campaign_id: str, revision_id: str, action: Action, prompt: str, *, session_id: str | None = None, focus_record_id: str | None = None, focus_content_digest: str | None = None) -> GenerationRecord:
        record, reserved = self.prepare(
            generation_id, campaign_id, revision_id, action, prompt,
            session_id=session_id, focus_record_id=focus_record_id,
            focus_content_digest=focus_content_digest,
        )
        if reserved:
            self.dispatch(record)
        return record

    def prepare(self, generation_id: str, campaign_id: str, revision_id: str, action: Action, prompt: str, *, session_id: str | None = None, focus_record_id: str | None = None, focus_content_digest: str | None = None) -> tuple[GenerationRecord, bool]:
        """Persist a pinned source envelope without contacting the provider."""
        if not self._enabled:
            raise ProviderUnavailable("provider feature is disabled")
        consent = self.repository.consent()
        if consent is None or not consent.current or not self.provider.verify() or consent != self._current_consent_identity():
            raise ConsentRequired("provider credential configuration and current consent are required")
        if session_id is not None:
            session = self.repository.get_session(session_id)
            if session.campaign_id != campaign_id or session.mode != "active":
                raise ValueError("unsafe_binding")
            revision_id = session.base_revision
            confirmed_facts = tuple(item for item in session.captures if item.capture_type.value == "confirmed_fact")
        else:
            confirmed_facts = ()
        records = self.source_loader.load(campaign_id, revision_id, prompt)
        envelope = self.selector.select(campaign_id, revision_id, records, session_id=session_id, confirmed_facts=confirmed_facts)
        request = GenerationRequest(
            generation_id, campaign_id, revision_id, action, prompt, envelope,
            focus_record_id, focus_content_digest,
        )
        record = GenerationRecord(request)
        if not self.repository.reserve_generation(record):
            return self.repository.get_generation(generation_id), False
        return record, True

    def dispatch(self, record: GenerationRecord) -> GenerationRecord:
        """Dispatch one previously reserved generation."""
        generation_id = record.request.generation_id
        stored = self.repository.get_generation(generation_id)
        if stored is None or stored.request != record.request:
            raise ValueError("unsafe_binding")
        if stored.events or stored.terminal_status is not None:
            return stored
        request = record.request
        self.repository.dispatch_log.append(generation_id)
        self._append(record, "start")
        pending_terminal: tuple[str, str | None] | None = None
        try:
            for event_type, fragment in self.provider.stream(request):
                if event_type not in {"delta", "usage", "completion", "failure"}:
                    raise ProviderUnavailable("provider stream capability rejected")
                if pending_terminal is not None:
                    raise ProviderUnavailable("provider emitted events after terminal state")
                if event_type in {"completion", "failure"}:
                    pending_terminal = (event_type, fragment)
                    continue
                if event_type == "delta" and fragment:
                    record.terminal_content += fragment
                self._append(record, event_type, fragment)
            if pending_terminal is not None:
                self._finalize(record, pending_terminal[0], pending_terminal[1], status="complete" if pending_terminal[0] == "completion" else "failed")
            else:
                self._finalize(record, "failure", retryable=True, status="failed")
        except ProviderUnavailable:
            self._finalize(record, "failure", retryable=pending_terminal is None, status="failed")
        return record

    @staticmethod
    def resume(record: GenerationRecord, after_sequence: int) -> tuple[StreamEvent, ...]:
        return tuple(event for event in record.events if event.sequence > after_sequence)

    def _append(self, record: GenerationRecord, event_type: str, fragment: str | None = None, *, retryable: bool | None = None) -> None:
        record.events.append(StreamEvent(len(record.events) + 1, event_type, fragment, retryable))
        self.repository.save_generation(record)

    def _finalize(self, record: GenerationRecord, event_type: str, fragment: str | None = None, *, retryable: bool | None = None, status: str) -> None:
        event = StreamEvent(len(record.events) + 1, event_type, fragment, retryable)
        self.repository.finalize_generation(record, event, status)
