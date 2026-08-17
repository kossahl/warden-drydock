from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json

from warden_drydock.hosted.engine.models import ExactTextChange, Status, exact_diff_digest
from warden_drydock.hosted.revisions.models import StaleHeadError


class ProposalConflict(RuntimeError):
    pass


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    REJECTED = "rejected"
    APPROVED = "approved"
    APPROVING = "approving"
    CONFLICT = "conflict"
    PUBLISHED = "published"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class ProposalVersion:
    proposal_id: str
    version: int
    campaign_id: str
    base_revision: str
    changes: tuple[ExactTextChange, ...]
    diff_digest: str
    payload_digest: str
    status: ProposalStatus = ProposalStatus.DRAFT


def _payload_digest(changes: tuple[ExactTextChange, ...]) -> str:
    payload = [{"id": c.change_id, "subject": c.subject_id, "replacement": c.replacement,
                "expected": c.expected_content_digest, "kind": c.change_kind.value,
                "record_type": c.record_type} for c in changes]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProposalService:
    """Small immutable proposal state machine; authority remains with publisher."""
    def __init__(self, repository, *, head, stage, publish) -> None:
        self.repository, self._head, self._stage, self._publish = repository, head, stage, publish

    def draft(self, proposal_id, campaign_id, base_revision, changes):
        changes = tuple(changes)
        if not changes or len({c.change_id for c in changes}) != len(changes):
            raise ValueError("proposal changes must be non-empty and uniquely identified")
        item = ProposalVersion(proposal_id, self.repository.next_version(proposal_id), campaign_id,
            base_revision, changes, exact_diff_digest(changes), _payload_digest(changes))
        self.repository.add(item)
        return item

    def correct(self, version, changes):
        if version.status is not ProposalStatus.DRAFT:
            raise ValueError("only draft versions can be corrected")
        self.repository.replace_status(version, ProposalStatus.REJECTED)
        return self.draft(version.proposal_id, version.campaign_id, version.base_revision, changes)

    def reject(self, version):
        if version.status is ProposalStatus.REJECTED:
            return version
        if version.status is not ProposalStatus.DRAFT:
            raise ValueError("only draft versions can be rejected")
        return self.repository.replace_status(version, ProposalStatus.REJECTED)

    def approve(self, version, *, diff_digest, base_revision, payload_digest):
        if (diff_digest, base_revision, payload_digest) != (version.diff_digest, version.base_revision, version.payload_digest):
            raise ValueError("approval binding mismatch")
        version = self.repository.claim(version)
        if version is None:
            raise ValueError("only the current draft version can be approved")
        if self._head(version.campaign_id) != version.base_revision:
            return self.repository.replace_status(version, ProposalStatus.CONFLICT)
        staged = self._stage(version)
        if getattr(staged, "status", None) is not Status.STAGED:
            return self.repository.replace_status(version, ProposalStatus.DRAFT)
        try:
            result = self._publish(version, staged)
        except StaleHeadError:
            return self.repository.replace_status(version, ProposalStatus.CONFLICT)
        except Exception:
            # The immutable intent may be reconciled later; never replay blindly.
            return self.repository.replace_status(version, ProposalStatus.QUARANTINED)
        return self.repository.replace_status(version, ProposalStatus.PUBLISHED if result is not None else ProposalStatus.APPROVED)


class InMemoryProposalRepository:
    def __init__(self): self.items = {}; self.audit = []
    def next_version(self, proposal_id): return 1 + max((v.version for v in self.items.values() if v.proposal_id == proposal_id), default=0)
    def add(self, item): self.items[(item.proposal_id, item.version)] = item; self.audit.append((item.proposal_id, item.version, item.status.value))
    def replace_status(self, item, status):
        current = self.items[(item.proposal_id, item.version)]
        if current.status == status: return current
        updated = replace(current, status=status); self.items[(item.proposal_id, item.version)] = updated
        self.audit.append((item.proposal_id, item.version, status.value)); return updated
    def claim(self, item):
        current = self.items[(item.proposal_id, item.version)]
        if current.status is not ProposalStatus.DRAFT: return None
        return self.replace_status(current, ProposalStatus.APPROVING)
