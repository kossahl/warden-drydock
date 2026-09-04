from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone

from warden_drydock.hosted.engine.models import ExactTextChange, Status, exact_diff_digest
from warden_drydock.hosted.revisions.models import SnapshotManifest, StaleHeadError


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


_PUBLIC_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_DOMAIN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _require_public_id(value, field):
    if not isinstance(value, str) or not 3 <= len(value) <= 80 or _PUBLIC_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is not a safe public identifier")


def _require_domain_id(value, field):
    if not isinstance(value, str) or not 3 <= len(value) <= 200 or _DOMAIN_ID.fullmatch(value) is None:
        raise ValueError(f"{field} is not a safe domain identifier")


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
    generation_id: str | None = None
    source_revision: str | None = None
    source_set_digest: str | None = None
    terminal_draft_digest: str | None = None
    published_revision_id: str | None = None
    editor_metadata: dict | None = None

    def __post_init__(self):
        for field, value in (("proposal_id", self.proposal_id),
                             ("campaign_id", self.campaign_id),
                             ("base_revision", self.base_revision)):
            _require_public_id(value, field)
        if self.version < 1 or not self.changes:
            raise ValueError("proposal version and changes must be non-empty")
        for change in self.changes:
            _require_public_id(change.change_id, "change_id")
            _require_domain_id(change.subject_id, "subject_id")
            if change.record_type is not None:
                _require_domain_id(change.record_type, "record_type")
            if change.expected_content_digest is not None and _DIGEST.fullmatch(change.expected_content_digest) is None:
                raise ValueError("expected_content_digest is not a lowercase SHA-256 digest")
        for field, value in (("diff_digest", self.diff_digest), ("payload_digest", self.payload_digest)):
            if _DIGEST.fullmatch(value) is None:
                raise ValueError(f"{field} is not a lowercase SHA-256 digest")
        provenance = (self.generation_id, self.source_revision, self.source_set_digest, self.terminal_draft_digest)
        if any(value is not None for value in provenance):
            if any(value is None for value in provenance):
                raise ValueError("proposal provenance must be complete")
            _require_public_id(self.generation_id, "generation_id")
            _require_public_id(self.source_revision, "source_revision")
            if self.source_revision != self.base_revision:
                raise ValueError("proposal source revision must equal base revision")
            for field, value in (("source_set_digest", self.source_set_digest), ("terminal_draft_digest", self.terminal_draft_digest)):
                if _DIGEST.fullmatch(value) is None:
                    raise ValueError(f"{field} is not a lowercase SHA-256 digest")
        if self.published_revision_id is not None:
            _require_public_id(self.published_revision_id, "published_revision_id")


def _payload_digest(changes: tuple[ExactTextChange, ...]) -> str:
    payload = [{"id": c.change_id, "subject": c.subject_id, "replacement": c.replacement,
                "expected": c.expected_content_digest, "kind": c.change_kind.value,
                "record_type": c.record_type} for c in changes]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _diff_digest(changes: tuple[ExactTextChange, ...]) -> str:
    return exact_diff_digest(changes)


