from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import re
import tempfile
import threading
from datetime import datetime, timezone

from warden_drydock import __version__
from warden_drydock.hosted.ai.models import Action, CaptureType, GenerationRecord, ProviderConsent
from warden_drydock.hosted.ai.live import LiveSessionService, StaleController, StaleWorkflow
from warden_drydock.hosted.ai.provider import OpenAIResponsesAdapter, ProviderUnavailable
from warden_drydock.hosted.ai.repository import InMemoryAIRepository
from warden_drydock.hosted.ai.retrieval import DeterministicSourceSelector, EngineSourceLoader
from warden_drydock.hosted.ai.service import ConsentRequired, GroundedAIService
from warden_drydock.hosted.engine import (
    DeterministicEngine, ExactTextChange, InitializeRequest, RetrievalKind,
    ChangeKind,
    ContextRequest, RetrievalRequest, StageExactDiffRequest, Status,
    WorkspaceHandle, WorkspaceRegistry, WorkspaceRequest,
    content_digest, exact_diff_digest,
)
from warden_drydock.hosted.proposals.service import (
    InMemoryProposalRepository, ProposalService, ProposalStatus, ProposalVersion,
)
from warden_drydock.hosted.projections import (
    ApprovedHistoryQuery, AtlasProjectionRebuilder, AtlasQueryService, Authority,
    InMemoryAtlasProjectionRepository, NeighborhoodQuery, RecordLibraryQuery,
    approved_history_contract, campaign_collection_contract,
    neighborhood_contract, overview_contract, record_detail_contract,
    record_library_contract, workflow_summary_contract,
)
from warden_drydock.hosted.projections.atlas_models import decode_cursor, encode_cursor
from warden_drydock.hosted.revisions import (
    FileSnapshotStore, InMemoryWorkflowRepository, IntentStatus,
    PublicationIntent, PublicationIntentError, PublicationKind, RevisionService,
    SnapshotManifest, StaleHeadError, canonicalize_tree,
)
from warden_drydock.standalone import frontmatter

from .contracts import (
    HTTPContractSemanticError, append_draft, canonical_digest, normalize_text,
    text_digest, validate_http_semantics,
)
from .repository import InMemoryHTTPRepository, ReceiptConflict
from .editor import change_for, diff_digest as editor_diff_digest, parse_document, serialize_document, document_digest, _document
from .editor_semantics import EditorSemanticError, validate_editor_semantics


_PUBLIC_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_DOMAIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _slice_payload_digest(changes: tuple[ExactTextChange, ...]) -> str:
    if len(changes) != 1:
        raise ValueError("the browser slice requires exactly one proposal change")
    change = changes[0]
    value = {"id": change.change_id, "subject": change.subject_id,
             "replacement": change.replacement,
             "expected": change.expected_content_digest,
             "kind": change.change_kind.value, "record_type": change.record_type}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _slice_diff_digest(changes: tuple[ExactTextChange, ...]) -> str:
    if len(changes) != 1:
        raise ValueError("the browser slice requires exactly one proposal change")
    change = changes[0]
    value = {"change_id": change.change_id, "change_kind": change.change_kind.value,
             "expected_content_digest": change.expected_content_digest,
             "record_type": change.record_type,
             "replacement_digest": content_digest(change.replacement),
             "subject_id": change.subject_id}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class HTTPFailure(RuntimeError):
    def __init__(self, status: int, category: str, code: str, stage: str, request_id: str = "request_http", retryable: bool = False) -> None:
        self.status = status
        self.payload = {
            "contract_name": "error_response", "contract_version": 2,
            "error": {"category": category, "code": code, "stage": stage,
                      "request_id": request_id, "retryable": retryable},
        }
        super().__init__(code)


