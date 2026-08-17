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

    def record_consent(self, *, explicit: bool, notice: str = "minimal deterministic excerpts", endpoint_id: str = "responses_api", region: str = "provider_default") -> ProviderConsent:
        if not explicit:
            raise ConsentRequired("explicit data-transfer consent is required")
        if not self.provider.verify():
            raise ConsentRequired("provider verification is required before consent")
        consent = ProviderConsent(
            credential_revision_fingerprint=self.provider.credential_revision_fingerprint(),
            adapter_version=self.provider.adapter_version,
            endpoint_id=endpoint_id,
            region=region,
            storage_mode="no_training",
            retrieval_policy_version=1,
            notice_digest=canonical_digest({"notice": notice}),
            current=True,
        )
        self.repository.set_consent(consent)
        return consent

    def disable(self) -> None:
        self._enabled = False

    def start(self, generation_id: str, campaign_id: str, revision_id: str, action: Action, prompt: str, *, session_id: str | None = None) -> GenerationRecord:
        if not self._enabled:
            raise ProviderUnavailable("provider feature is disabled")
        consent = self.repository.consent()
        if consent is None or not consent.current or not self.provider.verify() or consent.credential_revision_fingerprint != self.provider.credential_revision_fingerprint():
            raise ConsentRequired("provider verification and current consent are required")
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
        request = GenerationRequest(generation_id, campaign_id, revision_id, action, prompt, envelope)
        existing = self.repository.get_generation(generation_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError("idempotency_digest_conflict")
            return existing
        record = GenerationRecord(request)
        # The persistence boundary is completed before provider dispatch.
        self.repository.begin_generation(record)
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
                self._append(record, pending_terminal[0], pending_terminal[1])
                record.terminal_status = "complete" if pending_terminal[0] == "completion" else "failed"
            else:
                self._append(record, "failure", retryable=True)
                record.terminal_status = "failed"
        except ProviderUnavailable:
            self._append(record, "failure", retryable=pending_terminal is None)
            record.terminal_status = "failed"
        self.repository.save_generation(record)
        return record

    @staticmethod
    def resume(record: GenerationRecord, after_sequence: int) -> tuple[StreamEvent, ...]:
        return tuple(event for event in record.events if event.sequence > after_sequence)

    def _append(self, record: GenerationRecord, event_type: str, fragment: str | None = None, *, retryable: bool | None = None) -> None:
        record.events.append(StreamEvent(len(record.events) + 1, event_type, fragment, retryable))
        self.repository.save_generation(record)
