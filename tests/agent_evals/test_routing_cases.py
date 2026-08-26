import json
import re
import unittest
from collections import Counter
from pathlib import Path


DATASET_PATH = Path(__file__).with_name("routing_cases.json")
KNOWN_AGENTS = {
    "adapter_specialist",
    "architect",
    "core_implementer",
    "docs_maintainer",
    "hosted_backend_implementer",
    "product_designer",
    "product_strategist",
    "reviewer",
    "test_engineer",
    "web_frontend_implementer",
}
HOSTED_AGENTS = {
    "hosted_backend_implementer",
    "product_designer",
    "web_frontend_implementer",
}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def load_dataset():
    with DATASET_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


class RoutingCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset()
        cls.cases = cls.dataset["cases"]

    def test_dataset_envelope(self):
        self.assertEqual(
            set(self.dataset), {"schema_version", "kind", "dataset_id", "cases"}
        )
        self.assertEqual(self.dataset["schema_version"], 1)
        self.assertEqual(self.dataset["kind"], "routing_cases")
        self.assertTrue(ID_PATTERN.fullmatch(self.dataset["dataset_id"]))
        self.assertTrue(self.cases)

    def test_case_shape_and_values(self):
        for case in self.cases:
            with self.subTest(case_id=case.get("case_id")):
                self.assertEqual(
                    set(case), {"case_id", "prompt", "expected", "metadata"}
                )
                self.assertTrue(ID_PATTERN.fullmatch(case["case_id"]))
                self.assertTrue(case["prompt"].strip())

                expected = case["expected"]
                self.assertEqual(
                    set(expected),
                    {
                        "delegation",
                        "valid_agents",
                        "forbidden_agents",
                        "sequence",
                        "rationale",
                    },
                )
                self.assertIn(
                    expected["delegation"], {"required", "optional", "forbidden"}
                )
                self.assertTrue(expected["rationale"].strip())
                self.assertEqual(expected["valid_agents"], sorted(expected["valid_agents"]))
                self.assertEqual(
                    expected["forbidden_agents"], sorted(expected["forbidden_agents"])
                )
                self.assertEqual(len(expected["valid_agents"]), len(set(expected["valid_agents"])))
                self.assertEqual(
                    len(expected["forbidden_agents"]),
                    len(set(expected["forbidden_agents"])),
                )
                self.assertLessEqual(set(expected["valid_agents"]), KNOWN_AGENTS)
                self.assertLessEqual(set(expected["forbidden_agents"]), KNOWN_AGENTS)

                metadata = case["metadata"]
                self.assertLessEqual(set(metadata), {"source", "tags", "notes"})
                self.assertIn(metadata["source"], {"synthetic", "sanitized_repository_example"})
                tags = metadata.get("tags", [])
                self.assertEqual(tags, sorted(tags))
                self.assertEqual(len(tags), len(set(tags)))
                self.assertTrue(all(TAG_PATTERN.fullmatch(tag) for tag in tags))

    def test_case_ids_are_unique_and_sorted(self):
        case_ids = [case["case_id"] for case in self.cases]
        self.assertEqual(case_ids, sorted(case_ids))
        self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_cross_field_invariants(self):
        for case in self.cases:
            expected = case["expected"]
            valid = set(expected["valid_agents"])
            forbidden = set(expected["forbidden_agents"])
            sequence = expected["sequence"]
            with self.subTest(case_id=case["case_id"]):
                self.assertTrue(valid.isdisjoint(forbidden))
                if expected["delegation"] == "required":
                    self.assertTrue(valid)
                if expected["delegation"] == "forbidden":
                    self.assertFalse(valid)
                    self.assertIsNone(sequence)
                if sequence is not None:
                    self.assertTrue(sequence)
                    self.assertLessEqual(set(sequence), valid)

    def test_required_coverage(self):
        positive_counts = Counter()
        forbidden_counts = Counter()
        for case in self.cases:
            positive_counts.update(case["expected"]["valid_agents"])
            forbidden_counts.update(case["expected"]["forbidden_agents"])

        self.assertEqual(set(positive_counts), KNOWN_AGENTS)
        self.assertEqual(set(forbidden_counts), KNOWN_AGENTS)
        for agent in KNOWN_AGENTS:
            with self.subTest(agent=agent):
                self.assertGreaterEqual(positive_counts[agent], 2)
                self.assertGreaterEqual(forbidden_counts[agent], 2)

    def test_parent_only_and_multistage_counts(self):
        parent_only = [
            case
            for case in self.cases
            if case["expected"]["delegation"] == "forbidden"
        ]
        multistage = [
            case for case in self.cases if case["expected"]["sequence"] is not None
        ]
        self.assertGreaterEqual(len(parent_only), 3)
        self.assertGreaterEqual(len(multistage), 3)

    def test_hosted_roles_have_positive_and_boundary_cases(self):
        for agent in HOSTED_AGENTS:
            positive = [
                case
                for case in self.cases
                if agent in case["expected"]["valid_agents"]
                and "positive" in case["metadata"].get("tags", [])
            ]
            boundary = [
                case
                for case in self.cases
                if agent in case["expected"]["forbidden_agents"]
                and "boundary" in case["metadata"].get("tags", [])
            ]
            with self.subTest(agent=agent):
                self.assertTrue(positive)
                self.assertTrue(boundary)

    def test_hosted_multistage_sequence_preserves_role_order(self):
        case = next(
            case
            for case in self.cases
            if case["case_id"] == "routing-sequence-004"
        )
        self.assertEqual(
            [
                "product_strategist",
                "product_designer",
                "architect",
                "hosted_backend_implementer",
                "web_frontend_implementer",
                "reviewer",
            ],
            case["expected"]["sequence"],
        )

    def test_fixture_does_not_claim_live_routing_results(self):
        serialized = json.dumps(self.dataset).lower()
        for unsupported_field in ("actual_agent", "selected_agent", "routing_api"):
            self.assertNotIn(unsupported_field, serialized)


if __name__ == "__main__":
    unittest.main()
