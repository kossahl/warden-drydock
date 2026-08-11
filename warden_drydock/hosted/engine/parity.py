from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from enum import Enum
import io
from pathlib import Path
import subprocess
import sys

from warden_drydock import cli, standalone

from .facade import DeterministicEngine, _tree_digest
from .models import (
    ContextRequest,
    EngineResult,
    RetrievalKind,
    RetrievalRequest,
    RetrievalResult,
    Status,
    WorkspaceHandle,
    WorkspaceRequest,
)
from .registry import WorkspaceRegistry


class ParityOperation(str, Enum):
    INDEX = "index"
    CONTEXT = "context"
    VALIDATE = "validate"
    FIND = "find"
    SHOW = "show"
    RELATED = "related"
    BACKLINKS = "backlinks"
    HISTORY = "history"


@dataclass(frozen=True)
class ParityCase:
    operation: ParityOperation
    subject_id: str | None = None
    depth: int = 1


@dataclass(frozen=True)
class ParityOutcome:
    return_code: int
    output: str


@dataclass(frozen=True)
class ParityReport:
    case: ParityCase
    matches: bool
    engine_status: Status | None
    cli_return_code: int
    standalone_return_code: int
    engine_digest: str | None
    cli_digest: str
    standalone_digest: str
    engine_semantics: tuple[tuple[str, ...], ...] = ()
    cli_semantics: tuple[tuple[str, ...], ...] = ()
    standalone_semantics: tuple[tuple[str, ...], ...] = ()
    engine_raised: bool = False
    generated_synchronized: bool = True


