from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[3]
HTTP_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http"
V1_ROOT = HTTP_ROOT / "v1"
V2_ROOT = HTTP_ROOT / "v2"


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _without_versions(value):
    if isinstance(value, dict):
        return {
            key: _without_versions(child)
            for key, child in value.items()
            if key not in {"contract_version", "$id", "title", "compatibility"}
        }
    if isinstance(value, list):
        return [_without_versions(child) for child in value]
    return value


class HostedHttpV2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads((V2_ROOT / "index.json").read_text(encoding="utf-8"))
        cls.schema = json.loads((V2_ROOT / cls.index["schema"]).read_text(encoding="utf-8"))
        cls.examples = json.loads((V2_ROOT / cls.index["examples"]).read_text(encoding="utf-8"))["examples"]
        cls.routes = json.loads((V2_ROOT / cls.index["routes"]).read_text(encoding="utf-8"))

    def test_active_package_index_is_explicit_and_complete(self) -> None:
        aggregate = json.loads((HTTP_ROOT / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(4, aggregate["contract_version"])
        self.assertEqual(
            ["hosted_http_v2", "hosted_atlas_http_v2", "hosted_live_http_v1"],
            [item["name"] for item in aggregate["packages"]],
        )
        self.assertEqual(
            ["v2/index.json", "atlas/v2/index.json", "live/v1/index.json"],
            [item["index"] for item in aggregate["packages"]],
        )
        self.assertIn("active", aggregate["packages_semantics"])
        self.assertNotIn("historical_packages", aggregate)
        self.assertEqual(
            {"index.json", "http.schema.json", "routes.json", "examples.json", "semantic-invariants.json"},
            {path.name for path in V2_ROOT.iterdir() if path.is_file()},
        )
        for name in ("index.json", "routes.json", "examples.json", "semantic-invariants.json"):
            document = json.loads((V2_ROOT / name).read_text(encoding="utf-8"))
            self.assertEqual(2, document["contract_version"], name)

        package_entry = aggregate["packages"][0]
        package_index_path = HTTP_ROOT / package_entry["index"]
        package_index = json.loads(package_index_path.read_text(encoding="utf-8"))
        self.assertEqual(2, package_index["contract_version"])
        package_root = package_index_path.parent
        schema = json.loads((package_root / package_index["schema"]).read_text(encoding="utf-8"))
        self.assertEqual(schema, self.schema)
        examples = json.loads((package_root / package_index["examples"]).read_text(encoding="utf-8"))["examples"]
        generation = next(item["payload"] for item in examples if item["name"] == "generation")
        self.assertEqual(2, generation["contract_version"])
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(generation)))

    def test_every_v2_example_is_closed_and_valid(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        validator = Draft202012Validator(self.schema)
        for example in self.examples:
            with self.subTest(example=example["name"]):
                self.assertEqual([], list(validator.iter_errors(example["payload"])))
                for value in _walk(example["payload"]):
                    if isinstance(value, dict) and "contract_version" in value:
                        self.assertEqual(2, value["contract_version"])

        base = deepcopy(
            next(item["payload"] for item in self.examples if item["name"] == "provider_readiness")
        )
        unexpected = deepcopy(base)
        unexpected["unexpected_property"] = True
        with self.subTest(mutation="unexpected_top_level_property"):
            self.assertTrue(list(validator.iter_errors(unexpected)))
        for version in (1, 3):
            mutated = deepcopy(base)
            mutated["contract_version"] = version
            with self.subTest(mutation="contract_version", version=version):
                self.assertTrue(list(validator.iter_errors(mutated)))

    def test_unrelated_payloads_copy_v1_semantics(self) -> None:
        v1_schema = json.loads((V1_ROOT / "http.schema.json").read_text(encoding="utf-8"))
        excluded = {"ask_start_request", "generation_view", "generation_context"}
        v1_definitions = {
            key: value for key, value in v1_schema["$defs"].items() if key not in excluded
        }
        v2_definitions = {
            key: value for key, value in self.schema["$defs"].items()
            if key not in {"generation_start_request", "generation_view", "generation_context"}
        }
        self.assertEqual(_without_versions(v1_definitions), _without_versions(v2_definitions))

    def test_general_generation_context_is_closed(self) -> None:
        validator = Draft202012Validator(self.schema)
        campaign = next(item["payload"] for item in self.examples if item["name"] == "generation_start_campaign")
        record = next(item["payload"] for item in self.examples if item["name"] == "generation_start_record_session")
        for action in ("ask", "check", "generate"):
            valid = deepcopy(campaign)
            valid["action"] = action
            self.assertEqual([], list(validator.iter_errors(valid)))

        invalid = []
        missing_digest = deepcopy(record)
        del missing_digest["context"]["content_digest"]
        invalid.append(missing_digest)
        campaign_with_record = deepcopy(campaign)
        campaign_with_record["context"]["record_id"] = "campaign-main"
        invalid.append(campaign_with_record)
        loose_focus = deepcopy(campaign)
        loose_focus["focus_record_id"] = "campaign-main"
        invalid.append(loose_focus)
        wrong_action = deepcopy(campaign)
        wrong_action["action"] = "publish"
        invalid.append(wrong_action)
        for payload in invalid:
            self.assertTrue(list(validator.iter_errors(payload)))

    def test_generation_routes_replace_asks_without_parallel_atlas_api(self) -> None:
        by_id = {item["id"]: item for item in self.routes["routes"]}
        self.assertEqual(
            "/campaigns/{campaign_id}/revisions/{revision_id}/generations",
            by_id["generation_start"]["path"],
        )
        self.assertEqual("/generations/{generation_id}", by_id["generation_read"]["path"])
        self.assertEqual("/generations/{generation_id}/events", by_id["generation_events"]["path"])
        route_text = json.dumps(self.routes)
        self.assertNotIn("/asks", route_text)
        self.assertNotIn("/atlas/generations", route_text)

    def test_root_proposal_routes_are_unchanged(self) -> None:
        v1_routes = json.loads((V1_ROOT / "routes.json").read_text(encoding="utf-8"))["routes"]
        v2_routes = self.routes["routes"]
        v1_proposals = {
            item["id"]: item["path"] for item in v1_routes if item["id"].startswith("proposal_")
        }
        v2_proposals = {
            item["id"]: item["path"] for item in v2_routes if item["id"].startswith("proposal_")
        }
        self.assertEqual(v1_proposals, v2_proposals)


if __name__ == "__main__":
    unittest.main()
