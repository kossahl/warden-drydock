from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading

from warden_drydock import __version__
from warden_drydock.hosted.ai.models import Action, GenerationRecord, ProviderConsent
from warden_drydock.hosted.ai.provider import OpenAIResponsesAdapter, ProviderUnavailable
from warden_drydock.hosted.ai.repository import InMemoryAIRepository
from warden_drydock.hosted.ai.retrieval import DeterministicSourceSelector, EngineSourceLoader
from warden_drydock.hosted.ai.service import ConsentRequired, GroundedAIService
from warden_drydock.hosted.engine import (
    DeterministicEngine, ExactTextChange, InitializeRequest, RetrievalKind,
    ContextRequest, RetrievalRequest, StageExactDiffRequest, Status,
    WorkspaceHandle, WorkspaceRegistry, WorkspaceRequest,
    content_digest, exact_diff_digest,
)
from warden_drydock.hosted.proposals.service import (
    InMemoryProposalRepository, ProposalService, ProposalStatus, ProposalVersion,
)
from warden_drydock.hosted.revisions import (
    FileSnapshotStore, InMemoryWorkflowRepository, PublicationIntent,
    PublicationKind, RevisionService, SnapshotManifest, canonicalize_tree,
)
from warden_drydock.standalone import frontmatter

