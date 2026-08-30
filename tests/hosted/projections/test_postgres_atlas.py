from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
import uuid

from warden_drydock.hosted.projections.atlas_models import ApprovedHistoryQuery
from warden_drydock.hosted.projections.atlas_rebuild import AtlasProjectionRebuilder
from warden_drydock.hosted.projections.atlas_repository import (
    AtlasQueryService,
    PostgresAtlasProjectionRepository,
)
from warden_drydock.hosted.ai.models import (
    Action,
    GenerationRecord,
    GenerationRequest,
    SourceEnvelope,
    SourceExcerpt,
)
from warden_drydock.hosted.ai.repository import PostgresAIRepository
from warden_drydock.hosted.revisions import (
    FileSnapshotStore,
    InMemoryWorkflowRepository,
    PublicationIntent,
    PublicationKind,
    RevisionService,
    canonicalize_tree,
)


DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None
    sql = None


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL Atlas test is opt-in")
class PostgresAtlasIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suffix = uuid.uuid4().hex[:16]
        self.campaign_id = "campaign_" + self.suffix
        self.connect = lambda: psycopg.connect(DATABASE_URL)
        self.pg = PostgresAtlasProjectionRepository(self.connect)
        self.generation_id = "generation_" + self.suffix
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / ".drydock.json").write_text(
            '{"campaign_name":"PostgreSQL Atlas","adapter":"mothership"}\n',
            encoding="utf-8",
        )
        self.store = FileSnapshotStore(root / "store")
        self.workflow = InMemoryWorkflowRepository()
        self.revisions = RevisionService(self.store, self.workflow)

    def tearDown(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hosted_ai_stream_event WHERE generation_id=%s",
                (self.generation_id,),
            )
            cursor.execute(
                "DELETE FROM hosted_ai_generation WHERE generation_id=%s",
                (self.generation_id,),
            )
            cursor.execute(
                "DELETE FROM hosted_atlas_projection_checkpoint WHERE campaign_id=%s",
                (self.campaign_id,),
            )

    def write_record(self, record_id: str, status: str, summary: str) -> None:
        (self.source / f"{record_id}.md").write_text(
            f"---\nid: {record_id}\ntype: npc\nname: {record_id}\nstatus: {status}\n---\n\n"
            f"# Record\n\n## Summary\n\n{summary}\n\n## Connections\n\n<!-- None. -->\n",
            encoding="utf-8",
        )

    def publish(self, revision: str, ordinal: int, parent: str | None):
        intent = PublicationIntent(
            f"intent_{self.suffix}_{ordinal}",
            f"token_{self.suffix}_{ordinal}",
            PublicationKind.CREATION if ordinal == 1 else PublicationKind.APPROVAL,
            self.campaign_id,
            revision,
            parent,
            ordinal,
            canonicalize_tree(self.source)[1],
            str(ordinal) * 64,
        )
        return self.revisions.publish(
            self.source,
            intent,
            framework_version="0.3.0",
            adapter_version="1.0.0",
            validation_contract_digest="f" * 64,
        )

    def test_historical_readback_restart_and_transaction_rollback(self) -> None:
        self.write_record("record-one", "draft", "First revision.")
        first = self.publish("revision_one", 1, None)
        self.write_record("record-one", "canon", "Second revision.")
        second = self.publish("revision_two", 2, first.revision_id)
        rebuilder = AtlasProjectionRebuilder(self.store, self.pg, self.workflow)
        first_bundle = rebuilder.rebuild(first)
        second_bundle = rebuilder.rebuild(second)

        restarted = PostgresAtlasProjectionRepository(self.connect)
        self.assertEqual(first_bundle, restarted.get(self.campaign_id, "revision_one"))
        self.assertEqual(second_bundle, restarted.get(self.campaign_id, "revision_two"))
        self.assertEqual(
            (1, 2),
            tuple(
                item.ordinal
                for item in AtlasQueryService(restarted).approved_history(
                    ApprovedHistoryQuery(
                        self.campaign_id, "revision_two", second.tree_digest
                    )
                ).entries
            ),
        )

        function_name = "drydock_atlas_fail_" + self.suffix
        trigger_name = "drydock_atlas_trigger_" + self.suffix
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS "
                    "$$ BEGIN RAISE EXCEPTION 'forced Atlas rollback'; END $$"
                ).format(sql.Identifier(function_name))
            )
            cursor.execute(
                sql.SQL(
                    "CREATE TRIGGER {} BEFORE INSERT ON hosted_atlas_record "
                    "FOR EACH ROW WHEN (NEW.campaign_id = {}) EXECUTE FUNCTION {}()"
                ).format(
                    sql.Identifier(trigger_name),
                    sql.Literal(self.campaign_id),
                    sql.Identifier(function_name),
                )
            )
        try:
            with self.assertRaisesRegex(
                psycopg.errors.RaiseException, "forced Atlas rollback"
            ):
                rebuilder.rebuild(first)
            self.assertEqual(
                first_bundle,
                PostgresAtlasProjectionRepository(self.connect).get(
                    self.campaign_id, "revision_one"
                ),
            )
        finally:
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP TRIGGER IF EXISTS {} ON hosted_atlas_record").format(
                        sql.Identifier(trigger_name)
                    )
                )
                cursor.execute(
                    sql.SQL("DROP FUNCTION IF EXISTS {}()").format(
                        sql.Identifier(function_name)
                    )
                )

    def test_generation_focus_survives_repository_restart(self) -> None:
        envelope = SourceEnvelope(
            self.campaign_id,
            "revision_one",
            (SourceExcerpt("record-one", "preparation", "Synthetic.", 1),),
        )
        request = GenerationRequest(
            self.generation_id,
            self.campaign_id,
            "revision_one",
            Action.ASK,
            "Synthetic prompt",
            envelope,
            "record-one",
            "a" * 64,
        )
        self.assertTrue(
            PostgresAIRepository(self.connect).reserve_generation(
                GenerationRecord(request)
            )
        )
        restarted = PostgresAIRepository(self.connect).get_generation(
            self.generation_id
        )
        self.assertEqual(
            ("record-one", "a" * 64),
            (
                restarted.request.focus_record_id,
                restarted.request.focus_content_digest,
            ),
        )


if __name__ == "__main__":
    unittest.main()
