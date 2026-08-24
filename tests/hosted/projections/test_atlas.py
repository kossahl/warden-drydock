from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from warden_drydock.hosted.projections.atlas_models import (
    ApprovedHistoryQuery,
    Authority,
    AtlasHistoryEntry,
    HistoryChangeKind,
    NeighborhoodQuery,
    RecordLibraryQuery,
    StatusKind,
    content_digest,
    decode_cursor,
)
from warden_drydock.hosted.ai.models import Action, GenerationRequest, SourceEnvelope
from warden_drydock.hosted.projections.atlas_rebuild import AtlasProjectionRebuilder
from warden_drydock.hosted.projections.atlas_repository import (
    AtlasQueryService,
    InMemoryAtlasProjectionRepository,
)
from warden_drydock.hosted.projections.atlas_contracts import (
    approved_history_contract,
    neighborhood_contract,
    record_detail_contract,
    record_library_contract,
)
from jsonschema import Draft202012Validator
from warden_drydock.hosted.revisions import (
    FileSnapshotStore,
    InMemoryWorkflowRepository,
    PublicationIntent,
    PublicationKind,
    RevisionService,
    SnapshotIntegrityError,
    SnapshotLineageError,
    canonicalize_tree,
)


class AtlasFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / ".drydock.json").write_text(
            '{"campaign_name":"Synthetic Atlas","adapter":"mothership"}\n',
            encoding="utf-8",
        )
        self.store = FileSnapshotStore(root / "store")
        self.workflow = InMemoryWorkflowRepository()
        self.revisions = RevisionService(self.store, self.workflow)
        self.projections = InMemoryAtlasProjectionRepository()

    def write_record(
        self,
        record_id: str,
        *,
        name: str | None = None,
        status: str | None = "draft",
        summary: str | None = None,
        connections: tuple[str, ...] = (),
        line_ending: str = "\n",
    ) -> None:
        frontmatter = ["---", f"id: {record_id}", "type: npc", f'name: "{name or record_id}"']
        if status is not None:
            frontmatter.append(f"status: {status}")
        body = frontmatter + ["---", "", "# Record", "", "## Summary", "", summary or f"Summary for {record_id}.", "", "## Connections", ""]
        body.extend(connections or ("<!-- None. -->",))
        body.append("")
        (self.source / f"{record_id}.md").write_bytes(
            line_ending.join(body).encode("utf-8")
        )

    def publish(
        self,
        revision_id: str,
        ordinal: int,
        parent: str | None,
        *,
        proposal: bool = False,
    ):
        intent = PublicationIntent(
            f"intent_{ordinal}",
            f"token_{ordinal}",
            PublicationKind.APPROVAL if proposal else PublicationKind.CREATION,
            "campaign_atlas",
            revision_id,
            parent,
            ordinal,
            canonicalize_tree(self.source)[1],
            f"{ordinal:x}" * 64,
        )
        return self.revisions.publish(
            self.source,
            intent,
            framework_version="0.3.0",
            adapter_version="1.0.0",
            validation_contract_digest="f" * 64,
        )

    def two_revisions(self):
        statuses = [
            "idea",
            "draft",
            "review",
            "canon",
            "revealed",
            "archived",
            "accepted",
            "legacy-status",
            None,
        ]
        duplicate = (
            "- `knows` → [[record-002|Two]] (`current`) — First occurrence.",
            "- `knows` → [[record-002|Two]] (`current`) — Duplicate occurrence.",
        )
        for index in range(1, 61):
            self.write_record(
                f"record-{index:03d}",
                name="Äther" if index == 10 else None,
                status=statuses[(index - 1) % len(statuses)],
                connections=duplicate if index == 1 else (),
            )
        first = self.publish("revision_one", 1, None)
        (self.source / "record-003.md").unlink()
        self.write_record(
            "record-001",
            status="canon",
            summary="Changed summary.",
            connections=duplicate,
        )
        self.write_record("record-061", status="revealed")
        second = self.publish("revision_two", 2, first.revision_id, proposal=True)
        return first, second