class ProposalService:
    """Small immutable proposal state machine; authority remains with publisher."""
    def __init__(self, repository, *, head, stage, publish, verify_publication=None,
                 diff_digest=_diff_digest, payload_digest=_payload_digest) -> None:
        self.repository, self._head, self._stage, self._publish = repository, head, stage, publish
        self._verify_publication = verify_publication
        self._diff_digest, self._payload_digest = diff_digest, payload_digest

    @staticmethod
    def _bind_manifest(version, result):
        if not isinstance(result, SnapshotManifest):
            raise ValueError("publication result is not a verified snapshot manifest")
        expected_change_digest = exact_diff_digest(version.changes) if version.editor_metadata else version.diff_digest
        if (result.campaign_id, result.parent_revision, result.change_digest) != (
            version.campaign_id, version.base_revision, expected_change_digest
        ):
            raise ValueError("publication result binding mismatch")
        return result

    def draft(self, proposal_id, campaign_id, base_revision, changes, *, generation_id=None, source_revision=None, source_set_digest=None, terminal_draft_digest=None):
        changes = tuple(changes)
        if not changes or len({c.change_id for c in changes}) != len(changes):
            raise ValueError("proposal changes must be non-empty and uniquely identified")
        item = ProposalVersion(proposal_id, self.repository.next_version(proposal_id), campaign_id,
            base_revision, changes, self._diff_digest(changes), self._payload_digest(changes),
            generation_id=generation_id, source_revision=source_revision,
            source_set_digest=source_set_digest,
            terminal_draft_digest=terminal_draft_digest)
        self.repository.add(item)
        return item

    def correct(self, version, changes, *, base_revision=None):
        corrected = self.repository.correct(
            version, tuple(changes), base_revision,
            diff_digest=self._diff_digest, payload_digest=self._payload_digest,
        )
        if corrected is None:
            raise ValueError("only draft or conflicted versions can be corrected")
        return corrected

    def reject(self, version):
        version = self.repository.reject(version)
        if version is None:
            raise ValueError("only draft versions can be rejected")
        return version

    def approve(self, version, *, diff_digest, base_revision, payload_digest, finalize=None):
        current = self.repository.get(version.proposal_id, version.version) if hasattr(self.repository, "get") else self.repository.items[(version.proposal_id, version.version)]
        if (diff_digest, base_revision, payload_digest) != (current.diff_digest, current.base_revision, current.payload_digest):
            raise ValueError("approval binding mismatch")
        if current.status is ProposalStatus.PUBLISHED:
            return current
        version = self.repository.claim(current)
        if version is None:
            raise ValueError("only the current draft version can be approved")
        try:
            head = self._head(version.campaign_id)
        except Exception:
            return self.repository.replace_status(version, ProposalStatus.DRAFT)
        if head != version.base_revision:
            return self.repository.replace_status(version, ProposalStatus.CONFLICT)
        try:
            staged = self._stage(version)
        except Exception:
            return self.repository.replace_status(version, ProposalStatus.DRAFT)
        if getattr(staged, "status", None) is not Status.STAGED:
            return self.repository.replace_status(version, ProposalStatus.DRAFT)
        try:
            result = self._publish(version, staged, finalize=finalize) if finalize is not None else self._publish(version, staged)
        except StaleHeadError:
            return self.repository.replace_status(version, ProposalStatus.CONFLICT)
        except Exception:
            # The immutable intent may be reconciled later; never replay blindly.
            return self.repository.replace_status(version, ProposalStatus.QUARANTINED)
        if result is not None:
            try:
                self._bind_manifest(version, result)
            except ValueError:
                return self.repository.replace_status(version, ProposalStatus.QUARANTINED)
        status = ProposalStatus.PUBLISHED if result is not None else ProposalStatus.APPROVED
        if hasattr(self.repository, "finalize"):
            return self.repository.finalize(version, status, result)
        return self.repository.replace_status(version, status)

    def reconcile(self, version, result):
        current = self.repository.get(version.proposal_id, version.version)
        if current.status is ProposalStatus.PUBLISHED:
            return current
        if current.status not in (ProposalStatus.APPROVING, ProposalStatus.APPROVED, ProposalStatus.QUARANTINED):
            raise ValueError("proposal is not reconcilable")
        manifest = self._bind_manifest(current, result)
        if self._verify_publication is None:
            raise ValueError("publication verifier is required for reconciliation")
        verified = self._verify_publication(manifest)
        if verified != manifest:
            raise ValueError("publication snapshot verification failed")
        return self.repository.finalize(current, ProposalStatus.PUBLISHED, manifest)