class SyntheticProvider:
    """Deterministic provider for tests and the synthetic browser demonstration."""

    adapter_version = "synthetic_v1"

    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def verify(self) -> bool:
        return True

    def credential_revision_fingerprint(self) -> str:
        return hashlib.sha256(b"synthetic-provider-credential").hexdigest()

    def stream(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderUnavailable("synthetic unavailable")
        name = request.envelope.excerpts[0].source_id
        yield "delta", f"The grounded source is {name}."
        yield "completion", None


@dataclass
class CampaignState:
    campaign_id: str
    campaign_name: str
    adapter_id: str
    revisions: dict[str, SnapshotManifest]
    workspaces: dict[str, object]


class SliceApplication:
    """Local Warden-only HTTP application composed from typed hosted services."""

    validation_contract_digest = hashlib.sha256(b"hosted-http-v2").hexdigest()
    _request_fields = {
        "provider_consent_request": {"contract_name", "contract_version", "operation_request", "input"},
        "campaign_create_request": {"contract_name", "contract_version", "operation_request", "input"},
        "generation_start_request": {"contract_name", "contract_version", "generation_id", "campaign_id", "source_revision", "action", "prompt", "context", "session_id"},
        "proposal_create_request": {"contract_name", "contract_version", "request_id", "idempotency_key", "payload_digest", "generation_id", "proposal_id", "campaign_id", "source_revision", "base_revision", "source_set_digest", "terminal_draft_digest", "subject_id"},
        "proposal_correction_request": {"contract_name", "contract_version", "operation_request", "proposal_id", "proposal_version", "source_revision", "base_revision", "change_id", "subject_id", "after_content"},
        "proposal_rejection_request": {"contract_name", "contract_version", "operation_request", "proposal_id", "proposal_version", "source_revision", "base_revision"},
        "proposal_approval_request": {"contract_name", "contract_version", "operation_request", "proposal_id", "proposal_version", "source_revision", "base_revision", "expected_campaign_head", "diff_digest", "proposal_payload_digest", "warden_confirmed"},
        "live_start_request": {"contract_name", "contract_version", "operation_request", "campaign_id", "session_id", "head_revision", "controller_id"},
        "live_takeover_request": {"contract_name", "contract_version", "operation_request", "campaign_id", "session_id", "controller_id", "controller_epoch"},
        "live_capture_request": {"contract_name", "contract_version", "operation_request", "campaign_id", "session_id", "controller_id", "controller_epoch", "event_id", "device_id", "operation_id", "device_order", "capture_type", "text", "record_id"},
        "live_end_request": {"contract_name", "contract_version", "operation_request", "campaign_id", "session_id", "controller_id", "controller_epoch", "device_id", "operation_id", "required_operation_ids"},
    }

    def __init__(self, root: Path | None = None, *, snapshot_root: Path | None = None,
                 provider=None, receipts=None,
                 proposal_repository=None, workflow_repository=None,
                 ai_repository=None, atlas_repository=None) -> None:
        self._temporary = None
        if root is None:
            self._temporary = tempfile.TemporaryDirectory()
            root = Path(self._temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        self._runtime_root = root.resolve()
        self.registry = WorkspaceRegistry(root / "workspaces")
        self.registry.recover_existing()
        self.engine = DeterministicEngine(self.registry)
        self.workflow = workflow_repository or InMemoryWorkflowRepository()
        self.revisions = RevisionService(
            FileSnapshotStore(snapshot_root or root / "revision-store"), self.workflow,
        )
        self.proposal_repository = proposal_repository or InMemoryProposalRepository()
        self.proposals = ProposalService(
            self.proposal_repository, head=self.workflow.head,
            stage=self._stage, publish=self._publish,
            verify_publication=lambda manifest: self.revisions.store.verify(
                manifest.tree_digest, manifest.campaign_id, manifest.revision_id,
            ),
            diff_digest=_slice_diff_digest, payload_digest=_slice_payload_digest,
        )
        self.receipts = receipts or InMemoryHTTPRepository()
        self.atlas_repository = atlas_repository or InMemoryAtlasProjectionRepository()
        self._atlas_provenance_overrides: dict[str, tuple[str, int]] = {}
        self.atlas_rebuilder = AtlasProjectionRebuilder(
            self.revisions.store, self.atlas_repository, self.workflow,
            proposal_provenance=self._atlas_provenance,
        )
        self.atlas = AtlasQueryService(self.atlas_repository)
        self.ai_repository = ai_repository or InMemoryAIRepository()
        self.live = LiveSessionService(self.ai_repository)
        self.provider = provider or OpenAIResponsesAdapter()
        loader = EngineSourceLoader(self.engine, self._workspace_for_revision)
        self.ai = GroundedAIService(
            self.ai_repository, DeterministicSourceSelector(), self.provider, loader,
            focus_verifier=self._verify_generation_focus,
        )
        self.campaigns: dict[str, CampaignState] = {}
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._editor_mutation_lock = threading.RLock()
        self._dispatching: set[str] = set()
        self._editor_workflow: dict[str, int] = {}
        self._editor_proposals: dict[tuple[str, int], dict] = {}
        self._recover_pending_atlas_publications()
        self._recover_state()
        self._recover_editor_state()
        self._recover_atlas()
        self._abandoned_claims = set(self.receipts.recover_pending())

    @staticmethod
    def _id(prefix: str, *parts: object) -> str:
        digest = hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:20]
        return f"{prefix}_{digest}"

    @staticmethod
    def _request_id(payload: dict) -> str:
        operation = payload.get("operation_request") or payload
        return operation.get("request_id", payload.get("request_id", "request_http"))

    @staticmethod
    def _authority(status: str) -> str:
        return status if status in {"canon", "revealed"} else "preparation"

    def _recover_state(self) -> None:
        inventory = self.revisions.verify_linear_inventory()
        grouped: dict[str, list[SnapshotManifest]] = {}
        for manifest in inventory:
            grouped.setdefault(manifest.campaign_id, []).append(manifest)
        recovered: dict[str, CampaignState] = {}
        workspace_number = 0
        for campaign_id, manifests in sorted(grouped.items()):
            head = max(manifests, key=lambda item: item.ordinal)
            if self.workflow.head(campaign_id) != head.revision_id:
                raise RuntimeError("http_snapshot_head_mismatch")
            head_tree = self.revisions.store.snapshots / head.tree_digest / campaign_id / head.revision_id / "tree"
            campaign_name = None
            for file_hash in head.files:
                if file_hash.relative_path.endswith(".md"):
                    metadata = frontmatter((head_tree / file_hash.relative_path).read_text(encoding="utf-8"))
                    if metadata.get("id") == "campaign-main":
                        campaign_name = metadata.get("name")
                        break
            if not isinstance(campaign_name, str) or not campaign_name:
                raise RuntimeError("http_campaign_metadata_missing")
            revisions: dict[str, SnapshotManifest] = {}
            workspaces: dict[str, WorkspaceHandle] = {}
            for manifest in sorted(manifests, key=lambda item: item.ordinal):
                workspace_number += 1
                handle = WorkspaceHandle(f"workspace_{workspace_number:08d}")
                tree = self.revisions.store.snapshots / manifest.tree_digest / campaign_id / manifest.revision_id / "tree"
                try:
                    workspace = self.registry._resolve(handle)
                    files, digest = canonicalize_tree(workspace)
                except Exception:
                    files, digest = (), ""
                if files != manifest.files or digest != manifest.tree_digest:
                    self.registry.materialize(handle, tree)
                revisions[manifest.revision_id] = manifest
                workspaces[manifest.revision_id] = handle
            recovered[campaign_id] = CampaignState(campaign_id, campaign_name, "mothership", revisions, workspaces)
        self.campaigns = recovered

    @property
    def _editor_state_file(self) -> Path:
        return self._runtime_root / "editor-state-v1.json"

    def _persist_editor_state(self) -> None:
        value = {"workflow": self._editor_workflow, "proposals": []}
        for (proposal_id, version), stored in self._editor_proposals.items():
            item = self.proposal_repository.get(proposal_id, version)
            value["proposals"].append({
                "proposal_id": proposal_id, "version": version,
                "value": stored["value"], "change": {
                    "change_id": stored["change"].change_id,
                    "subject_id": stored["change"].subject_id,
                    "expected_content_digest": stored["change"].expected_content_digest,
                    "replacement": stored["change"].replacement,
                    "change_kind": stored["change"].change_kind.value,
                    "record_type": stored["change"].record_type,
                }, "campaign_id": item.campaign_id, "base": item.base_revision,
            })
        temporary = self._editor_state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self._editor_state_file)

    def _recover_editor_state(self) -> None:
        # Proposal rows are authoritative.  Rebuild the editor cache from the
        # immutable metadata persisted with each row; the old sidecar is only
        # a fallback for rows written by the pre-binding implementation.
        persisted = getattr(self.proposal_repository, "editor_proposals", None)
        rows = ((item.proposal_id, item.version, item) for item in persisted()) if persisted is not None else ((key[0], key[1], item) for key, item in getattr(self.proposal_repository, "items", {}).items())
        for proposal_id, version, item in rows:
            key = (proposal_id, version)
            value = item.editor_metadata
            if not isinstance(value, dict) or value.get("contract_name") != "editor_proposal_view":
                continue
            if value.get("campaign_id") != item.campaign_id or value.get("proposal_version") != item.version:
                raise RuntimeError("editor_state_integrity_failure")
            change = item.changes[0]
            self._editor_proposals[key] = {
                "value": value, "change": change,
                "campaign_id": item.campaign_id, "base": item.base_revision,
                "workflow": value["editor_workflow_version"],
            }
            self._editor_workflow[item.campaign_id] = max(
                self._editor_workflow.get(item.campaign_id, 1),
                value["editor_workflow_version"],
            )
        if self._editor_proposals:
            return
        if not self._editor_state_file.exists():
            return
        try:
            value = json.loads(self._editor_state_file.read_text(encoding="utf-8"))
            self._editor_workflow = {str(k): int(v) for k, v in value.get("workflow", {}).items()}
            for stored in value.get("proposals", []):
                proposal_id, version = stored["proposal_id"], int(stored["version"])
                item = self.proposal_repository.get(proposal_id, version)
                if item is None:
                    continue
                if isinstance(item.editor_metadata, dict) and item.editor_metadata.get("contract_name") == "editor_proposal_view":
                    self._editor_proposals[(proposal_id, version)] = {
                        "value": item.editor_metadata, "change": item.changes[0],
                        "campaign_id": item.campaign_id, "base": item.base_revision,
                        "workflow": item.editor_metadata["editor_workflow_version"],
                    }
                    continue
                change = stored["change"]
                self._editor_proposals[(proposal_id, version)] = {
                    "value": stored["value"], "change": ExactTextChange(
                        change["change_id"], change["subject_id"], change["expected_content_digest"],
                        change["replacement"], ChangeKind(change["change_kind"]), change["record_type"],
                    ), "campaign_id": item.campaign_id, "base": item.base_revision,
                    "workflow": stored["value"]["editor_workflow_version"],
                }
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise RuntimeError("editor_state_integrity_failure")

    def _delete_pending_atlas_projection(self, manifest: SnapshotManifest) -> None:
        try:
            self.atlas_repository.delete(manifest.campaign_id, manifest.revision_id)
        except Exception:
            # Atlas reads independently require finalized campaign inventory.
            pass

    def _discard_pending_publication(self, manifest: SnapshotManifest) -> None:
        self.revisions.store.discard_unpublished_snapshot(manifest)
        self._delete_pending_atlas_projection(manifest)

    def _quarantine_invalid_pending_publication(
        self, manifest: SnapshotManifest, intent: PublicationIntent
    ) -> None:
        self.revisions.store.quarantine_snapshot(
            manifest.tree_digest, manifest.campaign_id, manifest.revision_id,
            "pending Atlas publication failed integrity verification",
        )
        self.workflow.quarantine_intent(intent.intent_id)
        self._delete_pending_atlas_projection(manifest)

    def _recover_pending_atlas_publications(self) -> None:
        """Resolve the snapshot/projection-before-head crash window deterministically."""
        for manifest in self.revisions.store.inventory():
            matches = self.workflow.matching_intents(
                manifest.publication_intent_token
            )
            if len(matches) != 1 or matches[0].status is not IntentStatus.PENDING:
                continue
            intent = matches[0]
            try:
                candidate = self.atlas_repository.get(
                    manifest.campaign_id, manifest.revision_id
                )
            except KeyError:
                self._discard_pending_publication(manifest)
                continue
            try:
                provenance = (
                    candidate.history_entry.proposal_id,
                    candidate.history_entry.proposal_version,
                )
                if provenance == (None, None):
                    if intent.kind is not PublicationKind.CREATION:
                        raise ValueError("pending approval projection lacks provenance")
                else:
                    proposal_id, proposal_version = provenance
                    proposal = self.proposal_repository.get(
                        proposal_id, proposal_version
                    )
                    if (
                        intent.kind is not PublicationKind.APPROVAL
                        or proposal is None
                        or proposal.status not in {
                            ProposalStatus.APPROVING,
                            ProposalStatus.APPROVED,
                            ProposalStatus.QUARANTINED,
                        }
                        or proposal.campaign_id != manifest.campaign_id
                        or proposal.base_revision != manifest.parent_revision
                        or proposal.diff_digest != manifest.change_digest
                        or manifest.publication_intent_token
                        != self._id("token", proposal_id, proposal_version)
                    ):
                        raise ValueError("pending approval provenance is invalid")
                    self._atlas_provenance_overrides[manifest.revision_id] = (
                        proposal_id, proposal_version
                    )
                try:
                    expected = self.atlas_rebuilder.build(
                        manifest, allow_pending_target=True
                    )
                finally:
                    self._atlas_provenance_overrides.pop(manifest.revision_id, None)
                if expected != candidate:
                    raise ValueError("pending Atlas projection binding mismatch")
                self.revisions.reconcile_manifest(manifest)
            except (KeyError, ValueError):
                self._quarantine_invalid_pending_publication(manifest, intent)
            except (PublicationIntentError, StaleHeadError):
                self._delete_pending_atlas_projection(manifest)

    def _atlas_provenance(self, campaign_id: str, revision_id: str):
        override = self._atlas_provenance_overrides.get(revision_id)
        if override is not None:
            return override
        item = self.proposal_repository.find_by_published_revision(
            campaign_id, revision_id
        )
        return (item.proposal_id, item.version) if item is not None else None

    def _recover_atlas(self) -> None:
        for campaign in self.campaigns.values():
            for manifest in sorted(campaign.revisions.values(), key=lambda item: item.ordinal):
                try:
                    projected = self.atlas_repository.get(
                        campaign.campaign_id, manifest.revision_id
                    )
                except KeyError:
                    projected = None
                if (
                    projected is None
                    or projected.tree_digest != manifest.tree_digest
                    or projected.projection_version != self.atlas_rebuilder.projection_version
                ):
                    self.atlas_rebuilder.rebuild(manifest)

    def _verify_generation_focus(
        self, campaign_id: str, revision_id: str, record_id: str, digest: str
    ) -> bool:
        try:
            record = self.atlas.record_detail(campaign_id, revision_id, record_id)
        except (KeyError, ValueError):
            return False
        return hmac.compare_digest(record.content_digest, digest)

    def _atlas_binding(
        self, campaign_id: str, revision_id: str, ordinal: int, tree_digest: str
    ):
        if (
            not isinstance(campaign_id, str)
            or not 3 <= len(campaign_id) <= 80
            or _PUBLIC_ID.fullmatch(campaign_id) is None
            or not isinstance(revision_id, str)
            or not 3 <= len(revision_id) <= 80
            or _PUBLIC_ID.fullmatch(revision_id) is None
            or not isinstance(tree_digest, str)
            or _DIGEST.fullmatch(tree_digest) is None
            or type(ordinal) is not int
            or ordinal < 1
        ):
            raise HTTPFailure(422, "unsafe_binding", "invalid_revision_binding", "atlas_read")
        try:
            viewed = self.atlas_repository.get(campaign_id, revision_id)
        except (KeyError, ValueError) as exc:
            raise HTTPFailure(404, "not_found", "revision_not_found", "atlas_read") from exc
        campaign = self.campaigns.get(campaign_id)
        manifest = campaign.revisions.get(revision_id) if campaign is not None else None
        if manifest is None or not self.workflow.publication_eligible(manifest):
            raise HTTPFailure(404, "not_found", "revision_not_found", "atlas_read")
        if (
            manifest.ordinal != ordinal
            or not hmac.compare_digest(manifest.tree_digest, tree_digest)
        ):
            raise HTTPFailure(422, "unsafe_binding", "invalid_revision_binding", "atlas_read")
        if viewed.ordinal != ordinal or not hmac.compare_digest(viewed.tree_digest, tree_digest):
            raise HTTPFailure(422, "unsafe_binding", "invalid_revision_binding", "atlas_read")
        head_id = self.workflow.head(campaign_id)
        if head_id is None:
            raise HTTPFailure(409, "snapshot_lineage_failure", "campaign_head_unavailable", "atlas_read")
        try:
            head = self.atlas_repository.get(campaign_id, head_id)
        except (KeyError, ValueError) as exc:
            raise HTTPFailure(409, "snapshot_integrity_failure", "head_projection_unavailable", "atlas_read") from exc
        return viewed, head

    def campaign_collection(self) -> tuple[int, dict]:
        items = []
        for campaign_id in sorted(self.campaigns):
            head_id = self.workflow.head(campaign_id)
            if head_id is None:
                raise HTTPFailure(409, "snapshot_lineage_failure", "campaign_head_unavailable", "campaign_recovery")
            try:
                head = self.atlas_repository.get(campaign_id, head_id)
            except (KeyError, ValueError) as exc:
                raise HTTPFailure(409, "snapshot_integrity_failure", "head_projection_unavailable", "campaign_recovery") from exc
            items.append((head, head, "ready"))
        return 200, campaign_collection_contract(tuple(items))

    def atlas_overview(self, campaign_id: str, revision_id: str, ordinal: int, tree_digest: str) -> tuple[int, dict]:
        viewed, head = self._atlas_binding(campaign_id, revision_id, ordinal, tree_digest)
        return 200, overview_contract(
            viewed, head, approved_revision_count=len(self.atlas_repository.list(campaign_id))
        )

    def atlas_record_library(self, campaign_id: str, revision_id: str, ordinal: int,
                             tree_digest: str, *, query: str, record_types: tuple[str, ...],
                             authorities: tuple[str, ...], statuses: tuple[str, ...],
                             limit: int, cursor: str | None) -> tuple[int, dict]:
        try:
            request = RecordLibraryQuery(
                campaign_id, revision_id, tree_digest, query,
                tuple(sorted(set(record_types))),
                tuple(sorted({Authority(item) for item in authorities}, key=lambda item: item.value)),
                tuple(sorted(set(statuses))), limit, cursor,
            )
            self.atlas.validate_record_library_cursor(request)
            viewed, head = self._atlas_binding(
                campaign_id, revision_id, ordinal, tree_digest
            )
            result = self.atlas.record_library(request)
        except ValueError as exc:
            code = "invalid_cursor_binding" if str(exc) == "invalid_cursor_binding" else "invalid_query_binding"
            raise HTTPFailure(422, "unsafe_binding", code, "atlas_record_library") from exc
        return 200, record_library_contract(result, viewed, head)

    def atlas_record_detail(self, campaign_id: str, record_id: str, revision_id: str,
                            ordinal: int, tree_digest: str) -> tuple[int, dict]:
        if (
            not isinstance(record_id, str)
            or not 1 <= len(record_id) <= 80
            or _DOMAIN_ID.fullmatch(record_id) is None
        ):
            raise HTTPFailure(
                422, "unsafe_binding", "invalid_record_binding", "atlas_record_detail"
            )
        viewed, head = self._atlas_binding(campaign_id, revision_id, ordinal, tree_digest)
        try:
            record = self.atlas.record_detail(campaign_id, revision_id, record_id)
        except KeyError as exc:
            raise HTTPFailure(404, "not_found", "record_not_found", "atlas_record_detail") from exc
        except ValueError as exc:
            raise HTTPFailure(422, "unsafe_binding", "invalid_record_binding", "atlas_record_detail") from exc
        return 200, record_detail_contract(record, viewed, head)

    def atlas_neighborhood(self, campaign_id: str, record_id: str, revision_id: str,
                           ordinal: int, tree_digest: str, *, depth: int, limit: int,
                           cursor: str | None) -> tuple[int, dict]:
        if depth != 1:
            raise HTTPFailure(422, "unsafe_binding", "invalid_depth", "atlas_neighborhood")
        try:
            request = NeighborhoodQuery(
                campaign_id, revision_id, tree_digest, record_id, limit, cursor
            )
            self.atlas.validate_neighborhood_cursor(request)
            viewed, head = self._atlas_binding(
                campaign_id, revision_id, ordinal, tree_digest
            )
            value = self.atlas.neighborhood(request)
        except KeyError as exc:
            raise HTTPFailure(404, "not_found", "record_not_found", "atlas_neighborhood") from exc
        except ValueError as exc:
            code = "invalid_cursor_binding" if str(exc) == "invalid_cursor_binding" else "invalid_query_binding"
            raise HTTPFailure(422, "unsafe_binding", code, "atlas_neighborhood") from exc
        return 200, neighborhood_contract(value, viewed, head)

    def atlas_history(self, campaign_id: str, revision_id: str, ordinal: int,
                      tree_digest: str, *, subject_record_id: str | None,
                      limit: int, cursor: str | None, direction: str) -> tuple[int, dict]:
        try:
            request = ApprovedHistoryQuery(
                campaign_id, revision_id, tree_digest, subject_record_id,
                limit, cursor, direction,
            )
            self.atlas.validate_history_cursor(request)
            viewed, head = self._atlas_binding(
                campaign_id, revision_id, ordinal, tree_digest
            )
            result = self.atlas.approved_history(request)
        except ValueError as exc:
            code = "invalid_cursor_binding" if str(exc) == "invalid_cursor_binding" else "invalid_query_binding"
            raise HTTPFailure(422, "unsafe_binding", code, "atlas_history") from exc
        bundles = {item.revision_id: item for item in self.atlas_repository.list(campaign_id)}
        return 200, approved_history_contract(result, viewed, head, bundles)

    def atlas_workflow_summary(self, campaign_id: str, revision_id: str,
                               ordinal: int, tree_digest: str) -> tuple[int, dict]:
        viewed, head = self._atlas_binding(campaign_id, revision_id, ordinal, tree_digest)
        session = self.ai_repository.active_session(campaign_id)
        active = None
        if session is not None:
            try:
                base = self.atlas_repository.get(campaign_id, session.base_revision)
            except KeyError as exc:
                raise HTTPFailure(409, "stale_revision", "session_base_revision_unavailable", "atlas_workflow") from exc
            active = {
                "session_id": session.session_id,
                "base_revision": {
                    "revision_id": base.revision_id, "ordinal": base.ordinal,
                    "tree_digest": base.tree_digest,
                },
                "workflow_version": session.workflow_version,
                "confirmed_table_fact_count": sum(item.capture_type.value == "confirmed_fact" for item in session.captures),
                "unresolved_question_count": sum(item.capture_type.value == "unresolved_question" for item in session.captures),
            }
        return 200, workflow_summary_contract(
            viewed, head,
            draft_generation_count=self.ai_repository.draft_generation_count(campaign_id, revision_id),
            proposal_counts=self.proposal_repository.workflow_counts(campaign_id, revision_id),
            active_session=active,
        )

    @staticmethod
    def _rfc3339(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _revision_contract(bundle) -> dict[str, object]:
        return {
            "revision_id": bundle.revision_id,
            "ordinal": bundle.ordinal,
            "tree_digest": bundle.tree_digest,
        }

    @classmethod
    def _workflow_binding(cls, campaign_id: str, viewed, head) -> dict[str, object]:
        return {
            "campaign_id": campaign_id,
            "viewed_revision": cls._revision_contract(viewed),
            "head_revision": cls._revision_contract(head),
        }

    @staticmethod
    def _workflow_page(rows, *, cursor: str | None, binding: dict[str, object], boundary_fields: tuple[str, ...]):
        start = 0
        if cursor is not None:
            decoded = decode_cursor(cursor)
            expected = dict(binding)
            for field in boundary_fields:
                expected[field] = decoded.get(field)
            if decoded != expected or any(decoded.get(field) is None for field in boundary_fields):
                raise ValueError("invalid_cursor_binding")
            boundary = tuple(decoded[field] for field in boundary_fields)
            identities = [tuple(row[field] for field in boundary_fields) for row in rows]
            try:
                start = identities.index(boundary) + 1
            except ValueError as exc:
                raise ValueError("invalid_cursor_binding") from exc
        return start

    def atlas_generation_collection(
        self, campaign_id: str, revision_id: str, ordinal: int, tree_digest: str,
        *, actions: tuple[str, ...], statuses: tuple[str, ...], record_id: str | None,
        limit: int, cursor: str | None,
    ) -> tuple[int, dict]:
        allowed_actions = {item.value for item in Action}
        allowed_statuses = {"pending", "complete", "failed", "cancelled"}
        if any(item not in allowed_actions for item in actions) or any(item not in allowed_statuses for item in statuses):
            raise HTTPFailure(422, "unsafe_binding", "invalid_query_binding", "atlas_generations")
        if record_id is not None and (not 1 <= len(record_id) <= 80 or _DOMAIN_ID.fullmatch(record_id) is None):
            raise HTTPFailure(422, "unsafe_binding", "invalid_query_binding", "atlas_generations")
        viewed, head = self._atlas_binding(campaign_id, revision_id, ordinal, tree_digest)
        if record_id is not None:
            try:
                self.atlas.record_detail(campaign_id, revision_id, record_id)
            except KeyError as exc:
                raise HTTPFailure(404, "not_found", "record_not_found", "atlas_generations") from exc
        action_filter, status_filter = set(actions), set(statuses)
        rows = []
        try:
            stored_rows = self.ai_repository.generation_rows(campaign_id, revision_id)
            for stored in stored_rows:
                record = stored["record"]
                request = record.request
                if request.envelope.campaign_id != campaign_id or request.envelope.revision_id != revision_id:
                    raise ValueError("source_digest_conflict")
                context = ({"scope": "record", "record_id": request.focus_record_id,
                            "content_digest": request.focus_content_digest}
                           if request.focus_record_id is not None else {"scope": "campaign"})
                if request.focus_record_id is not None and not self._verify_generation_focus(
                    campaign_id, revision_id, request.focus_record_id, request.focus_content_digest or ""
                ):
                    raise ValueError("source_digest_conflict")
                status = record.terminal_status or "pending"
                if action_filter and request.action.value not in action_filter:
                    continue
                if status_filter and status not in status_filter:
                    continue
                if record_id is not None and request.focus_record_id != record_id:
                    continue
                retryable = next(
                    (event.retryable for event in reversed(record.events)
                     if event.event_type in {"completion", "failure", "cancel"}), None
                )
                rows.append({
                    "generation_id": request.generation_id,
                    "action": request.action.value,
                    "context": context,
                    "source_revision": self._revision_contract(viewed),
                    "source_set_digest": request.envelope.source_set_digest,
                    "status": status,
                    "retryable": retryable,
                    "created_at": self._rfc3339(stored["created_at"]),
                })
        except ValueError as exc:
            raise HTTPFailure(
                409, "source_digest_conflict", "generation_provenance_mismatch",
                "atlas_generations",
            ) from exc
        rows.sort(key=lambda row: (row["created_at"], row["generation_id"]), reverse=True)
        cursor_binding = {
            "kind": "atlas_generations", "campaign_id": campaign_id,
            "revision_id": revision_id, "revision_ordinal": ordinal,
            "tree_digest": tree_digest, "actions": sorted(action_filter),
            "statuses": sorted(status_filter), "record_id": record_id, "limit": limit,
            "sort": "created_at_desc_generation_id_desc", "direction": "forward",
        }
        try:
            start = self._workflow_page(rows, cursor=cursor, binding=cursor_binding,
                                        boundary_fields=("created_at", "generation_id"))
        except ValueError as exc:
            raise HTTPFailure(422, "invalid_cursor_binding", "invalid_cursor_binding", "atlas_generations") from exc
        items = rows[start:start + limit]
        next_cursor = None
        if items and start + limit < len(rows):
            next_cursor = encode_cursor({**cursor_binding, "created_at": items[-1]["created_at"],
                                         "generation_id": items[-1]["generation_id"]})
        return 200, {
            "contract_name": "atlas_generation_collection", "contract_version": 2,
            "binding": self._workflow_binding(campaign_id, viewed, head),
            "filters": {"actions": sorted(action_filter), "statuses": sorted(status_filter), "record_id": record_id},
            "limit": limit, "sort": "created_at_desc_generation_id_desc",
            "items": items, "next_cursor": next_cursor,
        }

    def atlas_proposal_collection(
        self, campaign_id: str, revision_id: str, ordinal: int, tree_digest: str,
        *, statuses: tuple[str, ...], record_id: str | None,
        limit: int, cursor: str | None,
    ) -> tuple[int, dict]:
        allowed_statuses = {"draft", "rejected", "conflict", "published", "quarantined"}
        if any(item not in allowed_statuses for item in statuses):
            raise HTTPFailure(422, "unsafe_binding", "invalid_query_binding", "atlas_proposals")
        if record_id is not None and (not 1 <= len(record_id) <= 80 or _DOMAIN_ID.fullmatch(record_id) is None):
            raise HTTPFailure(422, "unsafe_binding", "invalid_query_binding", "atlas_proposals")
        viewed, head = self._atlas_binding(campaign_id, revision_id, ordinal, tree_digest)
        if record_id is not None:
            try:
                self.atlas.record_detail(campaign_id, revision_id, record_id)
            except KeyError as exc:
                raise HTTPFailure(404, "not_found", "record_not_found", "atlas_proposals") from exc
        status_filter = set(statuses)
        rows = []
        try:
            for stored in self.proposal_repository.proposal_rows(campaign_id, revision_id):
                item = stored["item"]
                if item.generation_id is None or item.source_revision != revision_id or len(item.changes) != 1:
                    raise ValueError("source_digest_conflict")
                generation = self.ai_repository.get_generation(item.generation_id)
                if generation is None:
                    raise ValueError("source_digest_conflict")
                request = generation.request
                change = item.changes[0]
                if (
                    request.campaign_id != campaign_id
                    or request.revision_id != revision_id
                    or request.envelope.source_set_digest != item.source_set_digest
                    or change.expected_content_digest is None
                ):
                    raise ValueError("source_digest_conflict")
                context = ({"scope": "record", "record_id": request.focus_record_id,
                            "content_digest": request.focus_content_digest}
                           if request.focus_record_id is not None else {"scope": "campaign"})
                if request.focus_record_id is not None and not self._verify_generation_focus(
                    campaign_id, revision_id, request.focus_record_id, request.focus_content_digest or ""
                ):
                    raise ValueError("source_digest_conflict")
                status = item.status.value
                matching_publication = (
                    self._matching_publication(item)
                    if item.status in {
                        ProposalStatus.APPROVING, ProposalStatus.APPROVED,
                        ProposalStatus.PUBLISHED, ProposalStatus.QUARANTINED,
                    }
                    else None
                )
                if item.status is ProposalStatus.PUBLISHED and matching_publication is None:
                    raise HTTPFailure(
                        503, "service_unavailable", "proposal_publication_unverified",
                        "atlas_proposals", retryable=True,
                    )
                if matching_publication is not None:
                    status = "published"
                elif status in {"approving", "approved"}:
                    status = "draft"
                if status_filter and status not in status_filter:
                    continue
                if record_id is not None and change.subject_id != record_id:
                    continue
                rows.append({
                    "proposal_id": item.proposal_id, "proposal_version": item.version,
                    "generation_id": item.generation_id, "action": request.action.value,
                    "context": context, "subject_record_id": change.subject_id,
                    "subject_content_digest": change.expected_content_digest,
                    "source_revision": self._revision_contract(viewed),
                    "base_revision": self._revision_contract(viewed), "status": status,
                    "validation_status": "passed",
                    "published_revision_id": (
                        matching_publication.revision_id
                        if matching_publication is not None
                        else item.published_revision_id
                    ),
                    "created_at": self._rfc3339(stored["created_at"]),
                })
        except ValueError as exc:
            if str(exc) in {"source_digest_conflict", "unsafe_binding"}:
                raise HTTPFailure(409, "source_digest_conflict", "proposal_provenance_mismatch", "atlas_proposals") from exc
            raise
        rows.sort(key=lambda row: (row["created_at"], row["proposal_id"], row["proposal_version"]), reverse=True)
        cursor_binding = {
            "kind": "atlas_proposals", "campaign_id": campaign_id,
            "revision_id": revision_id, "revision_ordinal": ordinal,
            "tree_digest": tree_digest, "statuses": sorted(status_filter),
            "record_id": record_id, "limit": limit,
            "sort": "created_at_desc_proposal_id_desc_proposal_version_desc", "direction": "forward",
        }
        try:
            start = self._workflow_page(rows, cursor=cursor, binding=cursor_binding,
                                        boundary_fields=("created_at", "proposal_id", "proposal_version"))
        except ValueError as exc:
            raise HTTPFailure(422, "invalid_cursor_binding", "invalid_cursor_binding", "atlas_proposals") from exc
        items = rows[start:start + limit]
        next_cursor = None
        if items and start + limit < len(rows):
            last = items[-1]
            next_cursor = encode_cursor({**cursor_binding, "created_at": last["created_at"],
                                         "proposal_id": last["proposal_id"],
                                         "proposal_version": last["proposal_version"]})
        return 200, {
            "contract_name": "atlas_proposal_collection", "contract_version": 2,
            "binding": self._workflow_binding(campaign_id, viewed, head),
            "filters": {"statuses": sorted(status_filter), "record_id": record_id},
            "limit": limit, "sort": "created_at_desc_proposal_id_desc_proposal_version_desc",
            "items": items, "next_cursor": next_cursor,
        }

    def _closed_request(self, payload: dict, expected: str, stage: str) -> None:
        fields = set(payload)
        allowed = self._request_fields[expected]
        required = allowed - ({"session_id"} if expected == "generation_start_request" else set())
        if payload.get("contract_name") != expected or payload.get("contract_version") != 2 or not required <= fields or not fields <= allowed:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        operation = payload.get("operation_request")
        if operation is not None:
            base = {"contract_name", "contract_version", "request_id", "operation", "idempotency_key", "payload_digest", "expected_revision", "expected_workflow_version"}
            if expected in {"proposal_correction_request", "proposal_rejection_request"}:
                base.add("subject_id")
            elif expected == "proposal_approval_request":
                base.update({"subject_id", "intent_digest"})
            if not isinstance(operation, dict) or set(operation) != base or operation.get("contract_name") != "operation_request" or operation.get("contract_version") != 2:
                raise HTTPFailure(422, "unsafe_binding", "invalid_operation_shape", stage, self._request_id(payload))
            expected_operation = {
                "provider_consent_request": "provider_consent",
                "campaign_create_request": "campaign_create",
                "proposal_correction_request": "proposal_correct",
                "proposal_rejection_request": "proposal_reject",
                "proposal_approval_request": "proposal_approve",
                "live_start_request": "live_start",
                "live_takeover_request": "live_takeover",
                "live_capture_request": "live_capture",
                "live_end_request": "live_end",
            }[expected]
            if operation.get("operation") != expected_operation:
                raise HTTPFailure(422, "unsafe_binding", "invalid_operation_shape", stage, self._request_id(payload))
        if expected == "provider_consent_request" and (not isinstance(payload["input"], dict) or set(payload["input"]) != {"explicit", "consent_identity_digest"}):
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        if expected == "campaign_create_request" and (not isinstance(payload["input"], dict) or set(payload["input"]) != {"campaign_id", "campaign_name", "adapter_id"}):
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        self._validate_request_values(payload, expected, stage)

    def _validate_request_values(self, payload: dict, expected: str, stage: str) -> None:
        """Enforce the closed wire-schema bounds before lookup, claim, or mutation."""
        request_id = self._request_id(payload)

        def public(value: object) -> bool:
            return isinstance(value, str) and 3 <= len(value) <= 80 and _PUBLIC_ID.fullmatch(value) is not None

        def domain(value: object) -> bool:
            return isinstance(value, str) and 3 <= len(value) <= 200 and _DOMAIN_ID.fullmatch(value) is not None

        def digest(value: object) -> bool:
            return isinstance(value, str) and _DIGEST.fullmatch(value) is not None

        operation = payload.get("operation_request")
        if operation is not None:
            valid_operation = (
                public(operation.get("request_id"))
                and public(operation.get("idempotency_key"))
                and digest(operation.get("payload_digest"))
                and (operation.get("expected_revision") is None or public(operation.get("expected_revision")))
                and (operation.get("expected_workflow_version") is None or (
                    type(operation.get("expected_workflow_version")) is int
                    and operation["expected_workflow_version"] >= 1
                ))
                and ("subject_id" not in operation or public(operation.get("subject_id")))
                and ("intent_digest" not in operation or digest(operation.get("intent_digest")))
            )
            if not valid_operation:
                raise HTTPFailure(422, "unsafe_binding", "invalid_request_value", stage, request_id)

        valid = True
        if expected == "provider_consent_request":
            valid = payload["input"].get("explicit") is True and digest(payload["input"].get("consent_identity_digest"))
        elif expected == "campaign_create_request":
            value = payload["input"]
            valid = (
                public(value.get("campaign_id"))
                and isinstance(value.get("campaign_name"), str)
                and 1 <= len(value["campaign_name"]) <= 120
                and value.get("adapter_id") == "mothership"
            )
        elif expected == "generation_start_request":
            context = payload.get("context")
            valid_context = isinstance(context, dict) and (
                set(context) == {"scope"} and context.get("scope") == "campaign"
                or set(context) == {"scope", "record_id", "content_digest"}
                and context.get("scope") == "record"
                and domain(context.get("record_id"))
                and digest(context.get("content_digest"))
            )
            valid = (
                public(payload.get("generation_id")) and public(payload.get("campaign_id"))
                and public(payload.get("source_revision"))
                and payload.get("action") in {"ask", "check", "generate"}
                and isinstance(payload.get("prompt"), str) and 1 <= len(payload["prompt"]) <= 4000
                and ("session_id" not in payload or public(payload.get("session_id")))
                and valid_context
            )
        elif expected == "proposal_create_request":
            valid = (
                all(public(payload.get(field)) for field in (
                    "request_id", "idempotency_key", "generation_id", "proposal_id",
                    "campaign_id", "source_revision", "base_revision",
                ))
                and all(digest(payload.get(field)) for field in (
                    "payload_digest", "source_set_digest", "terminal_draft_digest",
                ))
                and domain(payload.get("subject_id"))
            )
        elif expected == "proposal_correction_request":
            valid = (
                all(public(payload.get(field)) for field in (
                    "proposal_id", "source_revision", "base_revision", "change_id",
                ))
                and domain(payload.get("subject_id"))
                and type(payload.get("proposal_version")) is int and payload["proposal_version"] >= 1
                and isinstance(payload.get("after_content"), str)
                and 1 <= len(payload["after_content"]) <= 100_000
            )
        elif expected == "proposal_rejection_request":
            valid = (
                all(public(payload.get(field)) for field in ("proposal_id", "source_revision", "base_revision"))
                and type(payload.get("proposal_version")) is int and payload["proposal_version"] >= 1
            )
        elif expected == "proposal_approval_request":
            valid = (
                all(public(payload.get(field)) for field in (
                    "proposal_id", "source_revision", "base_revision", "expected_campaign_head",
                ))
                and all(digest(payload.get(field)) for field in ("diff_digest", "proposal_payload_digest"))
                and type(payload.get("proposal_version")) is int and payload["proposal_version"] >= 1
                and payload.get("warden_confirmed") is True
            )
        elif expected == "live_start_request":
            valid = (
                all(public(payload.get(field)) for field in (
                    "campaign_id", "session_id", "head_revision", "controller_id",
                ))
            )
        elif expected == "live_takeover_request":
            valid = (
                all(public(payload.get(field)) for field in ("campaign_id", "session_id", "controller_id"))
                and type(payload.get("controller_epoch")) is int and payload["controller_epoch"] >= 1
            )
        elif expected == "live_capture_request":
            record_id = payload.get("record_id")
            valid = (
                all(public(payload.get(field)) for field in (
                    "campaign_id", "session_id", "controller_id", "event_id", "device_id", "operation_id",
                ))
                and type(payload.get("controller_epoch")) is int and payload["controller_epoch"] >= 1
                and type(payload.get("device_order")) is int and payload["device_order"] >= 1
                and payload.get("capture_type") in {"confirmed_fact", "unresolved_question"}
                and isinstance(payload.get("text"), str)
                and 1 <= len(payload["text"]) <= 100_000
                and (record_id is None or (domain(record_id)))
            )
        elif expected == "live_end_request":
            required_operation_ids = payload.get("required_operation_ids")
            valid = (
                all(public(payload.get(field)) for field in (
                    "campaign_id", "session_id", "controller_id", "device_id", "operation_id",
                ))
                and type(payload.get("controller_epoch")) is int and payload["controller_epoch"] >= 1
                and isinstance(required_operation_ids, list)
                and all(
                    isinstance(item, dict)
                    and set(item) == {"device_id", "operation_id"}
                    and public(item["device_id"]) and public(item["operation_id"])
                    for item in required_operation_ids
                )
                and len({(item["device_id"], item["operation_id"]) for item in required_operation_ids}) == len(required_operation_ids)
            )
        if not valid:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_value", stage, request_id)
        if expected == "campaign_create_request" and payload["input"].get("adapter_id") != "mothership":
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        if expected == "provider_consent_request" and payload["input"].get("explicit") is not True:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        if expected == "proposal_approval_request" and payload.get("warden_confirmed") is not True:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))

    def _semantic(self, payload: dict, *, context=None, stage: str) -> None:
        try:
            validate_http_semantics(payload, context=context)
        except HTTPContractSemanticError as exc:
            status = 409 if exc.finding.category in {
                "idempotency_digest_conflict", "capability_rejected",
                "source_digest_conflict", "proposal_approval_conflict",
                "stale_revision", "stream_sequence_conflict",
                "stale_controller", "stale_workflow",
            } else 422
            raise HTTPFailure(
                status, exc.finding.category, exc.finding.code, stage,
                self._request_id(payload), False,
            ) from exc

    def _replay(self, operation: str, key: str, digest: str):
        try:
            return self.receipts.replay(operation, key, digest)
        except ReceiptConflict as exc:
            raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", operation) from exc

    def _store(self, operation: str, key: str, digest: str, status: int, response: dict) -> None:
        try:
            self.receipts.store(operation, key, digest, status, response)
        except ReceiptConflict as exc:
            raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", operation) from exc

    def _claim(self, operation: str, key: str, digest: str) -> bool:
        try:
            return self.receipts.claim(operation, key, digest)
        except ReceiptConflict as exc:
            raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", operation) from exc

    def _release(self, operation: str, key: str, digest: str) -> None:
        try:
            self.receipts.release(operation, key, digest)
        except ReceiptConflict as exc:
            raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", operation) from exc

    def _abandoned(self, operation: str, key: str, digest: str) -> bool:
        return (operation, key, digest) in self._abandoned_claims

    def provider_readiness(self) -> tuple[int, dict]:
        configured = self.provider.verify()
        available = configured
        identity = None
        current_identity = None
        if configured:
            try:
                current_identity = self.ai._current_consent_identity()
                identity = canonical_digest(asdict(current_identity))
            except ProviderUnavailable:
                available = False
        consent = self.ai_repository.consent()
        current = bool(consent and consent.current and available and consent == current_identity)
        payload = {
            "contract_name": "provider_readiness_response", "contract_version": 2,
            "provider_configured": configured, "provider_available": available,
            "consent_current": current, "consent_identity_digest": identity,
            "ai_available": bool(configured and available and current and identity),
        }
        self._semantic(payload, stage="provider_readiness")
        return 200, payload

    def provider_consent(self, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "provider_consent_request", "provider_consent")
        _, readiness = self.provider_readiness()
        identity = readiness["consent_identity_digest"]
        self._semantic(payload, context={"consent_identity_digest": identity}, stage="provider_consent")
        operation = payload["operation_request"]
        replay = self._replay("provider_consent", operation["idempotency_key"], operation["payload_digest"])
        if replay:
            _, response = replay
            return 200, response
        if identity is None:
            raise HTTPFailure(503, "provider_unavailable", "provider_unavailable", "provider_consent", operation["request_id"], True)
        if self._abandoned("provider_consent", operation["idempotency_key"], operation["payload_digest"]) and readiness["consent_current"]:
            self._store("provider_consent", operation["idempotency_key"], operation["payload_digest"], 200, readiness)
            return 200, readiness
        if not self._claim("provider_consent", operation["idempotency_key"], operation["payload_digest"]):
            if readiness["consent_current"]:
                self._store("provider_consent", operation["idempotency_key"], operation["payload_digest"], 200, readiness)
                return 200, readiness
            raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "provider_consent", operation["request_id"], True)
        try:
            self.ai.record_consent(explicit=True)
        except Exception:
            self._release("provider_consent", operation["idempotency_key"], operation["payload_digest"])
            raise
        _, response = self.provider_readiness()
        self._store("provider_consent", operation["idempotency_key"], operation["payload_digest"], 200, response)
        return 200, response

    def create_campaign(self, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "campaign_create_request", "campaign_create")
        self._semantic(payload, stage="campaign_create")
        operation, data = payload["operation_request"], payload["input"]
        replay = self._replay("campaign_create", operation["idempotency_key"], operation["payload_digest"])
        if replay:
            return (200 if replay[0] < 400 else replay[0]), replay[1]
        recovered = self._matching_creation(data, operation["idempotency_key"])
        if recovered is not None:
            self.atlas_rebuilder.rebuild(recovered)
            response = self.revision_view(data["campaign_id"], recovered.revision_id)[1]
            self._store("campaign_create", operation["idempotency_key"], operation["payload_digest"], 201, response)
            return 200, response
        if not self._claim("campaign_create", operation["idempotency_key"], operation["payload_digest"]):
            raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "campaign_create", operation["request_id"], True)
        handle: WorkspaceHandle | None = None
        published = False
        try:
            with self._lock:
                if data["campaign_id"] in self.campaigns:
                    raise HTTPFailure(409, "idempotency_digest_conflict", "campaign_id_conflict", "campaign_create", operation["request_id"])
                handle = self.registry.allocate()
                initialized = self.engine.initialize(InitializeRequest(
                    self._id("command", operation["request_id"], "initialize"), handle,
                    data["campaign_name"], data["adapter_id"],
                ))
                if initialized.status is not Status.STAGED:
                    raise HTTPFailure(422, "validation_finding", "campaign_initialization_failed", "campaign_create", operation["request_id"])
                indexed = self.engine.index(WorkspaceRequest(
                    self._id("command", operation["request_id"], "index"), handle,
                ))
                if indexed.status is not Status.STAGED:
                    raise HTTPFailure(422, "validation_finding", "campaign_index_failed", "campaign_create", operation["request_id"])
                context = self.engine.context(ContextRequest(
                    self._id("command", operation["request_id"], "context"), handle,
                ))
                if context.status is not Status.STAGED:
                    raise HTTPFailure(422, "validation_finding", "campaign_context_failed", "campaign_create", operation["request_id"])
                validated = self.engine.validate(WorkspaceRequest(
                    self._id("command", operation["request_id"], "validate"), handle,
                ))
                if validated.status is not Status.STAGED:
                    raise HTTPFailure(422, "validation_finding", "campaign_validation_failed", "campaign_create", operation["request_id"])
                source = self.registry._resolve(handle)
                _, tree_digest = canonicalize_tree(source)
                revision_id = self._id("revision", data["campaign_id"], tree_digest)
                intent = PublicationIntent(
                    self._id("intent", operation["request_id"]),
                    self._id("token", operation["idempotency_key"]),
                    PublicationKind.CREATION, data["campaign_id"], revision_id,
                    None, 1, tree_digest, canonical_digest(data),
                )
                manifest = self.revisions.publish(
                    source, intent, framework_version=__version__, adapter_version="1.0.0",
                    validation_contract_digest=self.validation_contract_digest,
                    before_finalize=self.atlas_rebuilder.rebuild_pending,
                    rollback=lambda item: self.atlas_repository.delete(
                        item.campaign_id, item.revision_id
                    ),
                )
                published = True
                self.campaigns[data["campaign_id"]] = CampaignState(
                    data["campaign_id"], data["campaign_name"], data["adapter_id"],
                    {revision_id: manifest}, {revision_id: handle},
                )
                response = self.revision_view(data["campaign_id"], revision_id)[1]
                self._store("campaign_create", operation["idempotency_key"], operation["payload_digest"], 201, response)
                return 201, response
        except HTTPFailure as exc:
            if handle is not None and not published:
                self.registry.discard(handle)
            if not published:
                self._store("campaign_create", operation["idempotency_key"], operation["payload_digest"], exc.status, exc.payload)
            raise
        except Exception as exc:
            if handle is not None and not published:
                self.registry.discard(handle)
            failure = HTTPFailure(503, "service_unavailable", "campaign_creation_failed", "campaign_create", operation["request_id"], True)
            if not published:
                self._release("campaign_create", operation["idempotency_key"], operation["payload_digest"])
            raise failure from exc

    def _matching_creation(self, data: dict, idempotency_key: str) -> SnapshotManifest | None:
        campaign = self.campaigns.get(data["campaign_id"])
        if campaign is None:
            return None
        expected = (
            None, 1, canonical_digest(data), self._id("token", idempotency_key),
        )
        candidates = [manifest for manifest in campaign.revisions.values() if (
            manifest.parent_revision, manifest.ordinal, manifest.change_digest,
            manifest.publication_intent_token,
        ) == expected and self.workflow.head(data["campaign_id"]) == manifest.revision_id]
        return candidates[0] if len(candidates) == 1 else None

    def _campaign_revision(self, campaign_id: str, revision_id: str):
        campaign = self.campaigns.get(campaign_id)
        if campaign is None or revision_id not in campaign.revisions:
            raise HTTPFailure(404, "not_found", "revision_not_found", "revision_read")
        return campaign, campaign.revisions[revision_id]

    def revision_view(self, campaign_id: str, revision_id: str) -> tuple[int, dict]:
        campaign, manifest = self._campaign_revision(campaign_id, revision_id)
        record = self._record(campaign_id, revision_id, "campaign-main")
        response = {
            "contract_name": "campaign_revision_view", "contract_version": 2,
            "campaign_id": campaign_id, "campaign_name": campaign.campaign_name,
            "adapter_id": campaign.adapter_id,
            "viewed_revision": self._revision_ref(manifest),
            "head_revision": self.workflow.head(campaign_id),
            "records": [{key: record[key] for key in ("record_id", "record_type", "name", "authority")}],
        }
        return 200, response

    @staticmethod
    def _revision_ref(manifest: SnapshotManifest) -> dict:
        return {"revision_id": manifest.revision_id, "ordinal": manifest.ordinal,
                "tree_digest": manifest.tree_digest, "validation_status": "passed"}

    def _workspace_for_revision(self, campaign_id: str, revision_id: str):
        campaign, _ = self._campaign_revision(campaign_id, revision_id)
        return campaign.workspaces[revision_id]

    def _record(self, campaign_id: str, revision_id: str, record_id: str) -> dict:
        handle = self._workspace_for_revision(campaign_id, revision_id)
        result = self.engine.retrieve(RetrievalRequest(
            self._id("command", campaign_id, revision_id, record_id), handle,
            RetrievalKind.SHOW, record_id,
        ))
        if result.result.status is not Status.STAGED or not result.records:
            raise HTTPFailure(404, "not_found", "record_not_found", "record_read")
        record = result.records[0]
        return {"contract_name": "record_view", "contract_version": 2,
                "campaign_id": campaign_id, "revision_id": revision_id,
                "record_id": record.subject_id, "record_type": record.record_type,
                "name": record.name, "authority": self._authority(record.status),
                "content": record.content}

    def record_view(self, campaign_id: str, revision_id: str, record_id: str) -> tuple[int, dict]:
        return 200, self._record(campaign_id, revision_id, record_id)

    # The editor uses a separate, additive contract.  Its records are parsed
    # from the verified revision and all writes still pass through the engine.
    def _editor_context(self, campaign_id: str, revision_id: str, record_id: str):
        campaign, manifest = self._campaign_revision(campaign_id, revision_id)
        record = self._record(campaign_id, revision_id, record_id)
        head_id = self.workflow.head(campaign_id)
        head = campaign.revisions[head_id] if head_id else manifest
        return campaign, manifest, head, parse_document(record["content"], record_id, record["record_type"])

    @staticmethod
    def _editor_revision_ref(manifest: SnapshotManifest) -> dict:
        return {"revision_id": manifest.revision_id, "ordinal": manifest.ordinal,
                "tree_digest": manifest.tree_digest}

    @staticmethod
    def _editor_immutable_revision_ref(manifest: SnapshotManifest) -> dict:
        return {"revision_id": manifest.revision_id, "ordinal": manifest.ordinal,
                "tree_digest": manifest.tree_digest, "immutable": True}

    def _editor_record_ids(self, campaign_id: str, revision_id: str) -> set[str]:
        return {record.record_id for record in self.atlas_repository.get(campaign_id, revision_id).records}

    def _editor_bound_removal_impact(
        self, campaign_id: str, revision_id: str, record_id: str, binding: dict,
    ) -> dict:
        """Read the verified impact while retaining its proposal-time binding.

        The graph is immutable at ``revision_id``; the workflow counter is not.
        Re-reading an impact for a proposal therefore must not silently replace
        the binding captured when that proposal was created.
        """
        _, impact = self.editor_removal_impact(campaign_id, revision_id, record_id)
        actual = impact["binding"]
        for key in ("campaign_id", "base_revision", "record_id", "record_digest"):
            if actual[key] != binding[key]:
                raise HTTPFailure(409, "unsafe_binding", "impact_binding_mismatch", "editor_proposal")
        bound = deepcopy(impact)
        bound["binding"] = deepcopy(binding)
        return bound

    def _editor_validate_candidate(self, candidate: dict, *, campaign_id: str,
                                   revision_id: str, expected_record_id: str | None) -> dict:
        try:
            document = _document(candidate)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPFailure(422, "proposal_validation_failure", str(exc), "editor_proposal") from exc
        if expected_record_id is not None and document["record_id"] != expected_record_id:
            raise HTTPFailure(422, "unsafe_binding", "record_id_mismatch", "editor_proposal")
        record_ids = self._editor_record_ids(campaign_id, revision_id)
        if document["record_id"] in record_ids and expected_record_id is None:
            raise HTTPFailure(409, "proposal_approval_conflict", "record_already_exists", "editor_proposal")
        for connection in document["connections"]:
            if connection["target_record_id"] not in record_ids:
                raise HTTPFailure(422, "proposal_validation_failure", "unknown_connection_target", "editor_proposal")
        return document

    def _editor_validate_changes(
        self, campaign_id: str, revision_id: str, changes: list[ExactTextChange],
        request_id: str,
    ) -> None:
        """Run the complete typed mutation through the deterministic engine.

        This is deliberately before a proposal is claimed or stamped as
        ``passed``.  Passing the complete change set also makes removal of a
        record and all affected typed connections one atomic validation unit.
        """
        base_handle = self._workspace_for_revision(campaign_id, revision_id)
        result = self.engine.stage_exact_diff(StageExactDiffRequest(
            self._id("command", request_id, "editor_validate"), base_handle,
            exact_diff_digest(tuple(changes)), tuple(changes),
        ))
        if result.staged_handle != base_handle:
            self.registry.discard(result.staged_handle)
        if result.status is Status.STAGED:
            return
        code = next((finding.code for finding in result.findings if finding.severity.value == "error"), None)
        if code is None and any(change.change_kind is ChangeKind.CREATE for change in changes):
            code = "record_type_unknown"
        raise HTTPFailure(
            422, "proposal_validation_failure", code or "proposal_validation_failure",
            "editor_proposal", request_id,
        )

    @staticmethod
    def _editor_property_changes(before: dict | None, after: dict | None) -> list[dict]:
        if before is None or after is None:
            return []
        changes = []
        for field in ("displayed_name", "status", "authority", "visibility"):
            if before[field] != after[field]:
                changes.append({"property": field, "before": before[field], "after": after[field]})
        for collection, identifier, value_key in (("fields", "field_id", "value"), ("sections", "section_id", "body")):
            old = {item[identifier]: item for item in before[collection]}
            new = {item[identifier]: item for item in after[collection]}
            for member_id in sorted(set(old) | set(new)):
                old_value = old.get(member_id, {}).get(value_key)
                new_value = new.get(member_id, {}).get(value_key)
                if old_value != new_value:
                    changes.append({"property": f"{collection}.{member_id}", "before": old_value, "after": new_value})
        return changes

    def _editor_connection_cards(self, change_id: str, before: dict | None, after: dict | None) -> list[dict]:
        old = {item["connection_id"]: item for item in (before or {}).get("connections", [])}
        new = {item["connection_id"]: item for item in (after or {}).get("connections", [])}
        cards = []
        for connection_id in sorted(set(old) | set(new)):
            previous, current = old.get(connection_id), new.get(connection_id)
            if previous == current:
                continue
            if previous is None:
                kind, connection = "connection_added", current
                effects = [{"source_record_id": after["record_id"], "target_record_id": current["target_record_id"], "connection_id": connection_id, "effect": "added"}]
            elif current is None:
                kind, connection = "connection_removed", previous
                effects = [{"source_record_id": before["record_id"], "target_record_id": previous["target_record_id"], "connection_id": connection_id, "effect": "removed"}]
            else:
                kind, connection = "connection_updated", {"before": previous, "after": current}
                effects = ([{"source_record_id": before["record_id"], "target_record_id": previous["target_record_id"], "connection_id": connection_id, "effect": "removed"}, {"source_record_id": after["record_id"], "target_record_id": current["target_record_id"], "connection_id": connection_id, "effect": "added"}] if previous["target_record_id"] != current["target_record_id"] else [{"source_record_id": after["record_id"], "target_record_id": current["target_record_id"], "connection_id": connection_id, "effect": "updated"}])
            cards.append({"change_id": self._id("change", change_id, connection_id, kind), "kind": kind,
                          "subject_record_id": (after or before)["record_id"], "before": None, "after": None,
                          "property_changes": [], "connection": connection, "resolution": None,
                          "derived_backlinks": effects})
        return cards

    @staticmethod
    def _editor_transition_changes(before: dict | None, after: dict | None, change_id: str) -> tuple[list[dict], list[dict]]:
        if before is None or after is None:
            return [], []
        authority = []
        visibility = []
        if before["authority"] != after["authority"]:
            authority.append({"change_id": change_id, "record_id": after["record_id"], "from": before["authority"], "to": after["authority"], "explicit_in_diff": True, "warden_approval_required": True})
        if before["visibility"] != after["visibility"]:
            visibility.append({"change_id": change_id, "record_id": after["record_id"], "before": before["visibility"], "after": after["visibility"], "audience_broadens": before["visibility"]["audience"] == "warden" and after["visibility"]["audience"] != "warden", "explicit_in_diff": True, "warden_approval_required": True})
        return authority, visibility

    def _editor_version(self, campaign_id: str) -> int:
        reader = getattr(self.proposal_repository, "editor_workflow_version", None)
        if reader is not None:
            return reader(campaign_id)
        return self._editor_workflow.get(campaign_id, 1)

    def _editor_semantic(self, payload: dict, *, stage: str, proposal: dict | None = None,
                         current_head: dict | None = None, current_workflow_version: int | None = None,
                         impact: dict | None = None, existing_record_ids: set[str] | None = None) -> None:
        try:
            validate_editor_semantics(
                payload, proposal=proposal, current_head=current_head,
                current_workflow_version=current_workflow_version, impact=impact,
                existing_record_ids=existing_record_ids,
            )
        except EditorSemanticError as exc:
            status = 409 if exc.category in {
                "idempotency_digest_conflict", "proposal_approval_conflict",
                "stale_revision", "workflow_conflict", "stale_record_digest",
            } else 422
            code = {
                "stale_revision": "stale_revision",
                "stale_record_digest": "stale_record_digest",
                "workflow_conflict": "workflow_conflict",
                "invalid_connections": "invalid_connections",
                "incomplete_removal_resolution": "incomplete_removal_resolution",
            }.get(exc.category, "editor_semantic_invalid")
            raise HTTPFailure(status, {
                "workflow_conflict": "unsafe_binding",
                "stale_record_digest": "stale_revision",
            }.get(exc.category, exc.category), code, stage, self._request_id(payload)) from exc

    def editor_record_read(self, campaign_id: str, revision_id: str, record_id: str) -> tuple[int, dict]:
        campaign, manifest, head, document = self._editor_context(campaign_id, revision_id, record_id)
        response = {"contract_name": "editor_record_view", "contract_version": 1,
                     "campaign_id": campaign_id, "viewed_revision": self._editor_revision_ref(manifest),
                     "head_revision": self._editor_revision_ref(head),
                     "editor_workflow_version": self._editor_version(campaign_id),
                     "historical": manifest.revision_id != head.revision_id,
                     "editable": manifest.revision_id == head.revision_id,
                     "record": document}
        self._editor_semantic(response, stage="editor_record_read")
        return 200, response

    @staticmethod
    def _editor_payload_digest(payload: dict) -> str:
        operation = payload.get("operation_request", {})
        ignored = {"contract_name", "contract_version", "operation_request", "request_id", "idempotency_key", "payload_digest"}
        return canonical_digest({key: value for key, value in payload.items() if key not in ignored})

    def _editor_proposal(self, campaign_id: str, revision_id: str, payload: dict, kind: str, record_id: str | None = None, *, operation_name: str | None = None, proposal_id_override: str | None = None, correction_of: dict | None = None) -> tuple[int, dict]:
        operation = payload.get("operation_request")
        if not isinstance(operation, dict):
            raise HTTPFailure(422, "unsafe_binding", "invalid_operation_shape", "editor_proposal", self._request_id(payload))
        operation_name = operation_name or ("editor_record_" + kind)
        expected_fields = {
            "create": {"contract_name", "contract_version", "operation_request", "binding", "candidate"},
            "edit": {"contract_name", "contract_version", "operation_request", "binding", "candidate"},
            "remove": {"contract_name", "contract_version", "operation_request", "binding", "impact_digest", "impact_binding", "resolutions"},
        }[kind]
        if operation_name == "editor_proposal_correct":
            expected_fields = {"contract_name", "contract_version", "operation_request", "prior_proposal", "binding", "mutation_kind", "candidate", "resolutions", "impact_digest", "impact_binding"}
        if set(payload) != expected_fields:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", "editor_proposal", self._request_id(payload))
        operation_fields = {"contract_name", "contract_version", "request_id", "operation", "idempotency_key", "payload_digest", "expected_revision", "expected_editor_workflow_version", "subject_id"}
        if set(operation) != operation_fields or operation.get("contract_name") != "editor_operation_request" or operation.get("contract_version") != 1:
            raise HTTPFailure(422, "unsafe_binding", "invalid_operation_shape", "editor_proposal", self._request_id(payload))
        expected_subject = proposal_id_override if operation_name == "editor_proposal_correct" else record_id
        if operation.get("subject_id") != expected_subject or operation.get("expected_revision") != revision_id:
            raise HTTPFailure(422, "unsafe_binding", "invalid_operation_binding", "editor_proposal", self._request_id(payload))
        replay = self._replay(operation_name, operation.get("idempotency_key"), operation.get("payload_digest"))
        if replay:
            return 200, replay[1]
        if operation.get("operation") != operation_name or operation.get("payload_digest") != self._editor_payload_digest(payload):
            raise HTTPFailure(422, "idempotency_digest_conflict", "payload_digest_mismatch", "editor_proposal", self._request_id(payload))
        binding = payload.get("binding")
        if not isinstance(binding, dict) or binding.get("campaign_id") != campaign_id or binding.get("record_id") != record_id:
            raise HTTPFailure(422, "unsafe_binding", "invalid_editor_binding", "editor_proposal", self._request_id(payload))
        expected = payload.get("expected_editor_workflow_version", operation.get("expected_editor_workflow_version"))
        current = self._editor_version(campaign_id)
        if expected != current or binding.get("expected_editor_workflow_version") != current:
            raise HTTPFailure(409, "unsafe_binding", "workflow_conflict", "editor_workflow", self._request_id(payload))
        base = binding.get("base_revision", {}).get("revision_id")
        head_id = self.workflow.head(campaign_id)
        if base != revision_id or head_id != revision_id:
            raise HTTPFailure(409, "stale_revision", "stale_revision", "editor_workflow", self._request_id(payload))
        before = None
        before_doc = None
        if record_id is not None and kind != "create":
            before = self._record(campaign_id, revision_id, record_id)["content"]
            before_doc = parse_document(before, record_id)
            if binding.get("record_digest") != before_doc["content_digest"]:
                raise HTTPFailure(409, "stale_revision", "stale_record_digest", "editor_proposal", self._request_id(payload))
        elif kind == "create" and binding.get("record_digest") is not None:
            raise HTTPFailure(422, "unsafe_binding", "invalid_editor_binding", "editor_proposal", self._request_id(payload))
        candidate = before_doc if kind == "remove" else payload.get("candidate")
        if not isinstance(candidate, dict):
            raise HTTPFailure(422, "proposal_validation_failure", "invalid_candidate", "editor_proposal", self._request_id(payload))
        candidate = self._editor_validate_candidate(candidate, campaign_id=campaign_id, revision_id=revision_id, expected_record_id=record_id)
        if kind == "create" and record_id in self._editor_record_ids(campaign_id, revision_id):
            raise HTTPFailure(409, "proposal_approval_conflict", "record_already_exists", "editor_proposal", self._request_id(payload))

        change_id = self._id("change", campaign_id, revision_id, record_id or candidate["record_id"], kind)
        change = change_for(before, candidate, change_id, ChangeKind.DELETE if kind == "remove" else (ChangeKind.CREATE if before is None else ChangeKind.UPDATE))
        changes = [change]
        removal_impact = None
        resolutions = payload.get("resolutions", [])
        if kind == "remove":
            _, removal_impact = self.editor_removal_impact(campaign_id, revision_id, record_id or "")
            if payload.get("impact_digest") != removal_impact["impact_digest"] or payload.get("impact_binding") != {"binding": removal_impact["binding"], "impact_digest": removal_impact["impact_digest"]}:
                raise HTTPFailure(422, "unsafe_binding", "impact_binding_mismatch", "editor_proposal", self._request_id(payload))
            expected_refs = {item["reference_id"] for item in removal_impact["incoming_references"]}
            if {item.get("reference_id") for item in resolutions} != expected_refs or len(resolutions) != len(expected_refs):
                raise HTTPFailure(422, "proposal_validation_failure", "incomplete_removal_resolution", "editor_proposal", self._request_id(payload))
            documents = {record.record_id: (parse_document(record.content, record.record_id, record.record_type), record.content) for record in self.atlas_repository.get(campaign_id, revision_id).records}
            for reference in removal_impact["incoming_references"]:
                resolution = next(item for item in resolutions if item.get("reference_id") == reference["reference_id"])
                if resolution.get("action") == "accept_unresolved" and not reference["permitted_unresolved"]:
                    raise HTTPFailure(422, "proposal_validation_failure", "resolution_not_permitted", "editor_proposal", self._request_id(payload))
                if resolution.get("action") == "redirect" and resolution.get("replacement_target_record_id") not in self._editor_record_ids(campaign_id, revision_id) - {record_id}:
                    raise HTTPFailure(422, "proposal_validation_failure", "unknown_connection_target", "editor_proposal", self._request_id(payload))
                source, source_content = deepcopy(documents[reference["source_record_id"]][0]), documents[reference["source_record_id"]][1]
                connection = next(item for item in source["connections"] if item["connection_id"] == reference["connection_id"])
                if resolution["action"] == "redirect":
                    connection["target_record_id"] = resolution["replacement_target_record_id"]
                else:
                    source["connections"] = [item for item in source["connections"] if item["connection_id"] != reference["connection_id"]]
                source["content_digest"] = document_digest(source)
                changes.append(change_for(source_content, source, self._id("change", campaign_id, revision_id, reference["source_record_id"], reference["connection_id"]), ChangeKind.UPDATE))

        self._editor_validate_changes(
            campaign_id, revision_id, changes, self._request_id(payload),
        )

        head_manifest = self.campaigns[campaign_id].revisions[head_id]
        self._editor_semantic(
            payload, stage="editor_proposal", current_head=self._editor_revision_ref(head_manifest),
            current_workflow_version=current, impact=removal_impact,
            existing_record_ids=self._editor_record_ids(campaign_id, revision_id),
        )

        proposal_id = proposal_id_override or self._id("proposal", campaign_id, revision_id, editor_diff_digest(tuple(changes)))
        if not self._claim(operation_name, operation["idempotency_key"], operation["payload_digest"]):
            replay = self._replay(operation_name, operation["idempotency_key"], operation["payload_digest"])
            if replay:
                return 200, replay[1]
            raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "editor_proposal", self._request_id(payload), True)
        version = self.proposal_repository.next_version(proposal_id)
        after_doc = None if kind == "remove" else candidate
        cards = [{"change_id": change.change_id, "kind": "record_removed" if kind == "remove" else ("record_created" if kind == "create" else "record_updated"), "subject_record_id": change.subject_id, "before": before_doc, "after": after_doc, "property_changes": self._editor_property_changes(before_doc, after_doc), "connection": None, "resolution": None, "derived_backlinks": []}]
        if kind != "remove":
            cards.extend(self._editor_connection_cards(change.change_id, before_doc, after_doc))
        if kind == "remove":
            for connection in removal_impact["outgoing_connections"]:
                cards.append({"change_id": self._id("change", change.change_id, connection["connection_id"]), "kind": "connection_removed", "subject_record_id": record_id, "before": None, "after": None, "property_changes": [], "connection": connection, "resolution": None, "derived_backlinks": [{"source_record_id": record_id, "target_record_id": connection["target_record_id"], "connection_id": connection["connection_id"], "effect": "removed"}]})
            for reference, source_change in zip(removal_impact["incoming_references"], changes[1:]):
                resolution = next(item for item in resolutions if item["reference_id"] == reference["reference_id"])
                cards.append({"change_id": source_change.change_id, "kind": "reference_resolution", "subject_record_id": reference["source_record_id"], "before": reference, "after": resolution, "property_changes": [], "connection": None, "resolution": resolution, "derived_backlinks": [{"source_record_id": reference["source_record_id"], "target_record_id": resolution.get("replacement_target_record_id") or reference["target_record_id"], "connection_id": reference["connection_id"], "effect": "updated" if resolution["action"] == "redirect" else "removed"}]})
        authority_changes, visibility_changes = self._editor_transition_changes(before_doc, after_doc, change.change_id)
        validation_digest = canonical_digest({"status": "passed", "error_count": 0, "findings": []})
        affected_record_count = len({card["subject_record_id"] for card in cards})
        diff_projection = {"cards": cards, "affected_record_count": affected_record_count,
                           "authority_changes": authority_changes, "visibility_changes": visibility_changes,
                           "unresolved_reference_count": sum(item.get("action") == "accept_unresolved" for item in resolutions),
                           "impact_digest": removal_impact["impact_digest"] if removal_impact else None}
        digest = canonical_digest(diff_projection)
        core_changes = []
        for card in cards:
            document = card.get("after") if isinstance(card.get("after"), dict) and "content_digest" in card["after"] else card.get("before")
            core_changes.append({"change_id": card["change_id"], "subject_id": card["subject_record_id"], "change_type": "add" if card["kind"] == "record_created" else ("remove" if card["kind"] == "record_removed" else "update"), "from_authority": document.get("authority", "absent") if isinstance(document, dict) else "preparation", "to_authority": (card["after"].get("authority") if isinstance(card.get("after"), dict) and "authority" in card["after"] else ("absent" if card["kind"] == "record_removed" else document.get("authority", "preparation") if isinstance(document, dict) else "preparation")), "content_digest": document.get("content_digest", text_digest(json.dumps(card.get("connection"), sort_keys=True))) if isinstance(document, dict) else text_digest(json.dumps(card.get("connection"), sort_keys=True))})
        proposal_binding = dict(binding, expected_editor_workflow_version=current + 1)
        value = {"contract_name": "editor_proposal_view", "contract_version": 1, "proposal_id": proposal_id, "proposal_version": version, "campaign_id": campaign_id, "source_revision": self._editor_revision_ref(self.campaigns[campaign_id].revisions[revision_id]), "base_revision": self._editor_revision_ref(self.campaigns[campaign_id].revisions[revision_id]), "expected_campaign_head": self._editor_revision_ref(self.campaigns[campaign_id].revisions[revision_id]), "editor_workflow_version": current + 1, "proposal_payload_digest": "0" * 64, "mutation_kind": kind, "record_bindings": [proposal_binding], "core_proposal": {"contract_name": "canon_proposal", "contract_version": 2, "draft": {"draft_id": proposal_id, "authority": "draft", "source_set_digest": text_digest(change.replacement), "content_digest": text_digest(change.replacement)}, "proposal": {"proposal_id": proposal_id, "proposal_version": version, "status": "needs_review", "campaign_id": campaign_id, "base_revision": revision_id, "source_revision": revision_id, "expected_campaign_head": revision_id, "expected_editor_workflow_version": current + 1, "diff_digest": digest, "authority_change_ids": [item["change_id"] for item in authority_changes], "visibility_change_ids": [item["change_id"] for item in visibility_changes], "changes": core_changes}, "validation": {"status": "passed", "validation_digest": validation_digest, "error_count": 0}, "approval_binding": None}, "diff": {"diff_digest": digest, "cards": cards, "affected_record_count": affected_record_count, "authority_changes": authority_changes, "visibility_changes": visibility_changes, "unresolved_reference_count": diff_projection["unresolved_reference_count"], "impact_digest": removal_impact["impact_digest"] if removal_impact else None, "summary": kind}, "impact_digest": removal_impact["impact_digest"] if removal_impact else None, "impact_binding": {"binding": binding, "impact_digest": removal_impact["impact_digest"]} if removal_impact else None, "resolutions": resolutions if kind == "remove" else [], "validation": {"status": "passed", "validation_digest": validation_digest, "error_count": 0, "findings": []}, "authority_outcome": authority_changes, "visibility_outcome": visibility_changes, "publication": {"status": "not_published", "published_revision": None}}
        if correction_of is not None:
            value["correction_of"] = correction_of
        if kind == "remove":
            value["record_bindings"] = [proposal_binding] + [dict(proposal_binding, record_id=ref["source_record_id"], record_digest=documents[ref["source_record_id"]][0]["content_digest"]) for ref in removal_impact["incoming_references"]]
        value["proposal_payload_digest"] = canonical_digest({key: item for key, item in value.items() if key != "proposal_payload_digest"})
        self._editor_semantic(
            value, stage="editor_proposal", impact=removal_impact,
            existing_record_ids=self._editor_record_ids(campaign_id, revision_id),
        )
        item = ProposalVersion(proposal_id, version, campaign_id, revision_id, tuple(changes), digest, value["proposal_payload_digest"], editor_metadata=value)
        add_editor = getattr(self.proposal_repository, "add_editor", None)
        if add_editor is not None:
            if not add_editor(item, campaign_id, current):
                raise HTTPFailure(409, "unsafe_binding", "workflow_conflict", "editor_workflow", self._request_id(payload))
        else:
            self.proposal_repository.add(item)
            self._editor_workflow[campaign_id] = current + 1
        self._editor_proposals[(proposal_id, version)] = {"value": value, "change": change, "campaign_id": campaign_id, "base": revision_id, "workflow": current + 1}
        self._persist_editor_state()
        self._store(operation_name, operation["idempotency_key"], operation["payload_digest"], 201, value)
        return 201, value

    def editor_record_create(self, campaign_id: str, revision_id: str, payload: dict) -> tuple[int, dict]:
        candidate = payload.get("candidate")
        record_id = candidate.get("record_id") if isinstance(candidate, dict) else None
        return self._editor_proposal(campaign_id, revision_id, payload, "create", record_id)

    def editor_record_edit(self, campaign_id: str, revision_id: str, record_id: str, payload: dict) -> tuple[int, dict]:
        return self._editor_proposal(campaign_id, revision_id, payload, "edit", record_id)

    def editor_record_remove(self, campaign_id: str, revision_id: str, record_id: str, payload: dict) -> tuple[int, dict]:
        return self._editor_proposal(campaign_id, revision_id, payload, "remove", record_id)

    def editor_proposal_correct(self, proposal_id: str, version: int, payload: dict) -> tuple[int, dict]:
        """Create an immutable corrected editor version from a prior version."""
        prior = self._editor_proposals.get((proposal_id, version))
        if prior is None:
            raise HTTPFailure(404, "not_found", "proposal_not_found", "editor_correct")
        if payload.get("prior_proposal") != {"proposal_id": proposal_id, "proposal_version": version}:
            raise HTTPFailure(422, "unsafe_binding", "invalid_editor_binding", "editor_correct", self._request_id(payload))
        binding = payload.get("binding")
        if not isinstance(binding, dict):
            raise HTTPFailure(422, "unsafe_binding", "invalid_editor_binding", "editor_correct", self._request_id(payload))
        current_head_id = self.workflow.head(prior["campaign_id"])
        current_head = self.campaigns[prior["campaign_id"]].revisions[current_head_id]
        current_workflow = self._editor_version(prior["campaign_id"])
        if (
            binding.get("base_revision") != self._editor_revision_ref(current_head)
            or binding.get("expected_editor_workflow_version") != current_workflow
            or binding.get("campaign_id") != prior["campaign_id"]
            or binding.get("record_id") != prior["value"]["record_bindings"][0]["record_id"]
        ):
            raise HTTPFailure(409, "stale_revision", "stale_revision", "editor_correct", self._request_id(payload))
        candidate = payload.get("candidate")
        kind = payload.get("mutation_kind", prior["value"]["mutation_kind"])
        if kind == "remove":
            candidate = None
        if kind != "remove" and not isinstance(candidate, dict):
            raise HTTPFailure(422, "proposal_validation_failure", "invalid_correction", "editor_correct", self._request_id(payload))
        request = deepcopy(payload)
        status, value = self._editor_proposal(
            prior["campaign_id"], current_head_id, request, kind,
            prior["change"].subject_id, operation_name="editor_proposal_correct",
            proposal_id_override=proposal_id,
            correction_of={"proposal_id": proposal_id, "proposal_version": version},
        )
        return status, value

    def editor_removal_impact(self, campaign_id: str, revision_id: str, record_id: str) -> tuple[int, dict]:
        campaign, manifest = self._campaign_revision(campaign_id, revision_id)
        removed = parse_document(self._record(campaign_id, revision_id, record_id)["content"], record_id)
        bundle = self.atlas_repository.get(campaign_id, revision_id)
        incoming = []
        for record in bundle.records:
            if record.record_id == record_id:
                continue
            document = parse_document(record.content, record.record_id, record.record_type)
            for connection in document["connections"]:
                if connection["target_record_id"] == record_id:
                    incoming.append({"reference_id": f"reference_{record.record_id}_{connection['connection_id']}",
                                     "connection_id": connection["connection_id"], "source_record_id": record.record_id,
                                     "target_record_id": record_id, "relationship": connection["relationship"],
                                     "state": connection["state"], "context": connection["context"],
                                     "resolution_required": True, "permitted_unresolved": False})
        impact = {"campaign_id": campaign_id, "base_revision": self._editor_revision_ref(manifest),
                  "record_id": record_id, "record_digest": removed["content_digest"],
                  "expected_editor_workflow_version": self._editor_version(campaign_id),
                  "record": removed, "outgoing_connections": removed["connections"],
                  "incoming_references": incoming, "backlink_policy": "server_derived_from_typed_connections"}
        impact["impact_digest"] = canonical_digest({key: impact[key] for key in ("record", "outgoing_connections", "incoming_references")})
        response = {"contract_name": "editor_removal_impact", "contract_version": 1,
                     "binding": {key: impact[key] for key in ("campaign_id", "base_revision", "record_id", "record_digest", "expected_editor_workflow_version")},
                     "impact_digest": impact["impact_digest"], "record": removed,
                     "outgoing_connections": removed["connections"], "incoming_references": incoming,
                     "backlink_policy": "server_derived_from_typed_connections"}
        self._editor_semantic(response, stage="editor_removal_impact")
        return 200, response

    def editor_proposal_read(self, proposal_id: str, version: int) -> tuple[int, dict]:
        stored = self._editor_proposals.get((proposal_id, version))
        if stored is None:
            raise HTTPFailure(404, "not_found", "proposal_not_found", "editor_proposal_read")
        value = stored["value"]
        impact = None
        if value.get("mutation_kind") == "remove":
            impact = self._editor_bound_removal_impact(
                stored["campaign_id"], stored["base"], stored["change"].subject_id,
                value["impact_binding"]["binding"],
            )
        self._editor_semantic(value, stage="editor_proposal_read", impact=impact,
                              existing_record_ids=self._editor_record_ids(stored["campaign_id"], stored["base"]))
        return 200, value

    def _validate_editor_action(self, proposal_id: str, version: int, payload: dict, stored: dict, *, approve: bool) -> None:
        """Compare the complete wire binding before touching proposal state."""
        required = {
            "proposal", "proposal_status", "mutation_kind", "source_revision",
            "base_revision", "expected_campaign_head", "expected_editor_workflow_version",
            "proposal_payload_digest", "diff_digest", "validation_status",
            "validation_digest", "record_bindings", "impact_digest", "impact_binding",
            "resolutions", "authority_outcome", "visibility_outcome", "warden_confirmed",
        }
        if approve:
            required |= {"diff", "affected_record_count", "confirmed_change_ids",
                         "confirmed_authority_change_ids", "confirmed_visibility_change_ids"}
        else:
            required.add("reason_code")
        if set(payload) - (required | {"contract_name", "contract_version", "operation_request"}) or not required.issubset(payload):
            raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        operation = payload.get("operation_request")
        expected_contract = "editor_proposal_approval_request" if approve else "editor_proposal_rejection_request"
        expected_operation = "editor_proposal_approve" if approve else "editor_proposal_reject"
        if payload.get("contract_name") != expected_contract or payload.get("contract_version") != 1:
            raise HTTPFailure(422, "unsafe_binding", "invalid_contract", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        operation_fields = {"contract_name", "contract_version", "request_id", "operation", "idempotency_key", "payload_digest", "expected_revision", "expected_editor_workflow_version", "subject_id", "intent_digest"}
        if not isinstance(operation, dict) or set(operation) != operation_fields or operation.get("contract_name") != "editor_operation_request" or operation.get("contract_version") != 1 or operation.get("subject_id") != proposal_id or operation.get("operation") != expected_operation:
            raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        if operation.get("payload_digest") != self._editor_payload_digest(payload):
            raise HTTPFailure(422, "idempotency_digest_conflict", "payload_digest_mismatch", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        value = stored["value"]
        expected_values = {
            "proposal_status": value["core_proposal"]["proposal"]["status"],
            "diff_digest": value["diff"]["diff_digest"],
            "validation_status": value["validation"]["status"],
            "validation_digest": value["validation"]["validation_digest"],
            "expected_editor_workflow_version": value["editor_workflow_version"],
        }
        for key in ("proposal_status", "mutation_kind", "source_revision", "base_revision",
                    "expected_campaign_head", "expected_editor_workflow_version",
                    "proposal_payload_digest", "diff_digest", "validation_status",
                    "validation_digest", "record_bindings", "impact_digest",
                    "impact_binding", "resolutions", "authority_outcome", "visibility_outcome"):
            if payload[key] != expected_values.get(key, value.get(key)):
                raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        if payload["proposal"] != {"proposal_id": proposal_id, "proposal_version": version}:
            raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        if approve:
            if payload["diff"] != value["diff"] or payload["proposal_status"] != "needs_review" or payload["validation_status"] != "passed" or payload["warden_confirmed"] is not True:
                raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve", self._request_id(payload))
            expected_ids = [card["change_id"] for card in value["diff"]["cards"]]
            if (payload["confirmed_change_ids"] != expected_ids
                    or payload["confirmed_authority_change_ids"] != [item["change_id"] for item in value["diff"]["authority_changes"]]
                    or payload["confirmed_visibility_change_ids"] != [item["change_id"] for item in value["diff"]["visibility_changes"]]
                    or payload["affected_record_count"] != value["diff"]["affected_record_count"]):
                raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve", self._request_id(payload))
        elif payload["warden_confirmed"] is not True or not re.fullmatch(r"^[a-z][a-z0-9_]+$", payload["reason_code"]):
            raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_reject", self._request_id(payload))
        expected_revision = payload["base_revision"]["revision_id"]
        campaign = self.campaigns[stored["campaign_id"]]
        head_id = self.workflow.head(stored["campaign_id"])
        head = campaign.revisions[head_id] if head_id else None
        if head is None or payload["expected_campaign_head"] != self._editor_revision_ref(head):
            raise HTTPFailure(409, "stale_revision", "stale_revision", "editor_approve" if approve else "editor_reject", self._request_id(payload))
        if operation.get("expected_revision") != expected_revision or operation.get("expected_editor_workflow_version") != payload["expected_editor_workflow_version"] or operation.get("intent_digest") != payload["diff_digest"]:
            raise HTTPFailure(422, "proposal_approval_conflict", "approval_binding_mismatch", "editor_approve" if approve else "editor_reject", self._request_id(payload))

    def _editor_action(self, proposal_id: str, version: int, payload: dict, action: str):
        with self._editor_mutation_lock:
            operation = payload.get("operation_request")
            receipt_operation = "editor_proposal_" + action
            if isinstance(operation, dict):
                replay = self._replay(receipt_operation, operation.get("idempotency_key"), operation.get("payload_digest"))
                if replay:
                    return 200, replay[1]
            stored = self._editor_proposals.get((proposal_id, version))
            if stored is None:
                raise HTTPFailure(404, "not_found", "proposal_not_found", "editor_" + action)
            expected = (payload.get("operation_request") or {}).get("expected_editor_workflow_version")
            if expected != self._editor_version(stored["campaign_id"]) or expected != stored["workflow"]:
                raise HTTPFailure(409, "unsafe_binding", "workflow_conflict", "editor_" + action, self._request_id(payload))
            item = self.proposal_repository.get(proposal_id, version)
            if item is None:
                raise HTTPFailure(404, "not_found", "proposal_not_found", "editor_" + action)
            impact = None
            if stored["value"].get("mutation_kind") == "remove":
                impact = self._editor_bound_removal_impact(
                    stored["campaign_id"], stored["base"], stored["change"].subject_id,
                    stored["value"]["impact_binding"]["binding"],
                )
            self._editor_semantic(
                payload, stage="editor_" + action, proposal=stored["value"],
                current_head=self._editor_revision_ref(self.campaigns[stored["campaign_id"]].revisions[self.workflow.head(stored["campaign_id"])]),
                current_workflow_version=expected, impact=impact,
                existing_record_ids=self._editor_record_ids(stored["campaign_id"], stored["base"]),
            )
            self._validate_editor_action(proposal_id, version, payload, stored, approve=action == "approve")
            if not self._claim(receipt_operation, operation["idempotency_key"], operation["payload_digest"]):
                replay = self._replay(receipt_operation, operation["idempotency_key"], operation["payload_digest"])
                if replay:
                    return 200, replay[1]
                raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "editor_" + action, self._request_id(payload), True)
            value = stored["value"]
            atomic_metadata = None

            def terminal_value(published_revision=None):
                terminal = deepcopy(value)
                terminal["core_proposal"]["proposal"]["status"] = "approved" if action == "approve" else "rejected"
                terminal["publication"] = {
                    "status": "published" if published_revision is not None else "not_published",
                    "published_revision": published_revision,
                }
                if action == "approve":
                    terminal["core_proposal"]["approval_binding"] = {
                        "proposal_id": proposal_id, "proposal_version": version,
                        "diff_digest": terminal["diff"]["diff_digest"],
                        "base_revision": terminal["base_revision"]["revision_id"],
                        "source_revision": terminal["source_revision"]["revision_id"],
                        "expected_campaign_head": terminal["expected_campaign_head"]["revision_id"],
                        "expected_editor_workflow_version": expected,
                        "validation_status": terminal["validation"]["status"],
                        "validation_digest": terminal["validation"]["validation_digest"],
                        "authority_change_ids": [item["change_id"] for item in terminal["diff"]["authority_changes"]],
                        "visibility_change_ids": [item["change_id"] for item in terminal["diff"]["visibility_changes"]],
                        "warden_confirmed": True,
                    }
                terminal["editor_workflow_version"] = expected + 1
                terminal["core_proposal"]["proposal"]["expected_editor_workflow_version"] = expected + 1
                for record_binding in terminal["record_bindings"]:
                    record_binding["expected_editor_workflow_version"] = expected + 1
                if terminal["core_proposal"].get("approval_binding") is not None:
                    terminal["core_proposal"]["approval_binding"]["expected_editor_workflow_version"] = expected + 1
                terminal["proposal_payload_digest"] = canonical_digest({
                    key: item for key, item in terminal.items() if key != "proposal_payload_digest"
                })
                self._editor_semantic(
                    terminal, stage="editor_" + action, impact=impact,
                    existing_record_ids=self._editor_record_ids(stored["campaign_id"], stored["base"]),
                )
                return terminal

            if action == "reject":
                atomic_reject = getattr(self.workflow, "finalize_editor_rejection", None)
                if atomic_reject is not None:
                    atomic_metadata = terminal_value()
                    if not atomic_reject(
                        stored["campaign_id"], proposal_id, version, expected, atomic_metadata
                    ):
                        raise HTTPFailure(409, "unsafe_binding", "workflow_conflict", "editor_reject", self._request_id(payload))
                    result = self.proposal_repository.get(proposal_id, version)
                else:
                    result = self.proposals.reject(item)
                    if result is None:
                        raise HTTPFailure(409, "proposal_approval_conflict", "proposal_state_conflict", "editor_reject", self._request_id(payload))
            else:
                atomic_publish = getattr(self.workflow, "finalize_editor_publication", None)

                def finalize_publication(intent):
                    nonlocal atomic_metadata
                    published = {
                        "revision_id": intent.revision_id, "ordinal": intent.ordinal,
                        "tree_digest": intent.tree_digest, "immutable": True,
                    }
                    atomic_metadata = terminal_value(published)
                    return atomic_publish(
                        intent, proposal_id, version, expected, atomic_metadata
                    )

                result = self.proposals.approve(
                    item, diff_digest=value["diff"]["diff_digest"], base_revision=stored["base"],
                    payload_digest=value["proposal_payload_digest"],
                    finalize=finalize_publication if atomic_publish is not None else None,
                )
                if result.status is ProposalStatus.CONFLICT:
                    raise HTTPFailure(409, "stale_revision", "stale_revision", "editor_approve", self._request_id(payload))
                if result.status is not ProposalStatus.PUBLISHED:
                    raise HTTPFailure(422, "proposal_validation_failure", "proposal_validation_failure", "editor_approve", self._request_id(payload))
                manifest = self._matching_publication(result)
                published_revision = self._editor_immutable_revision_ref(manifest)
            if atomic_metadata is None:
                advance_editor = getattr(self.proposal_repository, "advance_editor", None)
                if advance_editor is not None:
                    if not advance_editor(stored["campaign_id"], expected):
                        raise HTTPFailure(409, "unsafe_binding", "workflow_conflict", "editor_" + action, self._request_id(payload))
                else:
                    self._editor_workflow[stored["campaign_id"]] = expected + 1
                value = terminal_value(published_revision if action == "approve" else None)
            else:
                value = atomic_metadata
            stored["value"] = value
            save_editor = getattr(self.proposal_repository, "save_editor_metadata", None)
            if save_editor is not None and atomic_metadata is None:
                save_editor(proposal_id, version, value, result.published_revision_id if action == "approve" else None)
            self._persist_editor_state()
            if action == "approve":
                response = {"contract_name": "editor_proposal_approval_result", "contract_version": 1, "proposal": {"proposal_id": proposal_id, "proposal_version": version}, "outcome": "published", "published_revision": published_revision, "editor_workflow_version": expected + 1}
            else:
                response = {"contract_name": "editor_proposal_rejection_result", "contract_version": 1, "proposal": {"proposal_id": proposal_id, "proposal_version": version}, "outcome": "rejected", "editor_workflow_version": expected + 1}
            self._editor_semantic(response, stage="editor_" + action)
            self._store(receipt_operation, operation["idempotency_key"], operation["payload_digest"], 200, response)
            return 200, response

    def editor_proposal_reject(self, proposal_id: str, version: int, payload: dict):
        return self._editor_action(proposal_id, version, payload, "reject")

    def editor_proposal_approve(self, proposal_id: str, version: int, payload: dict):
        return self._editor_action(proposal_id, version, payload, "approve")

    def start_generation(self, campaign_id: str, revision_id: str, payload: dict) -> tuple[int, dict, bool]:
        self._closed_request(payload, "generation_start_request", "generation_start")
        self._semantic(payload, context={"path_params": {"campaign_id": campaign_id, "source_revision": revision_id}}, stage="generation_start")
        context = payload["context"]
        focus_record_id = context.get("record_id") if context["scope"] == "record" else None
        focus_content_digest = context.get("content_digest") if context["scope"] == "record" else None
        try:
            record, reserved = self.ai.prepare(
                payload["generation_id"], campaign_id, revision_id,
                Action(payload["action"]), payload["prompt"],
                session_id=payload.get("session_id"),
                focus_record_id=focus_record_id,
                focus_content_digest=focus_content_digest,
            )
        except ConsentRequired as exc:
            raise HTTPFailure(409, "capability_rejected", "explicit_consent_required", "generation_start", retryable=False) from exc
        except ProviderUnavailable as exc:
            raise HTTPFailure(503, "provider_unavailable", "provider_unavailable", "generation_start", retryable=True) from exc
        except ValueError as exc:
            if str(exc) == "idempotency_digest_conflict":
                raise HTTPFailure(
                    409, "idempotency_digest_conflict", "idempotency_digest_conflict",
                    "generation_start", request_id=payload["generation_id"],
                ) from exc
            raise HTTPFailure(422, "unsafe_binding", "invalid_generation_binding", "generation_start") from exc
        except KeyError as exc:
            raise HTTPFailure(
                422, "unsafe_binding", "invalid_generation_binding", "generation_start"
            ) from exc
        should_dispatch = reserved or (record.terminal_status is None and not record.events)
        return (202 if should_dispatch else 200), self._generation_view(record), should_dispatch

    def dispatch_generation(self, generation_id: str) -> None:
        with self._dispatch_lock:
            if generation_id in self._dispatching:
                return
            record = self.ai_repository.get_generation(generation_id)
            if record is None or record.terminal_status is not None or record.events:
                return
            self._dispatching.add(generation_id)
        try:
            self.ai.dispatch(record)
        finally:
            with self._dispatch_lock:
                self._dispatching.discard(generation_id)

    def generation_view(self, generation_id: str) -> tuple[int, dict]:
        record = self.ai_repository.get_generation(generation_id)
        if record is None:
            raise HTTPFailure(404, "not_found", "generation_not_found", "generation_read")
        return 200, self._generation_view(record)

    def _generation_view(self, record: GenerationRecord) -> dict:
        envelope = record.request.envelope
        sources = [{"source_id": item.source_id, "authority": item.authority,
                    "revision_id": envelope.revision_id, "order": item.order,
                    "excerpt": item.text, "excerpt_digest": item.digest}
                   for item in envelope.excerpts]
        status = record.terminal_status or "pending"
        content = record.terminal_content if status == "complete" else None
        context = (
            {"scope": "record", "record_id": record.request.focus_record_id,
             "content_digest": record.request.focus_content_digest}
            if record.request.focus_record_id is not None
            else {"scope": "campaign"}
        )
        payload = {"contract_name": "generation_view", "contract_version": 2,
                   "generation_id": record.request.generation_id,
                   "campaign_id": record.request.campaign_id,
                   "source_revision": record.request.revision_id,
                   "action": record.request.action.value, "context": context,
                   "session_id": envelope.session_id,
                   "draft_authority": "draft", "status": status,
                   "sources": sources, "source_set_digest": envelope.source_set_digest,
                   "last_sequence": len(record.events), "terminal_content": content,
                   "terminal_content_digest": text_digest(content) if content is not None else None}
        source_context = {"campaign_id": envelope.campaign_id, "revision_id": envelope.revision_id,
                          "session_id": envelope.session_id,
                          "retrieval_policy_version": envelope.retrieval_policy_version,
                          "source_set_digest": envelope.source_set_digest,
                          "sources": [{"source_id": item.source_id, "authority": item.authority,
                                       "text": item.text, "digest": item.digest, "order": item.order}
                                      for item in envelope.excerpts]}
        self._semantic(payload, context={
            "source_envelope": source_context,
            "generation_request": {
                "generation_id": record.request.generation_id,
                "campaign_id": record.request.campaign_id,
                "source_revision": record.request.revision_id,
                "action": record.request.action.value,
                "context": context,
                "session_id": envelope.session_id,
            },
        }, stage="generation_read")
        return payload

    def generation_events(self, generation_id: str, *, after: int | None, last_event_id: int | None) -> tuple[int, tuple[dict, ...]]:
        record = self.ai_repository.get_generation(generation_id)
        if record is None:
            raise HTTPFailure(404, "not_found", "generation_not_found", "ask_resume")
        if record.terminal_status is None and not record.events:
            threading.Thread(target=self.dispatch_generation, args=(generation_id,), daemon=True).start()
        context = {"after": after, "last_event_id": last_event_id, "last_sequence": len(record.events)}
        self._semantic({"contract_name": "generation_event", "contract_version": 2}, context=context, stage="generation_resume")
        observed = last_event_id if last_event_id is not None else (after or 0)
        events = tuple({"contract_name": "generation_event", "contract_version": 2,
                        "generation_id": generation_id, "sequence": event.sequence,
                        "event_type": event.event_type, "draft_fragment": event.draft_fragment,
                        "retryable": event.retryable}
                       for event in self.ai.resume(record, observed))
        return 200, events

    def create_proposal(self, generation_id: str, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "proposal_create_request", "proposal_create")
        generation = self.generation_view(generation_id)[1]
        if generation["status"] != "complete" or not generation["terminal_content"] or not generation["terminal_content_digest"]:
            raise HTTPFailure(
                422, "proposal_validation_failure", "generation_not_complete",
                "proposal_create", payload["request_id"],
            )
        if (
            payload["generation_id"] != generation_id
            or payload["campaign_id"] != generation["campaign_id"]
            or payload["source_revision"] != generation["source_revision"]
            or payload["base_revision"] != generation["source_revision"]
            or payload["source_set_digest"] != generation["source_set_digest"]
            or payload["terminal_draft_digest"] != generation["terminal_content_digest"]
        ):
            raise HTTPFailure(
                409, "source_digest_conflict", "generation_binding_mismatch",
                "proposal_create", payload["request_id"],
            )
        record = self._record(payload["campaign_id"], payload["base_revision"], payload["subject_id"])
        after = append_draft(record["content"], generation["terminal_content"])
        change = ExactTextChange(
            self._id("change", payload["proposal_id"], 1), payload["subject_id"],
            text_digest(record["content"]), after, record_type=record["record_type"],
        )
        self._validate_proposal_change(
            payload["campaign_id"], payload["base_revision"], change,
            "proposal_create", payload["request_id"],
        )
        item = ProposalVersion(
            payload["proposal_id"], 1,
            payload["campaign_id"], payload["base_revision"], (change,),
            _slice_diff_digest((change,)), _slice_payload_digest((change,)),
            generation_id=generation_id,
            source_revision=payload["source_revision"],
            source_set_digest=payload["source_set_digest"],
            terminal_draft_digest=payload["terminal_draft_digest"],
        )
        view = self._proposal_view(item)
        context = {"generation": generation, "record": record, "proposal": view,
                   "path_params": {"generation_id": generation_id}}
        self._semantic(payload, context=context, stage="proposal_create")
        replay = self._replay("proposal_create", payload["idempotency_key"], payload["payload_digest"])
        if replay:
            return 200, replay[1]
        if self._abandoned("proposal_create", payload["idempotency_key"], payload["payload_digest"]):
            try:
                existing = self.proposal_repository.get(item.proposal_id, 1)
            except KeyError:
                existing = None
            if existing == item:
                view = self._proposal_view(existing)
                self._store("proposal_create", payload["idempotency_key"], payload["payload_digest"], 201, view)
                return 200, view
        if not self._claim("proposal_create", payload["idempotency_key"], payload["payload_digest"]):
            try:
                existing = self.proposal_repository.get(item.proposal_id, 1)
            except KeyError:
                existing = None
            if existing == item:
                view = self._proposal_view(existing)
                self._store("proposal_create", payload["idempotency_key"], payload["payload_digest"], 201, view)
                return 200, view
            raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "proposal_create", payload["request_id"], True)
        try:
            self.proposal_repository.add(item)
        except Exception:
            self._release("proposal_create", payload["idempotency_key"], payload["payload_digest"])
            raise
        self._store("proposal_create", payload["idempotency_key"], payload["payload_digest"], 201, view)
        return 201, view

    def _proposal_item(self, proposal_id: str, version: int) -> ProposalVersion:
        try:
            item = self.proposal_repository.get(proposal_id, version)
        except KeyError as exc:
            raise HTTPFailure(404, "not_found", "proposal_not_found", "proposal_read") from exc
        if item is None:
            raise HTTPFailure(404, "not_found", "proposal_not_found", "proposal_read")
        if item.status in {ProposalStatus.APPROVING, ProposalStatus.APPROVED, ProposalStatus.QUARANTINED}:
            manifest = self._matching_publication(item)
            if manifest is not None:
                item = self.proposals.reconcile(item, manifest)
        if item.status is ProposalStatus.PUBLISHED and self._matching_publication(item) is None:
            raise HTTPFailure(503, "service_unavailable", "proposal_publication_unverified", "proposal_read", retryable=True)
        return item

    def _matching_publication(self, item: ProposalVersion) -> SnapshotManifest | None:
        campaign = self.campaigns.get(item.campaign_id)
        if campaign is None:
            return None
        publication_digest = exact_diff_digest(item.changes) if item.editor_metadata else item.diff_digest
        expected_revision = item.published_revision_id
        candidates = [manifest for manifest in campaign.revisions.values() if (
            manifest.parent_revision == item.base_revision
            and manifest.change_digest == publication_digest
            and manifest.publication_intent_token == self._id("token", item.proposal_id, item.version)
            and (expected_revision is None or manifest.revision_id == expected_revision)
            and self.workflow.publication_eligible(manifest)
        )]
        return candidates[0] if len(candidates) == 1 else None

    def _proposal_view(self, item: ProposalVersion) -> dict:
        change = item.changes[0]
        record = self._record(item.campaign_id, item.base_revision, change.subject_id)
        status = item.status.value
        if status in {"approving", "approved"}:
            status = "draft"
        view = {"contract_name": "proposal_view", "contract_version": 2,
                "proposal_id": item.proposal_id, "proposal_version": item.version,
                "campaign_id": item.campaign_id, "generation_id": item.generation_id,
                "source_revision": item.source_revision, "base_revision": item.base_revision,
                "source_set_digest": item.source_set_digest,
                "terminal_draft_digest": item.terminal_draft_digest,
                "artifact_kind": "proposal", "status": status,
                "exact_diff": [{"change_id": change.change_id, "subject_id": change.subject_id,
                                "change_type": change.change_kind.value, "record_type": change.record_type,
                                "from_authority": record["authority"], "to_authority": record["authority"],
                                "before_content": record["content"],
                                "after_content": normalize_text(change.replacement),
                                "before_digest": change.expected_content_digest,
                                "after_digest": text_digest(change.replacement)}],
                "diff_digest": item.diff_digest, "proposal_payload_digest": item.payload_digest,
                "validation_status": "passed", "published_revision_id": item.published_revision_id}
        return view

    def proposal_view(self, proposal_id: str, version: int) -> tuple[int, dict]:
        item = self._proposal_item(proposal_id, version)
        view = self._proposal_view(item)
        generation = self.generation_view(item.generation_id)[1]
        record = self._record(item.campaign_id, item.base_revision, item.changes[0].subject_id)
        self._semantic(view, context={"stored_proposal": view, "generation": generation, "record": record,
                                     "path_params": {"proposal_id": proposal_id, "proposal_version": version}}, stage="proposal_read")
        return 200, view

    def correct_proposal(self, proposal_id: str, version: int, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "proposal_correction_request", "proposal_correct")
        operation = payload["operation_request"]
        current = self._proposal_item(proposal_id, version)
        current_view = self._proposal_view(current)
        self._semantic(payload, context={"proposal": current_view, "path_params": {"proposal_id": proposal_id, "proposal_version": version}}, stage="proposal_correct")
        replay = self._replay("proposal_correct", operation["idempotency_key"], operation["payload_digest"])
        if replay:
            return 200, replay[1]
        change = current.changes[0]
        corrected_content = normalize_text(payload["after_content"])
        before_metadata = frontmatter(self._record(current.campaign_id, current.base_revision, change.subject_id)["content"])
        corrected_metadata = frontmatter(corrected_content)
        if any(corrected_metadata.get(field) != before_metadata.get(field) for field in ("id", "type", "status")):
            raise HTTPFailure(
                422, "proposal_validation_failure", "authority_transition_not_allowed",
                "proposal_correct", operation["request_id"],
            )
        corrected_change = ExactTextChange(change.change_id, change.subject_id, change.expected_content_digest, corrected_content, record_type=change.record_type)
        self._validate_proposal_change(
            current.campaign_id, current.base_revision, corrected_change,
            "proposal_correct", operation["request_id"],
        )
        matches = [item for item in self.proposal_repository.versions(proposal_id) if (
            item.version > version and item.campaign_id == current.campaign_id
            and item.base_revision == current.base_revision
            and item.changes == (corrected_change,)
            and item.generation_id == current.generation_id
            and item.source_revision == current.source_revision
            and item.source_set_digest == current.source_set_digest
            and item.terminal_draft_digest == current.terminal_draft_digest
        )]
        if self._abandoned("proposal_correct", operation["idempotency_key"], operation["payload_digest"]) and len(matches) == 1:
            view = self._proposal_view(matches[0])
            self._store("proposal_correct", operation["idempotency_key"], operation["payload_digest"], 201, view)
            return 200, view
        if not self._claim("proposal_correct", operation["idempotency_key"], operation["payload_digest"]):
            if len(matches) == 1:
                view = self._proposal_view(matches[0])
                self._store("proposal_correct", operation["idempotency_key"], operation["payload_digest"], 201, view)
                return 200, view
            raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "proposal_correct", operation["request_id"], True)
        try:
            corrected = self.proposals.correct(current, (corrected_change,), base_revision=current.base_revision)
        except Exception:
            self._release("proposal_correct", operation["idempotency_key"], operation["payload_digest"])
            raise
        view = self._proposal_view(corrected)
        self._store("proposal_correct", operation["idempotency_key"], operation["payload_digest"], 201, view)
        return 201, view

    def reject_proposal(self, proposal_id: str, version: int, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "proposal_rejection_request", "proposal_reject")
        operation = payload["operation_request"]
        current = self._proposal_item(proposal_id, version)
        self._semantic(payload, context={"proposal": self._proposal_view(current), "path_params": {"proposal_id": proposal_id, "proposal_version": version}}, stage="proposal_reject")
        replay = self._replay("proposal_reject", operation["idempotency_key"], operation["payload_digest"])
        if replay:
            return 200, replay[1]
        if self._abandoned("proposal_reject", operation["idempotency_key"], operation["payload_digest"]) and current.status is ProposalStatus.REJECTED:
            view = self._proposal_view(current)
            self._store("proposal_reject", operation["idempotency_key"], operation["payload_digest"], 200, view)
            return 200, view
        if not self._claim("proposal_reject", operation["idempotency_key"], operation["payload_digest"]):
            current = self._proposal_item(proposal_id, version)
            if current.status is ProposalStatus.REJECTED:
                view = self._proposal_view(current)
                self._store("proposal_reject", operation["idempotency_key"], operation["payload_digest"], 200, view)
                return 200, view
            raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "proposal_reject", operation["request_id"], True)
        try:
            rejected = self.proposals.reject(current)
        except Exception:
            self._release("proposal_reject", operation["idempotency_key"], operation["payload_digest"])
            raise
        view = self._proposal_view(rejected)
        self._store("proposal_reject", operation["idempotency_key"], operation["payload_digest"], 200, view)
        return 200, view

    def approve_proposal(self, proposal_id: str, version: int, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "proposal_approval_request", "proposal_approve")
        operation = payload["operation_request"]
        current = self._proposal_item(proposal_id, version)
        current_view = self._proposal_view(current)
        self._semantic(payload, context={"proposal": current_view, "path_params": {"proposal_id": proposal_id, "proposal_version": version}}, stage="proposal_approve")
        replay = self._replay("proposal_approve", operation["idempotency_key"], operation["payload_digest"])
        if replay:
            response = replay[1]
            response["exact_replay"] = True
            return replay[0], response
        if self._abandoned("proposal_approve", operation["idempotency_key"], operation["payload_digest"]):
            if current.status is ProposalStatus.APPROVING and self._matching_publication(current) is None:
                current = self.proposal_repository.replace_status(current, ProposalStatus.DRAFT)
            recovered = self._approval_result(current, operation["request_id"], exact_replay=True)
            if recovered is not None:
                status, result = recovered
                self._store("proposal_approve", operation["idempotency_key"], operation["payload_digest"], status, result)
                return status, result
        if not self._claim("proposal_approve", operation["idempotency_key"], operation["payload_digest"]):
            current = self._proposal_item(proposal_id, version)
            recovered = self._approval_result(current, operation["request_id"], exact_replay=True)
            if recovered is None:
                raise HTTPFailure(503, "service_unavailable", "operation_in_progress", "proposal_approve", operation["request_id"], True)
            status, result = recovered
            self._store("proposal_approve", operation["idempotency_key"], operation["payload_digest"], status, result)
            return status, result
        try:
            approved = self.proposals.approve(current, diff_digest=payload["diff_digest"], base_revision=payload["base_revision"], payload_digest=payload["proposal_payload_digest"])
        except Exception:
            self._release("proposal_approve", operation["idempotency_key"], operation["payload_digest"])
            raise
        recovered = self._approval_result(approved, operation["request_id"], exact_replay=False)
        if recovered is not None:
            status, result = recovered
        elif approved.status is ProposalStatus.DRAFT:
            self._release("proposal_approve", operation["idempotency_key"], operation["payload_digest"])
            raise HTTPFailure(
                422, "proposal_validation_failure", "proposal_validation_failure",
                "proposal_approve", operation["request_id"],
            )
        else:
            self._release("proposal_approve", operation["idempotency_key"], operation["payload_digest"])
            raise HTTPFailure(503, "service_unavailable", "proposal_publication_failed", "proposal_approve", operation["request_id"], True)
        generation = self.generation_view(current.generation_id)[1]
        record = self._record(current.campaign_id, current.base_revision, current.changes[0].subject_id)
        self._semantic(result, context={"stored_proposal": result["proposal"], "generation": generation,
                                        "record": record, "path_params": {"proposal_id": proposal_id, "proposal_version": version}}, stage="proposal_approve")
        self._store("proposal_approve", operation["idempotency_key"], operation["payload_digest"], status, result)
        return status, result

    def _approval_result(self, item: ProposalVersion, request_id: str, *, exact_replay: bool) -> tuple[int, dict] | None:
        if item.status is ProposalStatus.CONFLICT:
            return 409, {"contract_name": "proposal_approval_result", "contract_version": 2,
                "proposal": self._proposal_view(item), "outcome": "conflict", "published_revision": None,
                "error": {"category": "stale_revision", "code": "stale_campaign_head",
                          "stage": "proposal_approve", "request_id": request_id, "retryable": False},
                "exact_replay": exact_replay}
        if item.status is ProposalStatus.PUBLISHED:
            manifest = self._matching_publication(item)
            if manifest is None:
                return None
            return 200, {"contract_name": "proposal_approval_result", "contract_version": 2,
                "proposal": self._proposal_view(item), "outcome": "published",
                "published_revision": self._revision_ref(manifest), "error": None,
                "exact_replay": exact_replay}
        return None

    @staticmethod
    def _live_operation_version(payload: dict) -> int:
        operation = payload["operation_request"]
        return operation["expected_workflow_version"]

    def _live_stored_session(self, campaign_id: str, session_id: str):
        try:
            session = self.ai_repository.get_session(session_id)
        except KeyError as exc:
            raise HTTPFailure(404, "not_found", "session_not_found", "live") from exc
        if session.campaign_id != campaign_id:
            raise HTTPFailure(422, "unsafe_binding", "session_campaign_mismatch", "live")
        return session

    def _live_session_view(self, session) -> dict:
        events = [
            {
                "event_id": item.event_id, "event_type": item.capture_type.value,
                "device_id": item.device_id, "operation_id": item.operation_id,
                "device_order": item.device_order, "payload_digest": item.payload_digest,
                "base_revision": session.base_revision,
                "grounding_eligible": item.capture_type is CaptureType.CONFIRMED_FACT,
                "record_id": item.record_id,
            }
            for item in session.captures
        ]
        acknowledgements = [
            {"device_id": device_id, "operation_id": operation_id,
             "payload_digest": payload_digest, "outcome": "accepted"}
            for (device_id, operation_id), payload_digest in sorted(session.receipts.items())
        ]
        barrier = None
        if session.end_barrier is not None:
            required_ids = [{"device_id": device, "operation_id": operation}
                            for device, operation in session.end_barrier.required_operation_ids]
            end_identity = (session.end_barrier.end_device_id, session.end_barrier.end_operation_id)
            acknowledged_ids = [{"device_id": device, "operation_id": operation}
                                for device, operation in sorted(session.receipts)
                                if (device, operation) != end_identity]
            barrier = {
                "end_device_id": session.end_barrier.end_device_id,
                "end_operation_id": session.end_barrier.end_operation_id,
                "required_operation_ids": required_ids,
                "acknowledged_operation_ids": acknowledged_ids,
                "ready_for_proposal": session.end_barrier.ready_for_proposal,
            }
        return {
            "contract_name": "live_session_view", "contract_version": 2,
            "session_id": session.session_id, "campaign_id": session.campaign_id,
            "base_revision": session.base_revision,
            "reported_head_revision": session.reported_head_revision,
            "workflow_version": session.workflow_version,
            "controller": {
                "epoch": session.controller_epoch,
                "controller_id": session.controller_id, "mode": "controller",
            },
            "mode": session.mode, "events": events,
            "acknowledgements": acknowledgements, "end_barrier": barrier,
            "overlay": {
                "overlay_id": self._id("overlay", session.session_id),
                "authority": "non_canon", "base_revision": session.base_revision,
                "confirmed_fact_ids": [
                    item.event_id for item in session.captures
                    if item.capture_type is CaptureType.CONFIRMED_FACT
                ],
                "question_ids": [
                    item.event_id for item in session.captures
                    if item.capture_type is CaptureType.UNRESOLVED_QUESTION
                ],
            },
        }

    def live_start(self, campaign_id: str, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "live_start_request", "live_start")
        self._semantic(payload, context={"path_params": {"campaign_id": campaign_id}}, stage="live_start")
        operation = payload["operation_request"]
        if payload["campaign_id"] != campaign_id:
            raise HTTPFailure(422, "unsafe_binding", "session_campaign_mismatch", "live_start", operation["request_id"])
        # P1-2: only persist a session pinned to a real, publication-eligible revision.
        try:
            _, manifest = self._campaign_revision(payload["campaign_id"], payload["head_revision"])
        except HTTPFailure as exc:
            raise HTTPFailure(404, "not_found", "revision_not_found", "live_start", operation["request_id"]) from exc
        if not self.workflow.publication_eligible(manifest):
            raise HTTPFailure(422, "unsafe_binding", "revision_not_publication_eligible", "live_start", operation["request_id"])
        try:
            session = self.live.start(
                payload["session_id"], payload["campaign_id"], payload["head_revision"],
                payload["controller_id"],
            )
        except ValueError as exc:
            if str(exc) == "active_session_conflict":
                raise HTTPFailure(409, "idempotency_digest_conflict", "active_session_conflict", "live_start", operation["request_id"]) from exc
            if str(exc) == "idempotency_digest_conflict":
                raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", "live_start", operation["request_id"]) from exc
            raise HTTPFailure(422, "unsafe_binding", "invalid_live_binding", "live_start", operation["request_id"]) from exc
        except RuntimeError as exc:
            raise HTTPFailure(503, "service_unavailable", "live_feature_disabled", "live_start", operation["request_id"], True) from exc
        return 201, self._live_session_view(session)

    def live_read(self, campaign_id: str) -> tuple[int, dict]:
        # Strictly read-only (P2-C): a GET must not mutate persisted state. The
        # current session is the highest persisted session_seq (always unambiguous).
        session = self.ai_repository.campaign_session(campaign_id)
        if session is None:
            raise HTTPFailure(404, "not_found", "session_not_found", "live_read")
        return 200, self._live_session_view(session)

    def live_takeover(self, campaign_id: str, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "live_takeover_request", "live_takeover")
        stored = self._live_stored_session(campaign_id, payload["session_id"])
        self._semantic(payload, context={"path_params": {"campaign_id": campaign_id}, "stored_session": self._live_session_view(stored)}, stage="live_takeover")
        operation = payload["operation_request"]
        try:
            session = self.live.takeover(
                payload["session_id"], payload["controller_id"], payload["controller_epoch"],
                operation["expected_workflow_version"],
            )
        except StaleController as exc:
            raise HTTPFailure(409, "stale_controller", "stale_controller_epoch", "live_takeover", operation["request_id"]) from exc
        except StaleWorkflow as exc:
            raise HTTPFailure(409, "stale_workflow", "stale_workflow_version", "live_takeover", operation["request_id"]) from exc
        except ValueError as exc:
            # P2-5: repository CAS failures surface as stale workflow/controller.
            if str(exc) == "stale_workflow_version":
                raise HTTPFailure(409, "stale_workflow", "stale_workflow_version", "live_takeover", operation["request_id"]) from exc
            if str(exc) == "stale_controller_epoch":
                raise HTTPFailure(409, "stale_controller", "stale_controller_epoch", "live_takeover", operation["request_id"]) from exc
            raise HTTPFailure(422, "unsafe_binding", "invalid_live_binding", "live_takeover", operation["request_id"]) from exc
        return 200, self._live_session_view(session)

    def live_capture(self, campaign_id: str, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "live_capture_request", "live_capture")
        stored = self._live_stored_session(campaign_id, payload["session_id"])
        self._semantic(payload, context={"path_params": {"campaign_id": campaign_id}, "stored_session": self._live_session_view(stored)}, stage="live_capture")
        operation = payload["operation_request"]
        try:
            outcome = self.live.capture(
                payload["session_id"], payload["controller_id"], payload["controller_epoch"],
                self._live_operation_version(payload),
                event_id=payload["event_id"], device_id=payload["device_id"],
                operation_id=payload["operation_id"], device_order=payload["device_order"],
                capture_type=CaptureType(payload["capture_type"]), text=payload["text"],
                record_id=payload.get("record_id"),
            )
        except StaleController as exc:
            raise HTTPFailure(409, "stale_controller", "stale_controller_epoch", "live_capture", operation["request_id"]) from exc
        except StaleWorkflow as exc:
            raise HTTPFailure(409, "stale_workflow", "stale_workflow_version", "live_capture", operation["request_id"]) from exc
        except ValueError as exc:
            # P2-5: repository CAS failures surface as stale workflow/controller.
            if str(exc) == "stale_workflow_version":
                raise HTTPFailure(409, "stale_workflow", "stale_workflow_version", "live_capture", operation["request_id"]) from exc
            if str(exc) == "stale_controller_epoch":
                raise HTTPFailure(409, "stale_controller", "stale_controller_epoch", "live_capture", operation["request_id"]) from exc
            if str(exc) == "idempotency_digest_conflict":
                raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", "live_capture", operation["request_id"]) from exc
            raise HTTPFailure(422, "unsafe_binding", "unsafe_binding", "live_capture", operation["request_id"]) from exc
        session = self.ai_repository.get_session(payload["session_id"])
        result = {
            "contract_name": "live_capture_result", "contract_version": 2,
            "outcome": outcome, "campaign_id": payload["campaign_id"],
            "session_id": payload["session_id"], "event_id": payload["event_id"],
            "device_id": payload["device_id"], "operation_id": payload["operation_id"],
            "session": self._live_session_view(session),
        }
        return 200, result

    def live_end(self, campaign_id: str, payload: dict) -> tuple[int, dict]:
        self._closed_request(payload, "live_end_request", "live_end")
        stored = self._live_stored_session(campaign_id, payload["session_id"])
        self._semantic(payload, context={"path_params": {"campaign_id": campaign_id}, "stored_session": self._live_session_view(stored)}, stage="live_end")
        operation = payload["operation_request"]
        try:
            session = self.live.end(
                payload["session_id"], payload["controller_id"], payload["controller_epoch"],
                self._live_operation_version(payload),
                device_id=payload["device_id"], operation_id=payload["operation_id"],
                required_operation_ids=tuple(
                    (item["device_id"], item["operation_id"])
                    for item in payload["required_operation_ids"]
                ),
            )
        except StaleController as exc:
            raise HTTPFailure(409, "stale_controller", "stale_controller_epoch", "live_end", operation["request_id"]) from exc
        except StaleWorkflow as exc:
            raise HTTPFailure(409, "stale_workflow", "stale_workflow_version", "live_end", operation["request_id"]) from exc
        except ValueError as exc:
            # P2-5: repository CAS failures surface as stale workflow/controller.
            if str(exc) == "stale_workflow_version":
                raise HTTPFailure(409, "stale_workflow", "stale_workflow_version", "live_end", operation["request_id"]) from exc
            if str(exc) == "stale_controller_epoch":
                raise HTTPFailure(409, "stale_controller", "stale_controller_epoch", "live_end", operation["request_id"]) from exc
            if str(exc) == "idempotency_digest_conflict":
                raise HTTPFailure(409, "idempotency_digest_conflict", "idempotency_digest_conflict", "live_end", operation["request_id"]) from exc
            if str(exc) == "live_unaccepted_barrier":
                raise HTTPFailure(409, "live_barrier_conflict", "live_unaccepted_barrier", "live_end", operation["request_id"]) from exc
            raise HTTPFailure(422, "unsafe_binding", "unsafe_binding", "live_end", operation["request_id"]) from exc
        return 200, self._live_session_view(session)

    def _validate_proposal_change(
        self, campaign_id: str, base_revision: str, change: ExactTextChange,
        stage: str, request_id: str,
    ) -> None:
        base_handle = self._workspace_for_revision(campaign_id, base_revision)
        result = self.engine.stage_exact_diff(StageExactDiffRequest(
            self._id("command", request_id, "validate"), base_handle,
            exact_diff_digest((change,)), (change,),
        ))
        if result.staged_handle != base_handle:
            self.registry.discard(result.staged_handle)
        if result.status is not Status.STAGED:
            raise HTTPFailure(
                422, "proposal_validation_failure", "proposal_validation_failure",
                stage, request_id,
            )

    def _stage(self, item: ProposalVersion):
        handle = self._workspace_for_revision(item.campaign_id, item.base_revision)
        return self.engine.stage_exact_diff(StageExactDiffRequest(
            self._id("command", item.proposal_id, item.version), handle,
            exact_diff_digest(item.changes), item.changes,
        ))

    def _publish(self, item: ProposalVersion, staged, *, finalize=None) -> SnapshotManifest:
        campaign = self.campaigns[item.campaign_id]
        source = self.registry._resolve(staged.staged_handle)
        _, tree_digest = canonicalize_tree(source)
        ordinal = campaign.revisions[item.base_revision].ordinal + 1
        revision_id = self._id("revision", item.proposal_id, item.version, item.diff_digest)
        publication_digest = exact_diff_digest(item.changes) if item.editor_metadata else item.diff_digest
        intent = PublicationIntent(
            self._id("intent", item.proposal_id, item.version),
            self._id("token", item.proposal_id, item.version),
            PublicationKind.APPROVAL, item.campaign_id, revision_id,
            item.base_revision, ordinal, tree_digest, publication_digest,
        )
        self._atlas_provenance_overrides[revision_id] = (item.proposal_id, item.version)
        try:
            manifest = self.revisions.publish(
                source, intent, framework_version=__version__, adapter_version="1.0.0",
                validation_contract_digest=self.validation_contract_digest,
                before_finalize=self.atlas_rebuilder.rebuild_pending,
                rollback=lambda candidate: self.atlas_repository.delete(
                    candidate.campaign_id, candidate.revision_id
                ),
                finalizer=finalize,
            )
        finally:
            self._atlas_provenance_overrides.pop(revision_id, None)
        campaign.revisions[revision_id] = manifest
        campaign.workspaces[revision_id] = staged.staged_handle
        return manifest
