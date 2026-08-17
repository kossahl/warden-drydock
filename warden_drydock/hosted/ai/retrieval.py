from __future__ import annotations

from collections.abc import Iterable

from .models import SourceEnvelope, SourceExcerpt
from warden_drydock.hosted.engine.models import RetrievalKind, RetrievalRequest, Status


class EngineSourceLoader:
    """Loads sources through the typed engine using a server-selected workspace."""

    def __init__(self, engine, workspace_for_revision) -> None:
        self.engine = engine
        self.workspace_for_revision = workspace_for_revision

    def load(self, campaign_id: str, revision_id: str, prompt: str) -> tuple[object, ...]:
        handle = self.workspace_for_revision(campaign_id, revision_id)
        result = self.engine.retrieve(RetrievalRequest(
            command_id="retrieval_" + __import__("hashlib").sha256(f"{campaign_id}:{revision_id}:{prompt}".encode()).hexdigest()[:16],
            workspace_handle=handle,
            kind=RetrievalKind.FIND,
            subject_id=prompt,
        ))
        if result.result.status is not Status.STAGED:
            raise ValueError("retrieval_consistency_failure")
        return result.records


class DeterministicSourceSelector:
    """Selects a stable bounded set before a provider can be invoked."""

    def __init__(self, *, max_sources: int = 20) -> None:
        self.max_sources = max_sources

    def select(self, campaign_id: str, revision_id: str, records: Iterable[object], *, session_id: str | None = None, confirmed_facts: Iterable[object] = ()) -> SourceEnvelope:
        ranked: list[tuple[int, str, str, str]] = []
        authority_rank = {"table_fact": 0, "canon": 1, "revealed": 2, "preparation": 3}
        for record in records:
            authority = str(getattr(record, "authority", getattr(record, "status", "preparation")))
            source_id = str(getattr(record, "source_id", None) or getattr(record, "subject_id"))
            text = str(getattr(record, "text", getattr(record, "content", "")) or "")
            if text:
                ranked.append((authority_rank.get(authority, 3), source_id, authority, text))
        for fact in confirmed_facts:
            source_id = str(getattr(fact, "event_id"))
            text = str(getattr(fact, "text"))
            ranked.append((0, source_id, "table_fact", text))
        ranked.sort(key=lambda item: (item[0], item[1], item[3]))
        excerpts = tuple(SourceExcerpt(source_id, authority, text, order) for order, (_, source_id, authority, text) in enumerate(ranked[:self.max_sources], 1))
        if not excerpts:
            raise ValueError("retrieval produced no grounded sources")
        return SourceEnvelope(campaign_id, revision_id, excerpts, session_id)