class ParityHarness:
    """Compare path-free facade semantics with CLI and installed standalone."""

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry

    def _run_cli(self, handle: WorkspaceHandle, case: ParityCase) -> ParityOutcome:
        root = self._registry._resolve(handle)
        stream = io.StringIO()
        with redirect_stdout(stream):
            return_code = cli.main(self._arguments(case, root=root))
        return ParityOutcome(return_code, stream.getvalue())

    def _run_standalone(
        self, handle: WorkspaceHandle, case: ParityCase
    ) -> ParityOutcome:
        root = self._registry._resolve(handle)
        script = root / "scripts" / "drydock.py"
        completed = subprocess.run(
            [sys.executable, str(script), *self._arguments(case, root=None)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return ParityOutcome(completed.returncode, completed.stdout)

    def compare(
        self,
        engine: DeterministicEngine,
        baseline: WorkspaceHandle,
        case: ParityCase | ParityOperation,
    ) -> ParityReport:
        normalized_case = case if isinstance(case, ParityCase) else ParityCase(case)
        engine_handle = self._registry.clone(baseline)
        cli_handle = self._registry.clone(baseline)
        standalone_handle = self._registry.clone(baseline)
        engine_result: EngineResult | RetrievalResult | None = None
        engine_raised = False
        try:
            try:
                engine_result = self._run_engine(
                    engine, engine_handle, normalized_case
                )
            except Exception:
                engine_raised = True
            cli_result = self._run_cli(cli_handle, normalized_case)
            synchronized = self.generated_standalone_is_synchronized(
                standalone_handle
            )
            standalone_result = (
                self._run_standalone(standalone_handle, normalized_case)
                if synchronized
                else ParityOutcome(2, "")
            )
            engine_command = (
                engine_result.result
                if isinstance(engine_result, RetrievalResult)
                else engine_result
            )
            engine_digest = _tree_digest(self._registry._resolve(engine_handle))
            cli_digest = _tree_digest(self._registry._resolve(cli_handle))
            standalone_digest = _tree_digest(
                self._registry._resolve(standalone_handle)
            )
            expected_status = (
                Status.STAGED if cli_result.return_code == 0 else Status.INVALID
            )
            engine_semantics = self._engine_semantics(
                engine_result, normalized_case
            )
            cli_semantics = self._output_semantics(
                cli_result.output, normalized_case
            )
            standalone_semantics = self._output_semantics(
                standalone_result.output, normalized_case
            )
            matches = (
                not engine_raised
                and synchronized
                and engine_command is not None
                and engine_command.status is expected_status
                and engine_command.result_digest == engine_digest
                and engine_digest == cli_digest == standalone_digest
                and cli_result.return_code == standalone_result.return_code
                and cli_semantics == standalone_semantics == engine_semantics
            )
            return ParityReport(
                normalized_case,
                matches,
                engine_command.status if engine_command else None,
                cli_result.return_code,
                standalone_result.return_code,
                engine_command.result_digest if engine_command else None,
                cli_digest,
                standalone_digest,
                engine_semantics,
                cli_semantics,
                standalone_semantics,
                engine_raised,
                synchronized,
            )
        finally:
            self._registry.discard(engine_handle)
            self._registry.discard(cli_handle)
            self._registry.discard(standalone_handle)

    def generated_standalone_is_synchronized(self, handle: WorkspaceHandle) -> bool:
        root = self._registry._resolve(handle)
        installed = (root / "scripts" / "drydock.py").read_bytes()
        source = Path(standalone.__file__).read_bytes()
        return installed == source

    @staticmethod
    def _run_engine(
        engine: DeterministicEngine,
        handle: WorkspaceHandle,
        case: ParityCase,
    ) -> EngineResult | RetrievalResult:
        if case.operation is ParityOperation.INDEX:
            return engine.index(WorkspaceRequest("parity_index", handle))
        if case.operation is ParityOperation.CONTEXT:
            return engine.context(ContextRequest("parity_context", handle))
        if case.operation is ParityOperation.VALIDATE:
            return engine.validate(WorkspaceRequest("parity_validate", handle))
        if case.subject_id is None:
            raise ValueError("retrieval parity requires a fixed subject")
        kind = RetrievalKind(case.operation.value)
        return engine.retrieve(
            RetrievalRequest(
                "parity_retrieve", handle, kind, case.subject_id, case.depth
            )
        )

    @staticmethod
    def _arguments(case: ParityCase, *, root: Path | None) -> list[str]:
        operation = case.operation.value
        if case.operation in {
            ParityOperation.INDEX,
            ParityOperation.CONTEXT,
            ParityOperation.VALIDATE,
        }:
            return [operation, str(root)] if root is not None else [operation]
        if case.subject_id is None:
            raise ValueError("retrieval parity requires a fixed subject")
        arguments = [operation, case.subject_id]
        if case.operation is ParityOperation.RELATED:
            arguments += ["--depth", str(case.depth)]
        if root is not None:
            arguments += ["--path", str(root)]
        return arguments

    @staticmethod
    def _engine_semantics(
        result: EngineResult | RetrievalResult | None,
        case: ParityCase,
    ) -> tuple[tuple[str, ...], ...]:
        if result is None:
            return (("missing_engine_result",),)
        if case.operation is ParityOperation.VALIDATE:
            assert isinstance(result, EngineResult)
            return tuple(
                (finding.severity.value, finding.code, finding.stage.value)
                for finding in result.findings
            )
        if isinstance(result, EngineResult):
            return ()
        if case.operation in {ParityOperation.FIND, ParityOperation.RELATED}:
            return tuple(
                (record.subject_id, record.record_type, record.name)
                for record in result.records
            )
        if case.operation is ParityOperation.SHOW:
            return tuple((record.content or "",) for record in result.records)
        if case.operation is ParityOperation.BACKLINKS:
            return tuple(
                (
                    connection.subject_id,
                    connection.relationship,
                    connection.state,
                    connection.context,
                )
                for connection in result.connections
            )
        return tuple(
            (
                connection.subject_id,
                connection.relationship,
                connection.context,
            )
            for connection in result.connections
        )

    @staticmethod
    def _output_semantics(
        output: str, case: ParityCase
    ) -> tuple[tuple[str, ...], ...]:
        if case.operation is ParityOperation.VALIDATE:
            semantics: list[tuple[str, ...]] = []
            for line in output.splitlines():
                if line.startswith("WARNING:"):
                    semantics.append(("warning", "validation_warning", "validate"))
                elif line.startswith("ERROR:"):
                    semantics.append(("error", "validation_error", "validate"))
            return tuple(semantics)
        if case.operation in {ParityOperation.INDEX, ParityOperation.CONTEXT}:
            return ()
        if case.operation is ParityOperation.SHOW:
            return ((output,),) if output else ()
        rows = [tuple(line.split("\t")) for line in output.splitlines() if line]
        if case.operation in {ParityOperation.FIND, ParityOperation.RELATED}:
            return tuple(row[:3] for row in rows)
        if case.operation is ParityOperation.HISTORY:
            return tuple(row[:3] for row in rows)
        return tuple(rows)
