from __future__ import annotations

import os
import unittest
import uuid

from warden_drydock.hosted.ai.models import (
    Action,
    GenerationRecord,
    GenerationRequest,
    SourceEnvelope,
    SourceExcerpt,
)
from warden_drydock.hosted.ai.repository import PostgresAIRepository


DATABASE_URL = os.environ.get("DRYDOCK_TEST_DATABASE_URL")
try:
    import psycopg
except ImportError:  # pragma: no cover - opt-in live boundary
    psycopg = None


@unittest.skipUnless(DATABASE_URL and psycopg, "live PostgreSQL AI idempotency test is opt-in")
class PostgresAIReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.suffix = uuid.uuid4().hex[:16]
        self.generation_id = "generation_" + self.suffix
        self.campaign_id = "campaign_" + self.suffix
        self.connect = lambda: psycopg.connect(DATABASE_URL)
        self.envelope = SourceEnvelope(
            self.campaign_id,
            "revision_one",
            (SourceExcerpt("record-one", "canon", "Synthetic.", 1),),
        )

    def tearDown(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_ai_stream_event WHERE generation_id=%s", (self.generation_id,))
            cursor.execute("DELETE FROM hosted_ai_generation WHERE generation_id=%s", (self.generation_id,))

    def request(self, prompt: str) -> GenerationRequest:
        return GenerationRequest(self.generation_id, self.campaign_id, "revision_one", Action.ASK, prompt, self.envelope)

    def test_replay_matches_stored_request_digest_and_rejects_only_mismatch(self) -> None:
        original = GenerationRecord(self.request("State?"))
        self.assertTrue(PostgresAIRepository(self.connect).reserve_generation(original))

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT request_digest,source_set_digest FROM hosted_ai_generation WHERE generation_id=%s",
                (self.generation_id,),
            )
            stored_request_digest, stored_source_set_digest = cursor.fetchone()
        expected = PostgresAIRepository._request_digest(original.request)
        self.assertEqual(
            (expected, self.envelope.source_set_digest),
            (stored_request_digest, stored_source_set_digest),
        )

        restarted = PostgresAIRepository(self.connect)
        replayed = restarted.get_generation(self.generation_id)
        self.assertEqual(original.request, replayed.request)
        self.assertEqual(expected, PostgresAIRepository._request_digest(replayed.request))
        self.assertFalse(restarted.reserve_generation(GenerationRecord(self.request("State?"))))
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM hosted_ai_generation WHERE generation_id=%s", (self.generation_id,))
            self.assertEqual(1, cursor.fetchone()[0])

        with self.assertRaisesRegex(ValueError, "idempotency_digest_conflict"):
            restarted.reserve_generation(GenerationRecord(self.request("Changed")))


if __name__ == "__main__":
    unittest.main()