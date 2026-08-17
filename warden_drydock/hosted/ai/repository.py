from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from typing import Iterator

from .models import GenerationRecord, LiveSession, ProviderConsent, SourceEnvelope


class InMemoryAIRepository:
    """Transactional reference repository used by services and deterministic tests."""

    def __init__(self) -> None:
        self.sources: dict[str, SourceEnvelope] = {}
        self.generations: dict[str, GenerationRecord] = {}
        self.sessions: dict[str, LiveSession] = {}
        self.dispatch_log: list[str] = []
        self.provider_consent: ProviderConsent | None = None

    @contextmanager
    def transaction(self) -> Iterator["InMemoryAIRepository"]:
        snapshot = deepcopy((self.sources, self.generations, self.sessions, self.dispatch_log, self.provider_consent))
        try:
            yield self
        except Exception:
            self.sources, self.generations, self.sessions, self.dispatch_log, self.provider_consent = snapshot
            raise

    def persist_sources(self, generation_id: str, envelope: SourceEnvelope) -> None:
        existing = self.sources.get(generation_id)
        if existing is not None and existing != envelope:
            raise ValueError("source_digest_conflict")
        self.sources[generation_id] = envelope

    def create_generation(self, record: GenerationRecord) -> None:
        existing = self.generations.get(record.request.generation_id)
        if existing is not None and existing.request != record.request:
            raise ValueError("idempotency_digest_conflict")
        self.generations.setdefault(record.request.generation_id, record)


class PostgresAIRepository:
    """Minimal PostgreSQL persistence for source-before-dispatch and live workflow state."""

    def __init__(self, connect) -> None:
        self._connect = connect

    def persist_generation_start(self, record: GenerationRecord) -> None:
        request = record.request
        envelope = request.envelope
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hosted_ai_generation "
                "(generation_id,campaign_id,revision_id,session_id,action,request_digest,source_set_digest,source_envelope,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'pending') "
                "ON CONFLICT (generation_id) DO NOTHING",
                (request.generation_id, request.campaign_id, request.revision_id,
                 envelope.session_id, request.action.value,
                 __import__("hashlib").sha256(request.prompt.encode("utf-8")).hexdigest(),
                 envelope.source_set_digest,
                 __import__("json").dumps({"sources": [item.__dict__ for item in envelope.excerpts]})),
            )
            if cursor.rowcount == 0:
                cursor.execute("SELECT source_set_digest FROM hosted_ai_generation WHERE generation_id=%s", (request.generation_id,))
                row = cursor.fetchone()
                if row is None or row[0] != envelope.source_set_digest:
                    raise ValueError("source_digest_conflict")

    def append_event(self, generation_id: str, sequence: int, event_type: str, payload: dict) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hosted_ai_stream_event(generation_id,sequence,event_type,payload) VALUES(%s,%s,%s,%s::jsonb)",
                (generation_id, sequence, event_type, __import__("json").dumps(payload)),
            )
