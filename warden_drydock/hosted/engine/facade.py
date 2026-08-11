from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
from collections.abc import Callable

from warden_drydock.core.generator import init_campaign
from warden_drydock.standalone import (
    _graph_or_exit,
    _adapter_config,
    _summary,
    build_context,
    build_indexes,
    create_entity,
    frontmatter,
    related_entities,
    validate_campaign,
)

from .models import (
    ChangeKind,
    ContextRequest,
    EngineResult,
    ExactTextChange,
    Finding,
    InitializeRequest,
    RetrievalKind,
    RetrievalRequest,
    RetrievalResult,
    RetrievedConnection,
    RetrievedRecord,
    Severity,
    Stage,
    StageExactDiffRequest,
    Status,
    WorkspaceHandle,
    WorkspaceRequest,
    content_digest,
    exact_diff_digest,
    _is_public_id,
)
from .registry import UnknownWorkspaceError, UnsafeWorkspaceError, WorkspaceRegistry


_DOMAIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _canonical_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


class DeterministicEngine:
    """Typed in-process facade over deterministic Drydock operations."""

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    def initialize(self, request: InitializeRequest) -> EngineResult:
        stage = Stage.INITIALIZE
        input_digest = _canonical_digest({"adapter": request.adapter, "name": request.campaign_name})
        invalid = self._validate_command(request.command_id, request.workspace_handle, stage, input_digest, "campaign_initialize")
        if invalid:
            return invalid
        if not _DOMAIN_ID.fullmatch(request.adapter):
            return self._failure(request.command_id, "campaign_initialize", request.workspace_handle, input_digest, stage, "unsafe_binding")
        try:
            root = self._registry._resolve(request.workspace_handle)
            with redirect_stdout(io.StringIO()):
                init_campaign(root, name=request.campaign_name, adapter=request.adapter)
            return self._success(request.command_id, "campaign_initialize", request.workspace_handle, request.workspace_handle, input_digest, root)
        except (UnknownWorkspaceError, UnsafeWorkspaceError) as exc:
            return self._workspace_failure(request.command_id, "campaign_initialize", request.workspace_handle, input_digest, stage, exc)
        except (OSError, SystemExit, ValueError):
            return self._failure(request.command_id, "campaign_initialize", request.workspace_handle, input_digest, stage, "initialization_failed")

    def index(self, request: WorkspaceRequest) -> EngineResult:
        return self._workspace_operation(request, Stage.INDEX, "artifacts_rebuild", build_indexes, ("entity_index", "connection_index"))

    def context(self, request: ContextRequest) -> EngineResult:
        input_value = {"focus_id": request.focus_id, "depth": request.depth, "max_records": request.max_records}
        input_digest = _canonical_digest(input_value)
        invalid = self._validate_command(request.command_id, request.workspace_handle, Stage.CONTEXT, input_digest, "artifacts_rebuild")
        if invalid:
            return invalid
        if request.depth < 0 or request.max_records <= 0 or (request.focus_id is not None and not _DOMAIN_ID.fullmatch(request.focus_id)):
            return self._failure(request.command_id, "artifacts_rebuild", request.workspace_handle, input_digest, Stage.CONTEXT, "unsafe_binding")
        try:
            root = self._registry._resolve(request.workspace_handle)
            with redirect_stdout(io.StringIO()):
                if request.focus_id:
                    related_entities(root, request.focus_id, request.depth)
                else:
                    _graph_or_exit(root)
                build_indexes(root)
                build_context(root, focus=request.focus_id, depth=request.depth, max_records=request.max_records)
            return self._success(request.command_id, "artifacts_rebuild", request.workspace_handle, request.workspace_handle, input_digest, root, ("entity_index", "connection_index", "ai_context"))
        except (UnknownWorkspaceError, UnsafeWorkspaceError) as exc:
            return self._workspace_failure(request.command_id, "artifacts_rebuild", request.workspace_handle, input_digest, Stage.CONTEXT, exc)
        except (OSError, SystemExit, ValueError):
            return self._failure(request.command_id, "artifacts_rebuild", request.workspace_handle, input_digest, Stage.CONTEXT, "context_failed")

    def validate(self, request: WorkspaceRequest) -> EngineResult:
        input_digest = _canonical_digest({})
        invalid = self._validate_command(request.command_id, request.workspace_handle, Stage.VALIDATE, input_digest, "campaign_validate")
        if invalid:
            return invalid
        try:
            root = self._registry._resolve(request.workspace_handle)
            output = io.StringIO()
            with redirect_stdout(output):
                return_code = validate_campaign(root)
            findings = self._validation_findings(output.getvalue(), request.workspace_handle, Stage.VALIDATE)
            return EngineResult(request.command_id, "campaign_validate", request.workspace_handle, request.workspace_handle, input_digest, _tree_digest(root), Status.INVALID if return_code else Status.STAGED, findings)
        except (UnknownWorkspaceError, UnsafeWorkspaceError) as exc:
            return self._workspace_failure(request.command_id, "campaign_validate", request.workspace_handle, input_digest, Stage.VALIDATE, exc)
        except (OSError, SystemExit, ValueError):
            return self._failure(request.command_id, "campaign_validate", request.workspace_handle, input_digest, Stage.VALIDATE, "validation_failed")

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        input_digest = _canonical_digest({"depth": request.depth, "kind": request.kind.value, "subject_id": request.subject_id})
        invalid = self._validate_command(request.command_id, request.workspace_handle, Stage.RETRIEVE, input_digest, "retrieve")
        if invalid:
            return RetrievalResult(invalid)
        invalid_subject = (
            not request.subject_id
            or len(request.subject_id) > 200
            or (
                request.kind is not RetrievalKind.FIND
                and not _DOMAIN_ID.fullmatch(request.subject_id)
            )
        )
        if request.depth < 0 or invalid_subject:
            return RetrievalResult(self._failure(request.command_id, "retrieve", request.workspace_handle, input_digest, Stage.RETRIEVE, "unsafe_binding"))
        try:
            root = self._registry._resolve(request.workspace_handle)
            entities, connections = _graph_or_exit(root)
            records: list[RetrievedRecord] = []
            edges: list[RetrievedConnection] = []
            if request.kind is RetrievalKind.FIND:
                needle = request.subject_id.casefold()
                selected = [entity for entity in entities.values() if needle in entity.entity_id.casefold() or needle in entity.name.casefold() or needle in _summary(entity).casefold()]
            elif request.kind is RetrievalKind.SHOW:
                selected = [entities[request.subject_id]] if request.subject_id in entities else []
            elif request.kind is RetrievalKind.RELATED:
                selected = related_entities(root, request.subject_id, request.depth)
            else:
                selected = []
                if request.subject_id not in entities:
                    raise KeyError(request.subject_id)
                historical = {"session", "debrief", "faction-turn", "consequence"}
                for edge in connections:
                    source = entities.get(edge.source_id)
                    if edge.target_id == request.subject_id and (request.kind is RetrievalKind.BACKLINKS or (source and source.entity_type in historical)):
                        edges.append(RetrievedConnection(edge.source_id, edge.target_id, edge.relationship, edge.state, edge.context))
            if request.kind in {RetrievalKind.SHOW, RetrievalKind.RELATED} and not selected:
                raise KeyError(request.subject_id)
            ordered = (
                selected
                if request.kind is RetrievalKind.RELATED
                else sorted(selected, key=lambda item: item.entity_id)
            )
            for entity in ordered:
                records.append(RetrievedRecord(entity.entity_id, entity.entity_type, entity.name, entity.status, entity.text))
            result = self._success(request.command_id, "retrieve", request.workspace_handle, request.workspace_handle, input_digest, root)
            return RetrievalResult(result, tuple(records), tuple(edges))
        except KeyError:
            return RetrievalResult(self._failure(request.command_id, "retrieve", request.workspace_handle, input_digest, Stage.RETRIEVE, "subject_unknown", Status.INVALID))
        except (UnknownWorkspaceError, UnsafeWorkspaceError) as exc:
            return RetrievalResult(self._workspace_failure(request.command_id, "retrieve", request.workspace_handle, input_digest, Stage.RETRIEVE, exc))
        except (OSError, SystemExit, ValueError):
            return RetrievalResult(self._failure(request.command_id, "retrieve", request.workspace_handle, input_digest, Stage.RETRIEVE, "retrieval_failed"))

    def stage_exact_diff(self, request: StageExactDiffRequest) -> EngineResult:
        input_digest = _canonical_digest({"diff_digest": request.diff_digest})
        invalid = self._validate_command(request.command_id, request.workspace_handle, Stage.STAGE, input_digest, "proposal_stage")
        if invalid:
            return invalid
        if not request.changes or not _DIGEST.fullmatch(request.diff_digest) or request.diff_digest != exact_diff_digest(request.changes):
            return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "diff_digest_mismatch", Status.INVALID)
        if any(
            not _DOMAIN_ID.fullmatch(change.subject_id)
            or not _is_public_id(change.change_id)
            or (
                change.expected_content_digest is not None
                and not _DIGEST.fullmatch(change.expected_content_digest)
            )
            or (change.record_type is not None and not _DOMAIN_ID.fullmatch(change.record_type))
            for change in request.changes
        ):
            return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "unsafe_binding", Status.INVALID)
        subject_ids = [change.subject_id for change in request.changes]
        if len(subject_ids) != len(set(subject_ids)):
            return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "duplicate_subject", Status.INVALID)
        staged: WorkspaceHandle | None = None
        try:
            source_root = self._registry._resolve(request.workspace_handle)
            entities, _ = _graph_or_exit(source_root)
            targets: list[tuple[ExactTextChange, Path | None]] = []
            for change in request.changes:
                entity = entities.get(change.subject_id)
                if change.change_kind is ChangeKind.CREATE:
                    if entity is not None or change.expected_content_digest is not None or not change.replacement or change.record_type is None:
                        return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "change_binding_mismatch", Status.INVALID)
                    metadata = frontmatter(change.replacement)
                    if metadata.get("id") != change.subject_id or metadata.get("type") != change.record_type:
                        return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "change_binding_mismatch", Status.INVALID)
                    config = _adapter_config(source_root)
                    rule = config.get("entity_types", {}).get(change.record_type)
                    if not isinstance(rule, dict):
                        return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "record_type_unknown", Status.INVALID)
                    targets.append((change, None))
                    continue
                if entity is None:
                    return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "subject_unknown", Status.INVALID)
                target = source_root / entity.path
                if content_digest(target.read_text(encoding="utf-8")) != change.expected_content_digest:
                    return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "content_digest_mismatch", Status.INVALID)
                if change.change_kind is ChangeKind.DELETE and change.replacement:
                    return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "change_binding_mismatch", Status.INVALID)
                targets.append((change, entity.path))
            staged = self._registry.clone(request.workspace_handle)
            staged_root = self._registry._resolve(staged)
            output = io.StringIO()
            with redirect_stdout(output):
                for change, relative in targets:
                    if change.change_kind is ChangeKind.CREATE:
                        created = create_entity(staged_root, change.record_type or "", change.subject_id, None)
                        created.write_text(change.replacement, encoding="utf-8")
                    elif change.change_kind is ChangeKind.DELETE:
                        assert relative is not None
                        (staged_root / relative).unlink()
                    else:
                        assert relative is not None
                        (staged_root / relative).write_text(change.replacement, encoding="utf-8")
                build_indexes(staged_root)
                build_context(staged_root)
                return_code = validate_campaign(staged_root)
            findings = self._validation_findings(output.getvalue(), staged, Stage.STAGE)
            return EngineResult(request.command_id, "proposal_stage", request.workspace_handle, staged, input_digest, _tree_digest(staged_root), Status.INVALID if return_code else Status.STAGED, findings, ("entity_index", "connection_index", "ai_context"))
        except (UnknownWorkspaceError, UnsafeWorkspaceError) as exc:
            self._discard_failed_stage(staged)
            return self._workspace_failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, exc)
        except SystemExit:
            self._discard_failed_stage(staged)
            return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "proposal_validation_failure", Status.INVALID)
        except (OSError, ValueError):
            self._discard_failed_stage(staged)
            return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "stage_failed")
        except Exception:
            self._discard_failed_stage(staged)
            return self._failure(request.command_id, "proposal_stage", request.workspace_handle, input_digest, Stage.STAGE, "stage_failed")

    def _discard_failed_stage(self, staged: WorkspaceHandle | None) -> None:
        if staged is None:
            return
        try:
            self._registry.discard(staged)
        except OSError:
            # discard unregisters before filesystem deletion, so a cleanup I/O
            # failure cannot leave a caller-accessible partial workspace.
            pass

    def _workspace_operation(self, request: WorkspaceRequest, stage: Stage, command: str, operation: Callable[[Path], object], artifact_ids: tuple[str, ...]) -> EngineResult:
        input_digest = _canonical_digest({})
        invalid = self._validate_command(request.command_id, request.workspace_handle, stage, input_digest, command)
        if invalid:
            return invalid
        try:
            root = self._registry._resolve(request.workspace_handle)
            with redirect_stdout(io.StringIO()):
                operation(root)
            return self._success(request.command_id, command, request.workspace_handle, request.workspace_handle, input_digest, root, artifact_ids)
        except (UnknownWorkspaceError, UnsafeWorkspaceError) as exc:
            return self._workspace_failure(request.command_id, command, request.workspace_handle, input_digest, stage, exc)
        except SystemExit:
            return self._failure(request.command_id, command, request.workspace_handle, input_digest, stage, "validation_error", Status.INVALID)
        except (OSError, ValueError):
            return self._failure(request.command_id, command, request.workspace_handle, input_digest, stage, f"{stage.value}_failed")

    def _validate_command(self, command_id: str, handle: WorkspaceHandle, stage: Stage, input_digest: str, command: str) -> EngineResult | None:
        if not _is_public_id(command_id):
            return self._failure("invalid_command", command, handle, input_digest, stage, "unsafe_binding", Status.INVALID)
        return None

    def _success(self, command_id: str, command: str, snapshot_handle: WorkspaceHandle, staged_handle: WorkspaceHandle, input_digest: str, root: Path, artifact_ids: tuple[str, ...] = ()) -> EngineResult:
        return EngineResult(command_id, command, snapshot_handle, staged_handle, input_digest, _tree_digest(root), Status.STAGED, (), artifact_ids)

    def _failure(self, command_id: str, command: str, handle: WorkspaceHandle, input_digest: str, stage: Stage, code: str, status: Status = Status.FAILED) -> EngineResult:
        return EngineResult(command_id, command, handle, handle, input_digest, None, status, (Finding(code, Severity.ERROR, stage, handle.value),))

    def _workspace_failure(self, command_id: str, command: str, handle: WorkspaceHandle, input_digest: str, stage: Stage, exc: Exception) -> EngineResult:
        code = "workspace_unknown" if isinstance(exc, UnknownWorkspaceError) else "workspace_unsafe"
        return self._failure(command_id, command, handle, input_digest, stage, code)

    def _validation_findings(self, output: str, handle: WorkspaceHandle, stage: Stage) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for line in output.splitlines():
            if line.startswith("WARNING:"):
                findings.append(Finding("validation_warning", Severity.WARNING, stage, handle.value))
            elif line.startswith("ERROR:"):
                findings.append(Finding("validation_error", Severity.ERROR, stage, handle.value))
        return tuple(findings)