class AtlasProjectionTests(AtlasFixture):
    def test_two_revisions_survive_head_advance_without_leakage(self) -> None:
        first, second = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        )
        first_bundle = rebuilder.rebuild(first)
        second_bundle = rebuilder.rebuild(second)
        self.assertEqual("revision_two", self.workflow.head("campaign_atlas"))
        self.assertEqual(60, len(first_bundle.records))
        self.assertEqual(60, len(second_bundle.records))
        self.assertIn("record-003", {item.record_id for item in first_bundle.records})
        self.assertNotIn("record-003", {item.record_id for item in second_bundle.records})
        self.assertNotIn("record-061", {item.record_id for item in first_bundle.records})
        self.assertIn("record-061", {item.record_id for item in second_bundle.records})
        self.assertEqual(first_bundle, self.projections.get("campaign_atlas", "revision_one"))

    def test_repeated_rebuild_is_identical_and_failure_preserves_previous(self) -> None:
        first, _ = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        )
        original = rebuilder.rebuild(first)
        self.assertEqual(original, rebuilder.rebuild(first))

        class FailingRepository(InMemoryAtlasProjectionRepository):
            def replace(self, bundle):
                raise RuntimeError("forced replacement failure")

        failing = FailingRepository()
        failing.bundles[(original.campaign_id, original.revision_id)] = original
        with self.assertRaisesRegex(RuntimeError, "forced replacement"):
            AtlasProjectionRebuilder(
                self.store, failing, self.workflow
            ).rebuild(first)
        self.assertEqual(original, failing.get(original.campaign_id, original.revision_id))

    def test_rebuild_rejects_all_unsafe_bindings_before_replacement(self) -> None:
        first, _ = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        )
        original = rebuilder.rebuild(first)

        rejected = (
            replace(first, campaign_id="campaign_other"),
            replace(first, revision_id="revision_other"),
            replace(first, tree_digest="0" * 64),
            replace(first, change_digest="0" * 64),
        )
        for manifest in rejected:
            with self.subTest(manifest=manifest):
                with self.assertRaises(
                    (FileNotFoundError, SnapshotIntegrityError, SnapshotLineageError)
                ):
                    rebuilder.rebuild(manifest)
                self.assertEqual(
                    original,
                    self.projections.get("campaign_atlas", "revision_one"),
                )

        with self.assertRaisesRegex(ValueError, "safe public identifier"):
            replace(first, campaign_id="../private")
        self.assertEqual(
            original, self.projections.get("campaign_atlas", "revision_one")
        )

        self.workflow.quarantine_intent("intent_1")
        with self.assertRaisesRegex(SnapshotLineageError, "ineligible revision"):
            rebuilder.rebuild(first)
        self.assertEqual(
            original, self.projections.get("campaign_atlas", "revision_one")
        )

    def test_empty_system_recovery_rebuilds_verified_revisions_in_order(self) -> None:
        self.two_revisions()
        recovered = InMemoryAtlasProjectionRepository()
        bundles = AtlasProjectionRebuilder(
            self.store, recovered, self.workflow
        ).rebuild_inventory("campaign_atlas")
        self.assertEqual((1, 2), tuple(item.ordinal for item in bundles))
        self.assertEqual(
            (1, 2),
            tuple(
                item.ordinal
                for item in AtlasQueryService(recovered).approved_history(
                    ApprovedHistoryQuery(
                        "campaign_atlas", "revision_two", bundles[-1].tree_digest
                    )
                ).entries
            ),
        )

    def test_status_authority_digest_edges_and_backlinks(self) -> None:
        first, second = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        )
        bundle = rebuilder.rebuild(first)
        rebuilder.rebuild(second)
        by_id = {item.record_id: item for item in bundle.records}
        self.assertEqual(Authority.CANON, by_id["record-004"].authority)
        self.assertEqual(Authority.REVEALED, by_id["record-005"].authority)
        self.assertEqual(Authority.PREPARATION, by_id["record-007"].authority)
        self.assertEqual(StatusKind.UNKNOWN, by_id["record-008"].raw_status.kind)
        self.assertEqual(StatusKind.MISSING, by_id["record-009"].raw_status.kind)
        self.assertEqual(2, len(bundle.edges))
        self.assertEqual(2, len({item.edge_id for item in bundle.edges}))
        service = AtlasQueryService(self.projections)
        neighborhood_query = NeighborhoodQuery(
            "campaign_atlas", "revision_one", bundle.tree_digest, "record-002"
        )
        neighborhood = service.neighborhood(neighborhood_query)
        self.assertEqual(
            {item.edge_id for item in bundle.edges},
            {item.edge_id for item in neighborhood.edges},
        )
        first_edge = service.neighborhood(replace(neighborhood_query, limit=1))
        self.assertIsNotNone(first_edge.next_cursor)
        second_edge = service.neighborhood(
            replace(neighborhood_query, limit=1, cursor=first_edge.next_cursor)
        )
        self.assertEqual(1, len(second_edge.edges))
        with self.assertRaisesRegex(ValueError, "invalid_cursor_binding"):
            service.neighborhood(
                replace(
                    neighborhood_query,
                    focus_record_id="record-001",
                    limit=1,
                    cursor=first_edge.next_cursor,
                )
            )

    def test_content_digest_normalizes_all_line_endings(self) -> None:
        expected = content_digest("one\ntwo\n")
        self.assertEqual(expected, content_digest("one\r\ntwo\r\n"))
        self.assertEqual(expected, content_digest("one\rtwo\r"))

    def test_search_filters_facets_ordering_and_cursor_binding(self) -> None:
        first, second = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        )
        bundle = rebuilder.rebuild(first)
        rebuilder.rebuild(second)
        service = AtlasQueryService(self.projections)
        query = RecordLibraryQuery(
            "campaign_atlas", "revision_one", bundle.tree_digest
        )
        first_page = service.record_library(query)
        self.assertEqual(60, first_page.total)
        self.assertEqual(50, len(first_page.items))
        self.assertEqual("record-001", first_page.items[0].record_id)
        second_page = service.record_library(
            replace(query, cursor=first_page.next_cursor)
        )
        self.assertEqual(10, len(second_page.items))
        self.assertEqual("record-051", second_page.items[0].record_id)
        back = service.record_library(
            replace(query, cursor=second_page.previous_cursor)
        )
        self.assertEqual(first_page.items, back.items)
        self.assertEqual(60, sum(item.count for item in first_page.type_facets))
        self.assertEqual(60, sum(item.count for item in first_page.status_facets))

        searched = service.record_library(replace(query, query="äTHER"))
        self.assertEqual(("record-010",), tuple(item.record_id for item in searched.items))
        canon = service.record_library(
            replace(query, authorities=(Authority.CANON,), statuses=("canon",))
        )
        self.assertTrue(canon.items)
        self.assertTrue(all(item.authority is Authority.CANON for item in canon.items))

        with self.assertRaisesRegex(ValueError, "invalid_cursor_binding"):
            service.record_library(
                replace(query, query="changed", cursor=first_page.next_cursor)
            )
        second_bundle = self.projections.get("campaign_atlas", "revision_two")
        with self.assertRaisesRegex(ValueError, "invalid_cursor_binding"):
            service.record_library(
                RecordLibraryQuery(
                    "campaign_atlas", "revision_two", second_bundle.tree_digest,
                    cursor=first_page.next_cursor,
                )
            )
        decoded = decode_cursor(first_page.next_cursor)
        self.assertEqual("revision_one", decoded["revision_id"])

    def test_approved_history_is_ordinal_subject_filtered_and_links_removal(self) -> None:
        first, second = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store,
            self.projections,
            self.workflow,
            proposal_provenance=lambda campaign, revision: (
                ("proposal_one", 1) if revision == "revision_two" else None
            ),
        )
        rebuilder.rebuild(first)
        second_bundle = rebuilder.rebuild(second)
        history = AtlasQueryService(self.projections).approved_history(
            ApprovedHistoryQuery(
                "campaign_atlas", "revision_two", second_bundle.tree_digest
            )
        )
        self.assertEqual((1, 2), tuple(item.ordinal for item in history.entries))
        self.assertEqual(("proposal_one", 1), (history.entries[1].proposal_id, history.entries[1].proposal_version))
        removed = AtlasQueryService(self.projections).approved_history(
            ApprovedHistoryQuery(
                "campaign_atlas", "revision_two", second_bundle.tree_digest,
                subject_record_id="record-003",
            )
        )
        self.assertEqual(2, removed.entries[-1].ordinal)
        change = removed.entries[-1].changes[0]
        self.assertEqual(HistoryChangeKind.REMOVED, change.change_kind)
        self.assertEqual("revision_one", change.link_revision_id)
        kinds = {
            item.change_kind
            for item in second_bundle.history_entry.changes
            if item.record_id == "record-001"
        }
        self.assertIn(HistoryChangeKind.AUTHORITY_TRANSITION, kinds)
        self.assertNotIn("audit", {item.change_kind.value for item in history.entries[1].changes})

    def test_approved_history_pages_more_than_one_hundred_revisions(self) -> None:
        first, _ = self.two_revisions()
        template = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        ).rebuild(first)
        repository = InMemoryAtlasProjectionRepository()
        for ordinal in range(1, 102):
            revision_id = f"revision_{ordinal:03d}"
            parent_revision_id = (
                None if ordinal == 1 else f"revision_{ordinal - 1:03d}"
            )
            tree_digest = f"{ordinal:064x}"
            history_entry = AtlasHistoryEntry(
                revision_id=revision_id,
                parent_revision_id=parent_revision_id,
                ordinal=ordinal,
                tree_digest=tree_digest,
                change_digest=f"{ordinal + 1024:064x}",
                changes=(),
            )
            repository.replace(
                replace(
                    template,
                    revision_id=revision_id,
                    parent_revision_id=parent_revision_id,
                    ordinal=ordinal,
                    tree_digest=tree_digest,
                    edges=(),
                    history_entry=history_entry,
                    projection_digest=f"{ordinal + 2048:064x}",
                )
            )
        service = AtlasQueryService(repository)
        query = ApprovedHistoryQuery(
            "campaign_atlas", "revision_101", f"{101:064x}"
        )
        first_page = service.approved_history(query)
        self.assertEqual(101, first_page.total)
        self.assertEqual(
            tuple(range(1, 51)), tuple(item.ordinal for item in first_page.entries)
        )
        second_page = service.approved_history(
            replace(query, cursor=first_page.next_cursor)
        )
        self.assertEqual(
            tuple(range(51, 101)), tuple(item.ordinal for item in second_page.entries)
        )
        final_page = service.approved_history(
            replace(query, cursor=second_page.next_cursor)
        )
        self.assertEqual((101,), tuple(item.ordinal for item in final_page.entries))
        self.assertIsNone(final_page.next_cursor)
        self.assertEqual(
            second_page.entries,
            service.approved_history(
                replace(query, cursor=final_page.previous_cursor)
            ).entries,
        )
        with self.assertRaisesRegex(ValueError, "invalid_cursor_binding"):
            service.approved_history(
                replace(query, subject_record_id="record-001", cursor=first_page.next_cursor)
            )

    def test_generation_focus_pair_is_closed_and_nullable(self) -> None:
        envelope = SourceEnvelope("campaign_atlas", "revision_one", ())
        GenerationRequest(
            "generation_one", "campaign_atlas", "revision_one", Action.ASK,
            "Synthetic prompt", envelope,
        )
        GenerationRequest(
            "generation_two", "campaign_atlas", "revision_one", Action.ASK,
            "Synthetic prompt", envelope, "record-one", "a" * 64,
        )
        with self.assertRaisesRegex(ValueError, "complete or absent"):
            GenerationRequest(
                "generation_three", "campaign_atlas", "revision_one", Action.ASK,
                "Synthetic prompt", envelope, "record-one", None,
            )

    def test_migration_is_additive_revision_keyed_and_keeps_database_internal(self) -> None:
        root = Path(__file__).resolve().parents[3]
        migration = (root / "warden_drydock" / "hosted" / "migrations" / "0006_atlas_projection.sql").read_text(encoding="utf-8")
        for table in (
            "hosted_atlas_projection_checkpoint", "hosted_atlas_record",
            "hosted_atlas_edge", "hosted_atlas_history_entry",
            "hosted_atlas_history_change",
        ):
            self.assertIn(f"CREATE TABLE {table}", migration)
        self.assertIn("PRIMARY KEY (campaign_id, revision_id)", migration)
        self.assertIn("focus_record_id", migration)
        self.assertNotIn("DROP TABLE", migration)
        compose = (root / "compose.yaml").read_text(encoding="utf-8")
        db_service = compose.split("  db:", 1)[1].split("networks:", 1)[0]
        self.assertNotIn("ports:", db_service)

    def test_contract_facing_models_validate_without_reconstructing_semantics(self) -> None:
        first, second = self.two_revisions()
        rebuilder = AtlasProjectionRebuilder(
            self.store, self.projections, self.workflow
        )
        first_bundle = rebuilder.rebuild(first)
        second_bundle = rebuilder.rebuild(second)
        service = AtlasQueryService(self.projections)
        schema_path = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "hosted" / "http" / "atlas" / "v1" / "atlas.schema.json"
        validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        query = RecordLibraryQuery(
            "campaign_atlas", "revision_one", first_bundle.tree_digest
        )
        payloads = [
            record_library_contract(
                service.record_library(query), first_bundle, second_bundle
            ),
            record_detail_contract(
                service.record_detail("campaign_atlas", "revision_one", "record-001"),
                first_bundle,
                second_bundle,
            ),
            neighborhood_contract(
                service.neighborhood(
                    NeighborhoodQuery(
                        "campaign_atlas", "revision_one", first_bundle.tree_digest,
                        "record-001",
                    )
                ),
                first_bundle,
                second_bundle,
            ),
            approved_history_contract(
                service.approved_history(
                    ApprovedHistoryQuery(
                        "campaign_atlas", "revision_two", second_bundle.tree_digest
                    )
                ),
                second_bundle,
                second_bundle,
                {"revision_one": first_bundle, "revision_two": second_bundle},
            ),
        ]
        for payload in payloads:
            with self.subTest(contract=payload["contract_name"]):
                self.assertEqual([], list(validator.iter_errors(payload)))


if __name__ == "__main__":
    unittest.main()
