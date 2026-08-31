from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from warden_drydock.hosted.engine import (
    ChangeKind,
    ContextRequest,
    DeterministicEngine,
    ExactTextChange,
    InitializeRequest,
    RetrievalKind,
    RetrievalRequest,
    Severity,
    Stage,
    StageExactDiffRequest,
    Status,
    WorkspaceHandle,
    WorkspaceRegistry,
    WorkspaceRequest,
    content_digest,
    exact_diff_digest,
)
from warden_drydock.hosted.engine.parity import (
    ParityCase,
    ParityHarness,
    ParityOperation,
)


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = WorkspaceRegistry(Path(self.temporary.name))
        self.engine = DeterministicEngine(self.registry)
        self.handle = self.registry.allocate()
        result = self.engine.initialize(
            InitializeRequest("command_initialize", self.handle, "Engine Test")
        )
        self.assertEqual(Status.STAGED, result.status)

    def show(self, handle: WorkspaceHandle | None = None):
        return self.engine.retrieve(
            RetrievalRequest(
                "command_show",
                handle or self.handle,
                RetrievalKind.SHOW,
                "campaign-main",
            )
        )


class WorkspaceBoundaryTests(EngineTestCase):
    def test_facade_request_types_have_no_path_fields(self) -> None:
        request_types = (
            InitializeRequest,
            WorkspaceRequest,
            ContextRequest,
            RetrievalRequest,
            StageExactDiffRequest,
            ExactTextChange,
        )
        for request_type in request_types:
            names = {field.name for field in fields(request_type)}
            self.assertFalse(any("path" in name for name in names), request_type)

    def test_handle_rejects_traversal_and_unknown_handle_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            WorkspaceHandle("../private")
        unknown = WorkspaceHandle("workspace_unknown")
        result = self.engine.validate(WorkspaceRequest("command_validate", unknown))
        self.assertEqual(Status.FAILED, result.status)
        self.assertEqual("workspace_unknown", result.findings[0].code)
        self.assertNotIn(self.temporary.name, repr(result))

    def test_workspace_handle_enforces_accepted_public_id_bounds(self) -> None:
        self.assertEqual("abc", WorkspaceHandle("abc").value)
        self.assertEqual(80, len(WorkspaceHandle("a" + "0" * 79).value))
        for invalid in ("a", "ab", "a" + "0" * 80):
            with self.subTest(invalid_length=len(invalid)):
                with self.assertRaises(ValueError):
                    WorkspaceHandle(invalid)

    def test_registry_rejects_a_server_mapping_that_escapes_storage(self) -> None:
        outside = Path(self.temporary.name).parent
        self.registry._paths[self.handle] = outside
        result = self.engine.validate(
            WorkspaceRequest("command_validate", self.handle)
        )
        self.assertEqual(Status.FAILED, result.status)
        self.assertEqual("workspace_unsafe", result.findings[0].code)
        self.assertNotIn(str(outside), repr(result))

    def test_registry_rejects_detected_symlink_inside_workspace(self) -> None:
        root = self.registry._resolve(self.handle)
        detected_link = root / "detected-link"
        detected_link.write_text("synthetic", encoding="utf-8")
        original = Path.is_symlink

        def detect(candidate: Path) -> bool:
            return candidate == detected_link or original(candidate)

        with mock.patch.object(Path, "is_symlink", autospec=True, side_effect=detect):
            result = self.engine.validate(
                WorkspaceRequest("command_validate", self.handle)
            )
        self.assertEqual(Status.FAILED, result.status)
        self.assertEqual("workspace_unsafe", result.findings[0].code)
        self.assertNotIn(str(detected_link), repr(result))

    def test_initialize_rejects_traversal_like_adapter_binding(self) -> None:
        target = self.registry.allocate()
        result = self.engine.initialize(
            InitializeRequest("command_initialize", target, "Unsafe", "../../private")
        )
        self.assertEqual(Status.FAILED, result.status)
        self.assertEqual("unsafe_binding", result.findings[0].code)

    def test_public_facade_has_no_publication_or_approval_authority(self) -> None:
        public_methods = {
            name
            for name, member in inspect.getmembers(DeterministicEngine, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(
            {"initialize", "index", "context", "validate", "retrieve", "stage_exact_diff"},
            public_methods,
        )
        self.assertFalse(public_methods & {"publish", "approve", "promote", "apply"})


class DeterministicOperationTests(EngineTestCase):
    def test_repeated_index_context_validate_and_retrieve_are_deterministic(self) -> None:
        first_index = self.engine.index(WorkspaceRequest("command_index", self.handle))
        second_index = self.engine.index(WorkspaceRequest("command_index", self.handle))
        self.assertEqual(first_index, second_index)

        request = ContextRequest("command_context", self.handle)
        first_context = self.engine.context(request)
        second_context = self.engine.context(request)
        self.assertEqual(first_context, second_context)

        first_validation = self.engine.validate(WorkspaceRequest("command_validate", self.handle))
        second_validation = self.engine.validate(WorkspaceRequest("command_validate", self.handle))
        self.assertEqual(first_validation, second_validation)
        self.assertEqual(Status.STAGED, first_validation.status)

        first_retrieval = self.show()
        second_retrieval = self.show()
        self.assertEqual(first_retrieval, second_retrieval)
        self.assertEqual("campaign-main", first_retrieval.records[0].subject_id)
        self.assertNotIn("path", repr(first_retrieval.result))

    def test_diff_digest_has_stable_golden_value(self) -> None:
        change = ExactTextChange(
            "change_one",
            "campaign-main",
            "a" * 64,
            "replacement\n",
        )
        canonical = json.dumps(
            [
                {
                    "change_id": change.change_id,
                    "change_kind": change.change_kind.value,
                    "expected_content_digest": change.expected_content_digest,
                    "record_type": change.record_type,
                    "replacement_digest": content_digest(change.replacement),
                    "subject_id": change.subject_id,
                }
            ],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            exact_diff_digest((change,)),
        )
        self.assertEqual(
            exact_diff_digest((change,)),
            exact_diff_digest((change,)),
        )

    def test_diff_digest_is_content_isolated(self) -> None:
        base = ExactTextChange(
            "change_one",
            "campaign-main",
            "a" * 64,
            "replacement\n",
        )
        variants = (
            replace(base, change_id="change_two"),
            replace(base, subject_id="campaign-alt"),
            replace(base, expected_content_digest="b" * 64),
            replace(base, replacement="other\n"),
            replace(base, change_kind=ChangeKind.DELETE),
            replace(base, record_type="npc"),
        )
        digests = {exact_diff_digest((base,))}
        digests.update(exact_diff_digest((variant,)) for variant in variants)
        self.assertEqual(1 + len(variants), len(digests))
        self.assertNotEqual(
            exact_diff_digest((base, variants[0])),
            exact_diff_digest((variants[0], base)),
        )

    def test_diff_digest_feeds_approval_and_publication_binding(self) -> None:
        from warden_drydock.hosted.proposals.service import (
            InMemoryProposalRepository,
            ProposalService,
            ProposalStatus,
        )
        from warden_drydock.hosted.revisions.models import FileHash, SnapshotManifest

        change = ExactTextChange(
            "change_one",
            "campaign-main",
            "a" * 64,
            "replacement\n",
        )
        digest = exact_diff_digest((change,))

        def manifest(version) -> SnapshotManifest:
            return SnapshotManifest(
                version.campaign_id,
                "revision_binding",
                version.base_revision,
                1,
                "b" * 64,
                (FileHash("record.md", "c" * 64),),
                "0.3.0",
                "1.0.0",
                "d" * 64,
                version.diff_digest,
                "token_binding",
            )

        repository = InMemoryProposalRepository()
        service = ProposalService(
            repository,
            head=lambda _proposal_id: "rev_binding",
            stage=lambda _version: type("Stage", (), {"status": Status.STAGED})(),
            publish=lambda _version, _staged: manifest(_version),
        )
        version = service.draft(
            "proposal_binding",
            "campaign_binding",
            "rev_binding",
            (change,),
        )
        self.assertEqual(digest, version.diff_digest)
        with self.assertRaises(ValueError):
            service.approve(
                version,
                diff_digest="0" * 64,
                base_revision=version.base_revision,
                payload_digest=version.payload_digest,
            )
        approved = service.approve(
            version,
            diff_digest=version.diff_digest,
            base_revision=version.base_revision,
            payload_digest=version.payload_digest,
        )
        self.assertEqual(ProposalStatus.PUBLISHED, approved.status)


class ExactDiffTests(EngineTestCase):
    @staticmethod
    def npc_text(entity_id: str, name: str) -> str:
        from warden_drydock.core.generator import DATA

        replacement = (DATA / "adapters" / "mothership" / "templates" / "npc.md").read_text(encoding="utf-8")
        replacement = replacement.replace('id: ""', f"id: {entity_id}", 1)
        replacement = replacement.replace("ownership: shared", "ownership: campaign", 1)
        replacement = replacement.replace('name: ""', f'name: "{name}"', 1)
        return replacement.replace("# Name", f"# {name}", 1)

    def test_exact_diff_create_and_delete_use_adapter_selected_targets(self) -> None:
        replacement = self.npc_text("npc-created", "Created NPC")
        creation = ExactTextChange(
            "change_create",
            "npc-created",
            None,
            replacement,
            ChangeKind.CREATE,
            "npc",
        )
        created = self.engine.stage_exact_diff(
            StageExactDiffRequest(
                "command_create",
                self.handle,
                exact_diff_digest((creation,)),
                (creation,),
            )
        )
        self.assertEqual(Status.STAGED, created.status)
        created_record = self.engine.retrieve(
            RetrievalRequest(
                "command_show",
                created.staged_handle,
                RetrievalKind.SHOW,
                "npc-created",
            )
        ).records[0]
        self.assertEqual(replacement, created_record.content)

        deletion = ExactTextChange(
            "change_delete",
            "npc-created",
            content_digest(replacement),
            "",
            ChangeKind.DELETE,
        )
        deleted = self.engine.stage_exact_diff(
            StageExactDiffRequest(
                "command_delete",
                created.staged_handle,
                exact_diff_digest((deletion,)),
                (deletion,),
            )
        )
        self.assertEqual(Status.STAGED, deleted.status)
        missing = self.engine.retrieve(
            RetrievalRequest(
                "command_show",
                deleted.staged_handle,
                RetrievalKind.SHOW,
                "npc-created",
            )
        )
        self.assertEqual(Status.INVALID, missing.result.status)

    def assert_stage_failure_discards(
        self,
        request: StageExactDiffRequest,
        expected_status: Status,
        fail: mock._patch,
    ) -> None:
        registered_before = set(self.registry._paths)
        leaked_handle = WorkspaceHandle(f"workspace_{self.registry._counter + 1:08d}")
        source_before = self.show()
        with fail:
            result = self.engine.stage_exact_diff(request)
        self.assertEqual(expected_status, result.status)
        self.assertEqual(self.handle, result.staged_handle)
        self.assertEqual(registered_before, set(self.registry._paths))
        self.assertEqual(source_before, self.show())
        leaked = self.engine.validate(
            WorkspaceRequest("command_validate", leaked_handle)
        )
        self.assertEqual("workspace_unknown", leaked.findings[0].code)

    def test_later_create_failure_discards_partial_staged_workspace(self) -> None:
        import warden_drydock.hosted.engine.facade as facade_module

        changes = tuple(
            ExactTextChange(
                f"change_create_{index}",
                f"npc-created-{index}",
                None,
                self.npc_text(f"npc-created-{index}", f"Created {index}"),
                ChangeKind.CREATE,
                "npc",
            )
            for index in (1, 2)
        )
        real_create = facade_module.create_entity
        call_count = 0

        def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise SystemExit("injected later create failure")
            return real_create(*args, **kwargs)

        self.assert_stage_failure_discards(
            StageExactDiffRequest(
                "command_multi_create",
                self.handle,
                exact_diff_digest(changes),
                changes,
            ),
            Status.INVALID,
            mock.patch.object(facade_module, "create_entity", side_effect=fail_second),
        )

    def test_oserror_after_clone_discards_staged_workspace(self) -> None:
        import warden_drydock.hosted.engine.facade as facade_module

        source = self.show()
        original = source.records[0].content
        assert original is not None
        replacement = original.replace('name: "Engine Test"', 'name: "Changed"')
        change = ExactTextChange(
            "change_campaign",
            "campaign-main",
            content_digest(original),
            replacement,
        )
        self.assert_stage_failure_discards(
            StageExactDiffRequest(
                "command_stage",
                self.handle,
                exact_diff_digest((change,)),
                (change,),
            ),
            Status.FAILED,
            mock.patch.object(
                facade_module, "build_indexes", side_effect=OSError("injected")
            ),
        )
        self.assertEqual(original, self.show().records[0].content)

    def test_exact_diff_is_isolated_rebuilt_validated_and_non_publishing(self) -> None:
        source = self.show()
        original = source.records[0].content
        assert original is not None
        replacement = original.replace('name: "Engine Test"', 'name: "Staged Name"').replace(
            "# Engine Test", "# Staged Name"
        )
        change = ExactTextChange(
            "change_campaign",
            "campaign-main",
            content_digest(original),
            replacement,
        )
        request = StageExactDiffRequest(
            "command_stage",
            self.handle,
            exact_diff_digest((change,)),
            (change,),
        )
        result = self.engine.stage_exact_diff(request)

        self.assertEqual(Status.STAGED, result.status)
        self.assertNotEqual(self.handle, result.staged_handle)
        self.assertEqual(
            original,
            self.show(self.handle).records[0].content,
            "source workspace must remain byte-for-byte unchanged",
        )
        self.assertEqual(replacement, self.show(result.staged_handle).records[0].content)
        self.assertEqual(
            ("entity_index", "connection_index", "ai_context"), result.artifact_ids
        )

    def test_diff_mismatch_and_stale_content_fail_without_source_mutation(self) -> None:
        source = self.show()
        original = source.records[0].content
        assert original is not None
        change = ExactTextChange("change_campaign", "campaign-main", "0" * 64, original)
        bad_diff = self.engine.stage_exact_diff(
            StageExactDiffRequest("command_stage", self.handle, "f" * 64, (change,))
        )
        self.assertEqual(Status.INVALID, bad_diff.status)
        self.assertEqual("diff_digest_mismatch", bad_diff.findings[0].code)

        stale = self.engine.stage_exact_diff(
            StageExactDiffRequest(
                "command_stage",
                self.handle,
                exact_diff_digest((change,)),
                (change,),
            )
        )
        self.assertEqual(Status.INVALID, stale.status)
        self.assertEqual("content_digest_mismatch", stale.findings[0].code)
        self.assertEqual(original, self.show().records[0].content)

    def test_invalid_staged_candidate_returns_typed_findings(self) -> None:
        source = self.show()
        original = source.records[0].content
        assert original is not None
        replacement = original.replace("status: draft", "status: impossible")
        change = ExactTextChange(
            "change_invalid",
            "campaign-main",
            content_digest(original),
            replacement,
        )
        result = self.engine.stage_exact_diff(
            StageExactDiffRequest(
                "command_stage",
                self.handle,
                exact_diff_digest((change,)),
                (change,),
            )
        )
        self.assertEqual(Status.INVALID, result.status)
        self.assertTrue(result.findings)
        self.assertTrue(all(finding.stage is Stage.STAGE for finding in result.findings))
        self.assertTrue(any(finding.severity is Severity.ERROR for finding in result.findings))
        self.assertTrue(all(finding.subject_id == result.staged_handle.value for finding in result.findings))


class ParityTests(EngineTestCase):
    @staticmethod
    def connected_text(template: str, entity_id: str, name: str) -> str:
        from warden_drydock.core.generator import DATA

        text = (DATA / "adapters" / "mothership" / "templates" / template).read_text(encoding="utf-8")
        text = text.replace('id: ""', f"id: {entity_id}", 1)
        text = text.replace("ownership: shared", "ownership: campaign", 1)
        text = text.replace('name: ""', f'name: "{name}"', 1)
        text = text.replace("# Name", f"# {name}", 1)
        connection = "- `affected-by` -> [[campaign-main|Engine Test]] (`current`) \u2014 Parity fixture."
        lines = [
            connection if line.startswith("<!-- - `relationship`") else line
            for line in text.splitlines()
        ]
        return "\n".join(lines) + "\n"

    def connected_baseline(self) -> WorkspaceHandle:
        changes = (
            ExactTextChange(
                "parity_create_npc",
                "npc-parity",
                None,
                self.connected_text("npc.md", "npc-parity", "Parity NPC"),
                ChangeKind.CREATE,
                "npc",
            ),
            ExactTextChange(
                "parity_create_debrief",
                "debrief-parity",
                None,
                self.connected_text(
                    "debrief.md", "debrief-parity", "Parity Debrief"
                ),
                ChangeKind.CREATE,
                "debrief",
            ),
        )
        result = self.engine.stage_exact_diff(
            StageExactDiffRequest(
                "parity_fixture",
                self.handle,
                exact_diff_digest(changes),
                changes,
            )
        )
        self.assertEqual(Status.STAGED, result.status)
        return result.staged_handle

    def test_framework_cli_and_generated_standalone_share_operation_output(self) -> None:
        harness = ParityHarness(self.registry)
        self.assertTrue(harness.generated_standalone_is_synchronized(self.handle))
        for operation in (ParityOperation.INDEX, ParityOperation.CONTEXT, ParityOperation.VALIDATE):
            with self.subTest(operation=operation):
                case = ParityCase(operation)
                self.assertTrue(
                    harness.compare(self.engine, self.handle, case).matches
                )

    def test_all_retrieval_operations_have_path_free_three_way_parity(self) -> None:
        harness = ParityHarness(self.registry)
        baseline = self.connected_baseline()
        cases = (
            ParityCase(ParityOperation.FIND, "campaign"),
            ParityCase(ParityOperation.SHOW, "campaign-main"),
            ParityCase(ParityOperation.RELATED, "npc-parity", 1),
            ParityCase(ParityOperation.BACKLINKS, "campaign-main"),
            ParityCase(ParityOperation.HISTORY, "campaign-main"),
        )
        for case in cases:
            with self.subTest(operation=case.operation):
                report = harness.compare(self.engine, baseline, case)
                self.assertTrue(report.matches, report)
                for row in report.engine_semantics:
                    self.assertFalse(any("/" in value or "\\" in value for value in row))

    def test_facade_related_preserves_focus_first_core_order(self) -> None:
        baseline = self.connected_baseline()
        result = self.engine.retrieve(
            RetrievalRequest(
                "parity_related_order",
                baseline,
                RetrievalKind.RELATED,
                "npc-parity",
                1,
            )
        )
        self.assertEqual(
            ("npc-parity", "campaign-main"),
            tuple(record.subject_id for record in result.records),
        )

    def test_parity_detects_facade_divergence_and_exception(self) -> None:
        harness = ParityHarness(self.registry)
        original = self.engine.context

        def divergent(request):
            return replace(original(request), result_digest="0" * 64)

        with mock.patch.object(self.engine, "context", side_effect=divergent):
            report = harness.compare(
                self.engine, self.handle, ParityOperation.CONTEXT
            )
        self.assertFalse(report.matches)
        self.assertFalse(report.engine_raised)

        with mock.patch.object(
            self.engine, "context", side_effect=RuntimeError("injected")
        ):
            report = harness.compare(
                self.engine, self.handle, ParityOperation.CONTEXT
            )
        self.assertFalse(report.matches)
        self.assertTrue(report.engine_raised)

    def test_validation_parity_detects_missing_facade_findings(self) -> None:
        source = self.show()
        original = source.records[0].content
        assert original is not None
        replacement = original.replace("status: draft", "status: impossible")
        change = ExactTextChange(
            "parity_invalid",
            "campaign-main",
            content_digest(original),
            replacement,
        )
        staged = self.engine.stage_exact_diff(
            StageExactDiffRequest(
                "parity_invalid_stage",
                self.handle,
                exact_diff_digest((change,)),
                (change,),
            )
        )
        self.assertEqual(Status.INVALID, staged.status)
        harness = ParityHarness(self.registry)
        case = ParityCase(ParityOperation.VALIDATE)
        self.assertTrue(harness.compare(self.engine, staged.staged_handle, case).matches)
        original_validate = self.engine.validate

        def missing_findings(request):
            return replace(original_validate(request), findings=())

        with mock.patch.object(
            self.engine, "validate", side_effect=missing_findings
        ):
            report = harness.compare(self.engine, staged.staged_handle, case)
        self.assertFalse(report.matches)
        self.assertTrue(report.cli_semantics)

        def changed_findings(request):
            result = original_validate(request)
            return replace(
                result,
                findings=tuple(
                    replace(finding, code="changed_validation")
                    for finding in result.findings
                ),
            )

        with mock.patch.object(
            self.engine, "validate", side_effect=changed_findings
        ):
            report = harness.compare(self.engine, staged.staged_handle, case)
        self.assertFalse(report.matches)

    def test_retrieval_parity_detects_divergence_and_exception(self) -> None:
        harness = ParityHarness(self.registry)
        case = ParityCase(ParityOperation.SHOW, "campaign-main")
        original_retrieve = self.engine.retrieve

        def divergent(request):
            return replace(original_retrieve(request), records=())

        with mock.patch.object(self.engine, "retrieve", side_effect=divergent):
            report = harness.compare(self.engine, self.handle, case)
        self.assertFalse(report.matches)
        self.assertFalse(report.engine_raised)

        with mock.patch.object(
            self.engine, "retrieve", side_effect=RuntimeError("injected")
        ):
            report = harness.compare(self.engine, self.handle, case)
        self.assertFalse(report.matches)
        self.assertTrue(report.engine_raised)


if __name__ == "__main__":
    unittest.main()
