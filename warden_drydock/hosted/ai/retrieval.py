from __future__ import annotations

from collections.abc import Iterable
import re
from dataclasses import dataclass

from .models import SourceEnvelope, SourceExcerpt
from warden_drydock.hosted.engine.models import RetrievalKind, RetrievalRequest, Status


class EngineSourceLoader:
    """Loads sources through the typed engine using a server-selected workspace."""

    def __init__(self, engine, workspace_for_revision) -> None:
        self.engine = engine
        self.workspace_for_revision = workspace_for_revision

    def load(self, campaign_id: str, revision_id: str, prompt: str) -> tuple[object, ...]:
        handle = self.workspace_for_revision(campaign_id, revision_id)
        stopwords = {"and", "are", "for", "from", "how", "is", "of", "the", "this", "to", "was", "what", "when", "where", "who", "why", "with"}
        tokens = sorted(set(re.findall(r"[a-z0-9-]{3,}", prompt.casefold())) - stopwords)
        if not tokens:
            raise ValueError("retrieval_consistency_failure")
        records: dict[str, object] = {}
        scores: dict[str, int] = {}
        for index, token in enumerate(tokens[:20], 1):
            result = self.engine.retrieve(RetrievalRequest(
                command_id="retrieval_" + __import__("hashlib").sha256(f"{campaign_id}:{revision_id}:{index}:{token}".encode()).hexdigest()[:16],
                workspace_handle=handle,
                kind=RetrievalKind.FIND,
                subject_id=token,
            ))
            if result.result.status is not Status.STAGED:
                raise ValueError("retrieval_consistency_failure")
            weight = 10000 // max(1, len(result.records)) + len(token)
            for record in result.records:
                records[record.subject_id] = record
                name = str(getattr(record, "name", "")).casefold()
                exact_bonus = 10000 if token == record.subject_id.casefold() or token == name else 0
                scores[record.subject_id] = scores.get(record.subject_id, 0) + weight + exact_bonus
        return tuple(RankedRecord(records[key], scores[key]) for key in sorted(records, key=lambda item: (-scores[item], item)))


@dataclass(frozen=True)
class RankedRecord:
    record: object
    relevance: int

    def __getattr__(self, name: str):
        return getattr(self.record, name)


class DeterministicSourceSelector:
    """Selects a stable bounded set before a provider can be invoked."""

    def __init__(self, *, max_sources: int = 20, max_excerpt_characters: int = 8000, max_total_characters: int = 32000) -> None:
        self.max_sources = max_sources
        self.max_excerpt_characters = max_excerpt_characters
        self.max_total_characters = max_total_characters

    def select(self, campaign_id: str, revision_id: str, records: Iterable[object], *, session_id: str | None = None, confirmed_facts: Iterable[object] = ()) -> SourceEnvelope:
        ranked: list[tuple[int, int, str, str, str]] = []
        authority_rank = {"table_fact": 0, "canon": 1, "revealed": 2, "preparation": 3}
        for record in records:
            authority = str(getattr(record, "authority", getattr(record, "status", "preparation")))
            if authority in {"draft", "idea", "unresolved"}:
                authority = "preparation"
            source_id = str(getattr(record, "source_id", None) or getattr(record, "subject_id"))
            text = str(getattr(record, "text", getattr(record, "content", "")) or "")
            if text:
                ranked.append((-int(getattr(record, "relevance", 0)), authority_rank.get(authority, 3), source_id, authority, text))
        for fact in confirmed_facts:
            source_id = str(getattr(fact, "event_id"))
            text = str(getattr(fact, "text"))
            ranked.append((-1000000, 0, source_id, "table_fact", text))
        ranked.sort(key=lambda item: (item[0], item[1], item[2], item[4]))
        excerpts: list[SourceExcerpt] = []
        remaining = self.max_total_characters
        for _, _, source_id, authority, text in ranked:
            if len(excerpts) >= self.max_sources or remaining <= 0:
                break
            bounded = text[: min(self.max_excerpt_characters, remaining)]
            if bounded:
                excerpts.append(SourceExcerpt(source_id, authority, bounded, len(excerpts) + 1))
                remaining -= len(bounded)
        excerpts = tuple(excerpts)
        if not excerpts:
            raise ValueError("retrieval produced no grounded sources")
        return SourceEnvelope(campaign_id, revision_id, excerpts, session_id)
