from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re

from .models import EngineResult, RetrievalResult, Status


_PUBLIC_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_ENGINE_COMMANDS = {
    "campaign_initialize",
    "proposal_stage",
    "artifacts_rebuild",
    "campaign_validate",
}


class ContractMappingError(ValueError):
    pass


class SourceAuthority(str, Enum):
    PREPARATION = "preparation"
    TABLE_FACT = "table_fact"
    CANON = "canon"
    REVEALED = "revealed"


@dataclass(frozen=True)
class RetrievalSourceBinding:
    subject_id: str
    source_id: str
    authority: SourceAuthority


@dataclass(frozen=True)
class RetrievalContractBinding:
    campaign_id: str
    revision_id: str
    retrieval_policy_version: int
    sources: tuple[RetrievalSourceBinding, ...]
    session_id: str | None = None


def to_engine_staged_result_v1(result: EngineResult) -> dict[str, object]:
    """Map an internal command result to exactly engine_staged_result v1."""

    if result.command not in _ENGINE_COMMANDS:
        raise ContractMappingError(
            f"{result.command!r} is not an engine_staged_result v1 command"
        )
    payload: dict[str, object] = {
        "contract_name": "engine_staged_result",
        "contract_version": 1,
        "command_id": result.command_id,
        "command": result.command,
        "snapshot_handle": result.snapshot_handle.value,
        "staged_handle": result.staged_handle.value,
        "input_digest": result.input_digest,
        "status": result.status.value,
        "findings": [
            {
                "code": finding.code,
                "severity": finding.severity.value,
                "stage": finding.stage.value,
                "subject_id": finding.subject_id,
            }
            for finding in result.findings
        ],
    }
    if result.result_digest is not None:
        payload["result_digest"] = result.result_digest
    return payload


def to_retrieval_source_envelope_v1(
    result: RetrievalResult,
    binding: RetrievalContractBinding,
) -> dict[str, object]:
    """Map content-bearing retrieval to the accepted pinned-source contract."""

    if result.result.command != "retrieve" or result.result.status is not Status.STAGED:
        raise ContractMappingError("only successful retrieval results can be mapped")
    _require_public_id(binding.campaign_id, "campaign_id")
    _require_public_id(binding.revision_id, "revision_id")
    if binding.session_id is not None:
        _require_public_id(binding.session_id, "session_id")
    if binding.retrieval_policy_version < 1:
        raise ContractMappingError("retrieval_policy_version must be positive")

    bindings: dict[str, RetrievalSourceBinding] = {}
    public_sources: set[str] = set()
    for source in binding.sources:
        if source.subject_id in bindings:
            raise ContractMappingError("subject_id binding is duplicated")
        _require_public_id(source.source_id, "source_id")
        if source.source_id in public_sources:
            raise ContractMappingError("source_id binding is duplicated")
        bindings[source.subject_id] = source
        public_sources.add(source.source_id)

    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for record in result.records:
        if record.content is None:
            raise ContractMappingError("retrieved record has no source content")
        if record.subject_id not in grouped:
            grouped[record.subject_id] = []
            order.append(record.subject_id)
        grouped[record.subject_id].append(record.content)
    for connection in result.connections:
        if connection.subject_id not in grouped:
            grouped[connection.subject_id] = []
            order.append(connection.subject_id)
        grouped[connection.subject_id].append(connection.context)
    if not order:
        raise ContractMappingError("retrieval contract requires at least one source")

    citations: list[dict[str, object]] = []
    excerpt_total = 0
    for citation_index, subject_id in enumerate(order, start=1):
        source = bindings.get(subject_id)
        if source is None:
            raise ContractMappingError(f"missing source binding for {subject_id!r}")
        excerpts: list[dict[str, object]] = []
        for excerpt_index, text in enumerate(grouped[subject_id], start=1):
            if not text or len(text) > 8000:
                raise ContractMappingError("retrieval excerpt is empty or exceeds v1")
            excerpts.append(
                {
                    "excerpt_id": f"excerpt_{citation_index:04d}_{excerpt_index:04d}",
                    "text": text,
                    "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "byte_count": len(text.encode("utf-8")),
                    "character_count": len(text),
                }
            )
        excerpt_total += len(excerpts)
        citations.append(
            {
                "citation_id": f"citation_{citation_index:04d}",
                "source_id": source.source_id,
                "authority": source.authority.value,
                "order": citation_index,
                "excerpt_count": len(excerpts),
                "excerpts": excerpts,
            }
        )

    source_set_digest = _canonical_digest(citations)
    payload: dict[str, object] = {
        "contract_name": "retrieval_source_envelope",
        "contract_version": 1,
        "campaign_id": binding.campaign_id,
        "revision_id": binding.revision_id,
        "retrieval_policy_version": binding.retrieval_policy_version,
        "citations": citations,
        "source_count": len(citations),
        "excerpt_count": excerpt_total,
        "source_set_digest": source_set_digest,
    }
    if binding.session_id is not None:
        payload["session_id"] = binding.session_id
    return payload


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_public_id(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= 80
        or not _PUBLIC_ID.fullmatch(value)
    ):
        raise ContractMappingError(f"{field} is not a valid public identifier")
