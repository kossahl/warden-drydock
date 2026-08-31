from __future__ import annotations

import json
from pathlib import Path
import unittest
from copy import deepcopy

from jsonschema import Draft202012Validator, FormatChecker

from tests.hosted.contracts.test_atlas_contracts import AtlasSemanticError, _parse_query


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http" / "atlas" / "v2"


class AtlasV2ContractTests(unittest.TestCase):
    def test_active_discovery_is_exact_and_historical_packages_remain_on_disk(self) -> None:
        aggregate = json.loads((CONTRACT_ROOT.parents[1] / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(3, aggregate["contract_version"])
        self.assertEqual(
            [("hosted_http_v2", "v2/index.json"), ("hosted_atlas_http_v2", "atlas/v2/index.json")],
            [(item["name"], item["index"]) for item in aggregate["packages"]],
        )
        self.assertTrue((CONTRACT_ROOT.parent / "v1" / "index.json").is_file())

    def test_package_is_closed_schema_valid_and_contains_only_read_routes(self) -> None:
        index = json.loads((CONTRACT_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(2, index["contract_version"])
        routes = json.loads((CONTRACT_ROOT / index["routes"]).read_text(encoding="utf-8"))
        self.assertTrue(routes["implemented_by_this_package"])
        self.assertTrue(all(item["method"] == "GET" for item in routes["routes"]))
        by_id = {item["id"]: item for item in routes["routes"]}
        self.assertEqual(
            {"atlas_campaign_list", "atlas_overview", "atlas_record_library", "atlas_record_detail",
             "atlas_neighborhood", "atlas_approved_history", "atlas_workflow_summary",
             "atlas_generations", "atlas_proposals"},
            set(by_id),
        )
        self.assertEqual(
            ["revision_id", "revision_ordinal", "tree_digest", "action", "status", "record_id", "limit", "cursor"],
            [item["name"] for item in by_id["atlas_generations"]["query_parameters"]],
        )
        self.assertEqual(
            ["revision_id", "revision_ordinal", "tree_digest", "status", "record_id", "limit", "cursor"],
            [item["name"] for item in by_id["atlas_proposals"]["query_parameters"]],
        )
        schema = json.loads((CONTRACT_ROOT / index["schema"]).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        examples = json.loads((CONTRACT_ROOT / index["examples"]).read_text(encoding="utf-8"))["examples"]
        for example in examples:
            with self.subTest(contract=example["contract_name"]):
                self.assertEqual([], list(validator.iter_errors(example)))

        generation_query = next(
            item for item in examples
            if item["contract_name"] == "atlas_generation_collection_query"
        )
        wrong_generation_action = deepcopy(generation_query)
        wrong_generation_action["filters"]["actions"] = ["publish"]
        with self.subTest(mutation="generations_off_enum_action"):
            self.assertTrue(list(validator.iter_errors(wrong_generation_action)))

        proposal_query = next(
            item for item in examples
            if item["contract_name"] == "atlas_proposal_collection_query"
        )
        wrong_proposal_parameter = deepcopy(proposal_query)
        wrong_proposal_parameter["action"] = "generate"
        with self.subTest(mutation="proposals_foreign_parameter"):
            self.assertTrue(list(validator.iter_errors(wrong_proposal_parameter)))

        proposal_collection = next(
            item for item in examples
            if item["contract_name"] == "atlas_proposal_collection"
        )
        wrong_proposal_command = deepcopy(proposal_collection)
        wrong_proposal_command["items"][0]["action"] = "publish"
        with self.subTest(mutation="proposals_off_enum_command"):
            self.assertTrue(list(validator.iter_errors(wrong_proposal_command)))

    def test_workflow_contracts_exclude_content_and_bind_forward_cursors(self) -> None:
        schema = json.loads((CONTRACT_ROOT / "atlas.schema.json").read_text(encoding="utf-8"))
        encoded = json.dumps({
            "generation": schema["$defs"]["generation_item"],
            "proposal": schema["$defs"]["proposal_item"],
        })
        for forbidden in ("prompt", "excerpt", "terminal_content", "before_content", "after_content", "session_id", "audit"):
            self.assertNotIn(forbidden, encoded)
        for name in ("generation_cursor_binding", "proposal_cursor_binding"):
            properties = schema["$defs"][name]["properties"]
            self.assertEqual("forward", properties["direction"]["const"])
            for field in ("revision_id", "revision_ordinal", "tree_digest", "statuses", "record_id", "limit", "sort"):
                self.assertIn(field, properties)
        self.assertIn("actions", schema["$defs"]["generation_cursor_binding"]["properties"])
        self.assertNotIn("actions", schema["$defs"]["proposal_cursor_binding"]["properties"])
        self.assertNotIn("actions", schema["$defs"]["proposal_filters"]["properties"])

    def test_proposal_publication_status_requires_exact_revision_identity(self) -> None:
        schema = json.loads((CONTRACT_ROOT / "atlas.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        examples = json.loads((CONTRACT_ROOT / "examples.json").read_text(encoding="utf-8"))["examples"]
        collection = next(
            item for item in examples
            if item["contract_name"] == "atlas_proposal_collection"
        )
        published_without_revision = deepcopy(collection)
        published_without_revision["items"][0]["status"] = "published"
        self.assertTrue(list(validator.iter_errors(published_without_revision)))

        draft_with_revision = deepcopy(collection)
        draft_with_revision["items"][0]["published_revision_id"] = "revision_three"
        self.assertTrue(list(validator.iter_errors(draft_with_revision)))

        published = deepcopy(collection)
        published["items"][0]["status"] = "published"
        published["items"][0]["published_revision_id"] = "revision_three"
        self.assertEqual([], list(validator.iter_errors(published)))

    def test_v1_routes_are_carried_forward_exactly_and_v2_additions_are_deliberate(self) -> None:
        v1 = json.loads((CONTRACT_ROOT.parent / "v1" / "routes.json").read_text(encoding="utf-8"))
        v2 = json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual(v1["query_serialization"], v2["query_serialization"])
        self.assertEqual(v1["routes"], v2["routes"][:len(v1["routes"])])
        self.assertEqual(["atlas_generations", "atlas_proposals"], [
            item["id"] for item in v2["routes"][len(v1["routes"]):]
        ])

    def test_v2_query_serialization_fixtures_execute(self) -> None:
        routes = json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))["routes"]
        by_id = {item["id"]: item for item in routes}
        fixtures = json.loads((CONTRACT_ROOT / "query-serialization-fixtures.json").read_text(encoding="utf-8"))
        for fixture in fixtures["positive"]:
            with self.subTest(positive=fixture["name"]):
                self.assertEqual(
                    fixture["expected"],
                    _parse_query(by_id[fixture["route"]], fixture["raw_query"]),
                )
        for fixture in fixtures["negative"]:
            with self.subTest(negative=fixture["name"]), self.assertRaises(AtlasSemanticError) as caught:
                _parse_query(
                    by_id[fixture["route"]], fixture["raw_query"],
                    fixture.get("cursor_binding"),
                )
            self.assertEqual(fixture["expected_category"], caught.exception.category)


if __name__ == "__main__":
    unittest.main()
