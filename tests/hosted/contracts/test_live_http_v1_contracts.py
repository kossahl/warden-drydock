from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from warden_drydock.hosted.ai.models import LiveEndBarrier
from warden_drydock.hosted.ai.repository import PostgresAIRepository


ROOT = Path(__file__).resolve().parents[3]
LIVE_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http" / "live" / "v1"
HTTP_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http"


class LiveHttpV1ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads((LIVE_ROOT / "index.json").read_text(encoding="utf-8"))
        cls.schema = json.loads((LIVE_ROOT / cls.index["schema"]).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)
        cls.examples = json.loads((LIVE_ROOT / cls.index["examples"]).read_text(encoding="utf-8"))["examples"]
        cls.invariants = json.loads((LIVE_ROOT / cls.index["semantic_invariants"]).read_text(encoding="utf-8"))

    def test_package_is_registered_in_active_index(self) -> None:
        aggregate = json.loads((HTTP_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(4, aggregate["contract_version"])
        self.assertEqual(
            "live/v1/index.json",
            next(item["index"] for item in aggregate["packages"] if item["name"] == "hosted_live_http_v1"),
        )

    def test_index_documents_full_package(self) -> None:
        self.assertEqual(1, self.index["contract_version"])
        for relative in (self.index["schema"], self.index["routes"], self.index["examples"],
                         self.index["semantic_invariants"], *self.index["negative_fixtures"]):
            self.assertTrue((LIVE_ROOT / relative).is_file(), relative)

    def test_every_example_is_closed_valid_and_v2_transport(self) -> None:
        for example in self.examples:
            with self.subTest(example=example["name"]):
                self.assertEqual([], list(self.validator.iter_errors(example["payload"])))
                self.assertEqual(2, example["payload"]["contract_version"])

    def test_negative_fixtures_are_exercised(self) -> None:
        for relative in self.index["negative_fixtures"]:
            with self.subTest(fixture=relative):
                fixture = json.loads((LIVE_ROOT / relative).read_text(encoding="utf-8"))
                self.assertIn("expected_category", fixture)
                self.assertIn("expected_evidence", fixture)
                self.assertIn("expected_path", fixture)
                errors = list(self.validator.iter_errors(fixture["instance"]))
                if errors:
                    # Documented schema-violating fixture.
                    self.assertTrue(
                        fixture["expected_path"] and fixture["expected_category"],
                        f"{relative} should document the schema violation",
                    )
                else:
                    # Schema-valid fixture: its expected_category must be a documented
                    # semantic rule category.
                    rule_categories = {rule["category"] for rule in self.invariants["rules"]}
                    self.assertIn(fixture["expected_category"], rule_categories)

    def test_semantic_invariants_are_parsed_with_authority_layer_note(self) -> None:
        self.assertEqual(1, self.invariants["contract_version"])
        self.assertTrue(self.invariants["mandatory_with_json_schema"])
        self.assertIn("authority_layer_rules", self.invariants)
        self.assertIn("http_live_device_replay", self.invariants["authority_layer_rules"])
        self.assertIn("http_live_end_barrier", self.invariants["authority_layer_rules"])
        rule_ids = {rule["id"] for rule in self.invariants["rules"]}
        self.assertEqual(
            {"http_operation_digest", "http_path_body_equality", "http_live_session_binding",
             "http_live_device_replay", "http_live_end_barrier"},
            rule_ids,
        )


class PostgresAIRoundTripTests(unittest.TestCase):
    """Cover encode/decode of end_barrier and capture provenance without a live DB."""

    def setUp(self) -> None:
        # The connect factory is never invoked by the static encode/decode helpers.
        self.repo = PostgresAIRepository(lambda: None)

    def test_end_barrier_encode_decode_round_trip(self) -> None:
        barrier = LiveEndBarrier(
            end_operation_id="operation_end",
            end_device_id="device_one",
            required_operation_ids=(("device_alpha", "operation_fact"),),
            ready_for_proposal=True,
        )
        encoded = self.repo._encode_end_barrier(barrier)
        self.assertIsInstance(encoded, str)
        decoded = self.repo._decode_end_barrier(encoded)
        self.assertEqual(barrier, decoded)

    def test_end_barrier_null_encode(self) -> None:
        # A null barrier is encoded as NULL and never decoded directly; get_session
        # guards on row[7] is not None before decoding.
        self.assertIsNone(self.repo._encode_end_barrier(None))

    def test_end_barrier_empty_required_set_round_trip(self) -> None:
        barrier = LiveEndBarrier("operation_end", "device_one", (), False)
        decoded = self.repo._decode_end_barrier(self.repo._encode_end_barrier(barrier))
        self.assertEqual(barrier, decoded)


if __name__ == "__main__":
    unittest.main()
