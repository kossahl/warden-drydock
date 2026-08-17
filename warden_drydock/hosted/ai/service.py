from __future__ import annotations

from collections.abc import Iterable

from .models import Action, GenerationRecord, GenerationRequest, ProviderConsent, StreamEvent, canonical_digest
from .provider import ProviderUnavailable


class ConsentRequired(PermissionError):
    pass


class GroundedAIService:
    def __init__(self, repository, selector, provider) -> None:
        self.repository = repository
        self.selector = selector
        self.provider = provider
        self._enabled = True

    def record_consent(self, *, explicit: bool, notice: str = "minimal deterministic excerpts", endpoint_id: str = "responses_api", region: str = "provider_default") -> ProviderConsent:
        if not explicit:
            raise ConsentRequired("explicit data-transfer consent is required")
        if not self.provider.verify():
            raise ConsentRequired("provider verification is required before consent")
        consent = ProviderConsent(
            credential_revision_fingerprint=canonical_digest({"configured": True, "adapter": self.provider.adapter_id}),
            adapter_version=self.provider.adapter_version,
            endpoint_id=endpoint_id,
            region=region,
            storage_mode="no_training",
            retrieval_policy_version=1,
            notice_digest=canonical_digest({"notice": notice}),
            current=True,
        )
        self.repository.provider_consent = consent
        return consent

    def disable(self) -> None:
        self._enabled = False

    def start(self, generation_id: str, campaign_id: str, revision_id: str, action: Action, prompt: str, records: Iterable[object], *, session_id: str | None = None, confirmed_facts: Iterable[object] = ()) -> GenerationRecord:
        if not self._enabled:
            raise ProviderUnavailable("provider feature is disabled")
        if self.repository.provider_consent is None or not self.repository.provider_consent.current or not self.provider.verify():
            raise ConsentRequired("provider verification and current consent are required")
        envelope = self.selector.select(campaign_id, revision_id, records, session_id=session_id, confirmed_facts=confirmed_facts)
        request = GenerationRequest(generation_id, campaign_id, revision_id, action, prompt, envelope)
        existing = self.repository.generations.get(generation_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError("idempotency_digest_conflict")
            return existing
        record = GenerationRecord(request)
        # The persistence boundary is completed before provider dispatch.
        with self.repository.transaction():
            self.repository.persist_sources(generation_id, envelope)
            self.repository.create_generation(record)
        self.repository.dispatch_log.append(generation_id)
        self._append(record, "start")
        try:
            for event_type, fragment in self.provider.stream(request):
                if event_type not in {"delta", "usage", "completion", "failure"}:
                    raise ValueError("capability_rejected")
                self._append(record, event_type, fragment)
                if event_type == "delta" and fragment:
                    record.terminal_content += fragment
                if event_type in {"completion", "failure"}:
                    record.terminal_status = "complete" if event_type == "completion" else "failed"
            if record.terminal_status is None:
                self._append(record, "failure", retryable=True)
                record.terminal_status = "failed"
        except ProviderUnavailable:
            self._append(record, "failure", retryable=True)
            record.terminal_status = "failed"
        return record

    @staticmethod
    def resume(record: GenerationRecord, after_sequence: int) -> tuple[StreamEvent, ...]:
        return tuple(event for event in record.events if event.sequence > after_sequence)

    @staticmethod
    def _append(record: GenerationRecord, event_type: str, fragment: str | None = None, *, retryable: bool | None = None) -> None:
        record.events.append(StreamEvent(len(record.events) + 1, event_type, fragment, retryable))
