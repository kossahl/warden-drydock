import json
import re
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
FIXTURE_PATH = HERE / "handoff_cases.json"
SCHEMA_PATH = HERE / "schema.json"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

REQUIRED_AGENTS = {
    "adapter_specialist",
    "agent_curator",
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
REQUIRED_SCENARIO_TAGS = {
    "failed_verification",
    "forbidden_sensitive_content",
    "missing_evidence",
    "missing_risk",
    "no_findings",
    "success",
    "unsupported_success",
    "user_decision",
}
PRIVACY_FORBIDDEN_CONTENT = {"campaign_canon", "raw_transcript", "secret"}
HOSTED_AGENTS = {
    "hosted_backend_implementer",
    "product_designer",
    "web_frontend_implementer",
}


def load_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class HandoffFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_json(FIXTURE_PATH)
        cls.schema = load_json(SCHEMA_PATH)
        cls.cases = cls.dataset["cases"]
        definitions = cls.schema["$defs"]
        cls.valid_agents = set(definitions["agentName"]["enum"])
        cls.valid_concepts = set(definitions["requiredConcept"]["enum"])
        cls.valid_forbidden = set(definitions["forbiddenContent"]["enum"])

    def test_dataset_envelope_matches_schema_v1_contract(self):
        self.assertEqual(1, self.dataset["schema_version"])
        self.assertEqual("handoff_cases", self.dataset["kind"])
        self.assertEqual("handoff-v1", self.dataset["dataset_id"])
        self.assertTrue(self.cases)

    def test_case_ids_are_valid_unique_and_sorted(self):
        case_ids = [case["case_id"] for case in self.cases]
        self.assertEqual(sorted(case_ids), case_ids)
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertTrue(all(ID_PATTERN.fullmatch(case_id) for case_id in case_ids))

    def test_fixture_shape_and_schema_enumerations(self):
        required_case_keys = {"case_id", "agent", "scenario", "handoff", "expected", "metadata"}
        required_expected_keys = {"verdict", "required_concepts", "forbidden_content", "rationale"}
        for case in self.cases:
            with self.subTest(case_id=case["case_id"]):
                self.assertEqual(required_case_keys, set(case))
                self.assertIn(case["agent"], self.valid_agents)
                self.assertTrue(case["scenario"].strip())
                self.assertIsInstance(case["handoff"], str)
                self.assertEqual(required_expected_keys, set(case["expected"]))
                self.assertIn(case["expected"]["verdict"], {"complete", "incomplete"})
                self.assertTrue(case["expected"]["rationale"].strip())
                concepts = case["expected"]["required_concepts"]
                forbidden = case["expected"]["forbidden_content"]
                self.assertEqual(len(concepts), len(set(concepts)))
                self.assertEqual(len(forbidden), len(set(forbidden)))
                self.assertLessEqual(set(concepts), self.valid_concepts)
                self.assertLessEqual(set(forbidden), self.valid_forbidden)
                self.assertEqual("synthetic", case["metadata"]["source"])
                tags = case["metadata"]["tags"]
                self.assertEqual(sorted(tags), tags)
                self.assertEqual(len(tags), len(set(tags)))

    def test_all_agents_and_both_verdicts_are_represented(self):
        agents = {case["agent"] for case in self.cases}
        verdicts = {case["expected"]["verdict"] for case in self.cases}
        self.assertEqual(REQUIRED_AGENTS, agents)
        self.assertEqual({"complete", "incomplete"}, verdicts)
        for agent in REQUIRED_AGENTS:
            agent_verdicts = {
                case["expected"]["verdict"]
                for case in self.cases
                if case["agent"] == agent
            }
            self.assertEqual({"complete", "incomplete"}, agent_verdicts, agent)

    def test_required_scenario_categories_are_covered(self):
        tags = {tag for case in self.cases for tag in case["metadata"]["tags"]}
        self.assertLessEqual(REQUIRED_SCENARIO_TAGS, tags)

    def test_privacy_case_declares_sensitive_content_forbidden(self):
        privacy_cases = [
            case for case in self.cases
            if "forbidden_sensitive_content" in case["metadata"]["tags"]
        ]
        self.assertTrue(privacy_cases)
        for case in privacy_cases:
            self.assertEqual("incomplete", case["expected"]["verdict"])
            self.assertTrue(
                PRIVACY_FORBIDDEN_CONTENT.intersection(case["expected"]["forbidden_content"])
            )

    def test_role_specific_concept_coverage(self):
        required_by_agent = {
            "adapter_specialist": {"generated_artifact_impact", "migration_follow_up"},
            "agent_curator": {"evidence", "next_action"},
            "architect": {"affected_invariants", "alternatives"},
            "core_implementer": {"changed_files", "verification"},
            "docs_maintainer": {"changed_files", "verification"},
            "hosted_backend_implementer": {
                "affected_invariants",
                "changed_files",
                "migration_follow_up",
                "verification",
            },
            "product_designer": {
                "affected_invariants",
                "evidence",
                "open_decision",
                "verification",
            },
            "product_strategist": {"non_goals", "open_decision"},
            "reviewer": {"file_reference", "reproduction", "severity"},
            "test_engineer": {"failure", "verification"},
            "web_frontend_implementer": {
                "changed_files",
                "open_decision",
                "verification",
            },
        }
        for agent, required in required_by_agent.items():
            represented = {
                concept
                for case in self.cases
                if case["agent"] == agent
                for concept in case["expected"]["required_concepts"]
            }
            self.assertLessEqual(required, represented, agent)

    def test_hosted_roles_cover_complete_and_incomplete_handoffs(self):
        for agent in HOSTED_AGENTS:
            cases = [case for case in self.cases if case["agent"] == agent]
            with self.subTest(agent=agent):
                self.assertEqual(2, len(cases))
                self.assertEqual(
                    {"complete", "incomplete"},
                    {case["expected"]["verdict"] for case in cases},
                )

    def test_ambiguous_hosted_packets_model_decision_required(self):
        cases = {
            case["case_id"]: case
            for case in self.cases
            if case["agent"] in HOSTED_AGENTS
        }
        complete = cases["handoff-product-designer-complete-open-decision"]
        self.assertEqual(
            "DECISION REQUIRED", complete["handoff"].splitlines()[0].strip()
        )

        incomplete_ids = {
            "handoff-hosted-backend-incomplete-assumed-tenancy",
            "handoff-product-designer-incomplete-scope-assumption",
            "handoff-web-frontend-incomplete-invented-api",
        }
        for case_id in incomplete_ids:
            case = cases[case_id]
            with self.subTest(case_id=case_id):
                self.assertEqual("incomplete", case["expected"]["verdict"])
                self.assertNotEqual(
                    "DECISION REQUIRED", case["handoff"].splitlines()[0].strip()
                )
                self.assertIn("DECISION REQUIRED", case["expected"]["rationale"])


if __name__ == "__main__":
    unittest.main()