class InMemoryProposalRepository:
    def __init__(self): self.items = {}; self.audit = []; self._lock = threading.RLock(); self._created_at = {}; self._editor_workflow = {}
    def editor_workflow_version(self, campaign_id):
        with self._lock:
            if campaign_id not in self._editor_workflow:
                values = [item.editor_metadata.get("editor_workflow_version", 1) - 1 for item in self.items.values() if item.campaign_id == campaign_id and item.editor_metadata]
                self._editor_workflow[campaign_id] = max(values, default=0) + 1
            return self._editor_workflow[campaign_id]
    def editor_proposals(self):
        with self._lock:
            return tuple(item for item in self.items.values() if item.editor_metadata)
    def add_editor(self, item, campaign_id, expected_version):
        with self._lock:
            if self.editor_workflow_version(campaign_id) != expected_version:
                return False
            if (item.proposal_id, item.version) in self.items:
                raise ValueError("proposal_version_conflict")
            self.items[(item.proposal_id, item.version)] = item
            self._created_at[(item.proposal_id, item.version)] = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=len(self._created_at))
            self._editor_workflow[campaign_id] = expected_version + 1
            self.audit.append((item.proposal_id, item.version, item.status.value))
            return True
    def advance_editor(self, campaign_id, expected_version):
        with self._lock:
            if self.editor_workflow_version(campaign_id) != expected_version:
                return False
            self._editor_workflow[campaign_id] = expected_version + 1
            return True
    def save_editor_metadata(self, proposal_id, version, metadata, published_revision_id=None):
        with self._lock:
            current = self.items[(proposal_id, version)]
            self.items[(proposal_id, version)] = replace(
                current, editor_metadata=metadata,
                published_revision_id=published_revision_id or current.published_revision_id,
            )
            return self.items[(proposal_id, version)]
    def next_version(self, proposal_id): return 1 + max((v.version for v in self.items.values() if v.proposal_id == proposal_id), default=0)
    def get(self, proposal_id, version): return self.items[(proposal_id, version)]
    def versions(self, proposal_id):
        return tuple(sorted((item for item in self.items.values() if item.proposal_id == proposal_id), key=lambda item: item.version))
    def workflow_counts(self, campaign_id, revision_id):
        counts = {name: 0 for name in ("draft", "rejected", "conflict", "published", "quarantined")}
        for item in self.items.values():
            if item.campaign_id == campaign_id and item.base_revision == revision_id and item.status.value in counts:
                counts[item.status.value] += 1
        return counts
    def find_by_published_revision(self, campaign_id, revision_id):
        matches = [item for item in self.items.values() if item.campaign_id == campaign_id and item.published_revision_id == revision_id]
        if len(matches) > 1:
            raise ValueError("proposal_publication_binding_conflict")
        return matches[0] if matches else None
    def add(self, item):
        self.items[(item.proposal_id, item.version)] = item
        self._created_at[(item.proposal_id, item.version)] = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=len(self._created_at))
        self.audit.append((item.proposal_id, item.version, item.status.value))
    def replace_status(self, item, status):
        current = self.items[(item.proposal_id, item.version)]
        if current.status == status: return current
        updated = replace(current, status=status); self.items[(item.proposal_id, item.version)] = updated
        self.audit.append((item.proposal_id, item.version, status.value)); return updated
    def claim(self, item):
        with self._lock:
            current = self.items[(item.proposal_id, item.version)]
            if current.status is not ProposalStatus.DRAFT: return None
            updated = replace(current, status=ProposalStatus.APPROVING)
            self.items[(item.proposal_id, item.version)] = updated
            self.audit.append((item.proposal_id, item.version, updated.status.value))
            return updated
    def reject(self, item):
        with self._lock:
            current = self.items[(item.proposal_id, item.version)]
            if current.status is ProposalStatus.REJECTED: return current
            if current.status is not ProposalStatus.DRAFT: return None
            updated = replace(current, status=ProposalStatus.REJECTED)
            self.items[(item.proposal_id, item.version)] = updated
            self.audit.append((item.proposal_id, item.version, updated.status.value))
            return updated
    def correct(self, item, changes, base_revision, *, diff_digest=_diff_digest, payload_digest=_payload_digest):
        with self._lock:
            current = self.items[(item.proposal_id, item.version)]
            if current.status not in (ProposalStatus.DRAFT, ProposalStatus.CONFLICT): return None
            if not changes or len({c.change_id for c in changes}) != len(changes): raise ValueError("proposal changes must be non-empty and uniquely identified")
            retired = replace(current, status=ProposalStatus.REJECTED)
            version = 1 + max((v.version for v in self.items.values() if v.proposal_id == item.proposal_id), default=0)
            corrected = ProposalVersion(
                item.proposal_id, version, item.campaign_id,
                base_revision or current.base_revision, changes,
                diff_digest(changes), payload_digest(changes),
                generation_id=current.generation_id,
                source_revision=current.source_revision,
                source_set_digest=current.source_set_digest,
                terminal_draft_digest=current.terminal_draft_digest,
            )
            self.items[(item.proposal_id, item.version)] = retired
            self.items[(item.proposal_id, version)] = corrected
            self._created_at[(item.proposal_id, version)] = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=len(self._created_at))
            self.audit.extend(((item.proposal_id, item.version, retired.status.value), (item.proposal_id, version, corrected.status.value)))
            return corrected

    def proposal_rows(self, campaign_id, revision_id):
        return tuple(
            {"item": item, "created_at": self._created_at[(item.proposal_id, item.version)]}
            for item in self.items.values()
            if item.campaign_id == campaign_id and item.base_revision == revision_id
        )

    def finalize(self, item, status, result=None):
        revision_id = result.revision_id if isinstance(result, SnapshotManifest) else None
        current = self.items[(item.proposal_id, item.version)]
        if current.status is status:
            if revision_id is not None and current.published_revision_id != revision_id:
                raise ValueError("proposal_publication_binding_conflict")
            return current
        updated = replace(current, status=status, published_revision_id=revision_id or current.published_revision_id)
        self.items[(item.proposal_id, item.version)] = updated
        self.audit.append((item.proposal_id, item.version, status.value))
        return updated