from .contracts import (
    HTTPContractSemanticError, append_draft, canonical_digest, normalize_text,
    text_digest, validate_http_semantics,
)
from .repository import InMemoryHTTPRepository, ReceiptConflict


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
            "contract_name": "error_response", "contract_version": 1,
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

    validation_contract_digest = hashlib.sha256(b"hosted-http-v1").hexdigest()
    _request_fields = {
        "provider_consent_request": {"contract_name", "contract_version", "operation_request", "input"},
        "campaign_create_request": {"contract_name", "contract_version", "operation_request", "input"},
        "ask_start_request": {"contract_name", "contract_version", "generation_id", "campaign_id", "source_revision", "action", "prompt"},
        "proposal_create_request": {"contract_name", "contract_version", "request_id", "idempotency_key", "payload_digest", "generation_id", "proposal_id", "campaign_id", "source_revision", "base_revision", "source_set_digest", "terminal_draft_digest", "subject_id"},
        "proposal_correction_request": {"contract_name", "contract_version", "operation_request", "proposal_id", "proposal_version", "source_revision", "base_revision", "change_id", "subject_id", "after_content"},
        "proposal_rejection_request": {"contract_name", "contract_version", "operation_request", "proposal_id", "proposal_version", "source_revision", "base_revision"},
        "proposal_approval_request": {"contract_name", "contract_version", "operation_request", "proposal_id", "proposal_version", "source_revision", "base_revision", "expected_campaign_head", "diff_digest", "proposal_payload_digest", "warden_confirmed"},
    }

    def __init__(self, root: Path | None = None, *, snapshot_root: Path | None = None,
                 provider=None, receipts=None,
                 proposal_repository=None, workflow_repository=None,
                 ai_repository=None) -> None:
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
        self.ai_repository = ai_repository or InMemoryAIRepository()
        self.provider = provider or OpenAIResponsesAdapter()
        loader = EngineSourceLoader(self.engine, self._workspace_for_revision)
        self.ai = GroundedAIService(
            self.ai_repository, DeterministicSourceSelector(), self.provider, loader,
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
        self.campaigns: dict[str, CampaignState] = {}
        self._lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._dispatching: set[str] = set()
        self._recover_state()
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

    def _closed_request(self, payload: dict, expected: str, stage: str) -> None:
        if payload.get("contract_name") != expected or payload.get("contract_version") != 1 or set(payload) != self._request_fields[expected]:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        operation = payload.get("operation_request")
        if operation is not None:
            base = {"contract_name", "contract_version", "request_id", "operation", "idempotency_key", "payload_digest", "expected_revision", "expected_workflow_version"}
            if expected in {"proposal_correction_request", "proposal_rejection_request"}:
                base.add("subject_id")
            elif expected == "proposal_approval_request":
                base.update({"subject_id", "intent_digest"})
            if not isinstance(operation, dict) or set(operation) != base or operation.get("contract_name") != "operation_request" or operation.get("contract_version") != 1:
                raise HTTPFailure(422, "unsafe_binding", "invalid_operation_shape", stage, self._request_id(payload))
            expected_operation = {
                "provider_consent_request": "provider_consent",
                "campaign_create_request": "campaign_create",
                "proposal_correction_request": "proposal_correct",
                "proposal_rejection_request": "proposal_reject",
                "proposal_approval_request": "proposal_approve",
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
        elif expected == "ask_start_request":
            valid = (
                public(payload.get("generation_id")) and public(payload.get("campaign_id"))
                and public(payload.get("source_revision")) and payload.get("action") == "ask"
                and isinstance(payload.get("prompt"), str) and 1 <= len(payload["prompt"]) <= 4000
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
        if not valid:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_value", stage, request_id)
        if expected == "campaign_create_request" and payload["input"].get("adapter_id") != "mothership":
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        if expected == "provider_consent_request" and payload["input"].get("explicit") is not True:
            raise HTTPFailure(422, "unsafe_binding", "invalid_request_shape", stage, self._request_id(payload))
        if expected == "ask_start_request" and payload.get("action") != "ask":
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
            "contract_name": "provider_readiness_response", "contract_version": 1,
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
            "contract_name": "campaign_revision_view", "contract_version": 1,
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
        return {"contract_name": "record_view", "contract_version": 1,
                "campaign_id": campaign_id, "revision_id": revision_id,
                "record_id": record.subject_id, "record_type": record.record_type,
                "name": record.name, "authority": self._authority(record.status),
                "content": record.content}

    def record_view(self, campaign_id: str, revision_id: str, record_id: str) -> tuple[int, dict]:
        return 200, self._record(campaign_id, revision_id, record_id)

    def start_ask(self, campaign_id: str, revision_id: str, payload: dict) -> tuple[int, dict, bool]:
        self._closed_request(payload, "ask_start_request", "ask_start")
        self._semantic(payload, context={"path_params": {"campaign_id": campaign_id, "source_revision": revision_id}}, stage="ask_start")
        try:
            record, reserved = self.ai.prepare(
                payload["generation_id"], campaign_id, revision_id,
                Action.ASK, payload["prompt"],
            )
        except ConsentRequired as exc:
            raise HTTPFailure(409, "capability_rejected", "explicit_consent_required", "ask_start", retryable=False) from exc
        except ProviderUnavailable as exc:
            raise HTTPFailure(503, "provider_unavailable", "provider_unavailable", "ask_start", retryable=True) from exc
        except ValueError as exc:
            if str(exc) == "idempotency_digest_conflict":
                raise HTTPFailure(
                    409, "idempotency_digest_conflict", "idempotency_digest_conflict",
                    "ask_start", request_id=payload["generation_id"],
                ) from exc
            raise HTTPFailure(422, "unsafe_binding", "invalid_ask_request", "ask_start") from exc
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
        payload = {"contract_name": "generation_view", "contract_version": 1,
                   "generation_id": record.request.generation_id,
                   "campaign_id": record.request.campaign_id,
                   "source_revision": record.request.revision_id,
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
        self._semantic(payload, context={"source_envelope": source_context}, stage="generation_read")
        return payload

    def generation_events(self, generation_id: str, *, after: int | None, last_event_id: int | None) -> tuple[int, tuple[dict, ...]]:
        record = self.ai_repository.get_generation(generation_id)
        if record is None:
            raise HTTPFailure(404, "not_found", "generation_not_found", "ask_resume")
        if record.terminal_status is None and not record.events:
            threading.Thread(target=self.dispatch_generation, args=(generation_id,), daemon=True).start()
        context = {"after": after, "last_event_id": last_event_id, "last_sequence": len(record.events)}
        self._semantic({"contract_name": "generation_event", "contract_version": 1}, context=context, stage="ask_resume")
        observed = last_event_id if last_event_id is not None else (after or 0)
        events = tuple({"contract_name": "generation_event", "contract_version": 1,
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
        expected_revision = item.published_revision_id
        candidates = [manifest for manifest in campaign.revisions.values() if (
            manifest.parent_revision == item.base_revision
            and manifest.change_digest == item.diff_digest
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
        view = {"contract_name": "proposal_view", "contract_version": 1,
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
            return 409, {"contract_name": "proposal_approval_result", "contract_version": 1,
                "proposal": self._proposal_view(item), "outcome": "conflict", "published_revision": None,
                "error": {"category": "stale_revision", "code": "stale_campaign_head",
                          "stage": "proposal_approve", "request_id": request_id, "retryable": False},
                "exact_replay": exact_replay}
        if item.status is ProposalStatus.PUBLISHED:
            manifest = self._matching_publication(item)
            if manifest is None:
                return None
            return 200, {"contract_name": "proposal_approval_result", "contract_version": 1,
                "proposal": self._proposal_view(item), "outcome": "published",
                "published_revision": self._revision_ref(manifest), "error": None,
                "exact_replay": exact_replay}
        return None

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

    def _publish(self, item: ProposalVersion, staged) -> SnapshotManifest:
        campaign = self.campaigns[item.campaign_id]
        source = self.registry._resolve(staged.staged_handle)
        _, tree_digest = canonicalize_tree(source)
        ordinal = campaign.revisions[item.base_revision].ordinal + 1
        revision_id = self._id("revision", item.proposal_id, item.version, item.diff_digest)
        intent = PublicationIntent(
            self._id("intent", item.proposal_id, item.version),
            self._id("token", item.proposal_id, item.version),
            PublicationKind.APPROVAL, item.campaign_id, revision_id,
            item.base_revision, ordinal, tree_digest, item.diff_digest,
        )
        manifest = self.revisions.publish(
            source, intent, framework_version=__version__, adapter_version="1.0.0",
            validation_contract_digest=self.validation_contract_digest,
        )
        campaign.revisions[revision_id] = manifest
        campaign.workspaces[revision_id] = staged.staged_handle
        return manifest
