from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from warden_drydock.hosted.projections.atlas_models import edge_id


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http" / "atlas" / "v1"


class AtlasSemanticError(ValueError):
    def __init__(self, category: str, detail: str) -> None:
        self.category = category
        super().__init__(detail)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def validate_semantics(instance: dict) -> None:
    for value in _walk(instance):
        if not isinstance(value, dict):
            continue
        if "raw_status" in value and "authority" in value:
            status = value["raw_status"]
            expected = (
                status.get("value")
                if status.get("classification") == "known"
                and status.get("value") in {"canon", "revealed"}
                else "preparation"
            )
            if value["authority"] != expected:
                raise AtlasSemanticError("unsafe_binding", "status authority mismatch")
        if "content" in value and "content_digest" in value:
            normalized = value["content"].replace("\r\n", "\n").replace("\r", "\n")
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if digest != value["content_digest"]:
                raise AtlasSemanticError("unsafe_binding", "content digest mismatch")
    if instance["contract_name"] == "atlas_contextual_generation":
        if (instance["focus_record_id"] is None) != (
            instance["focus_content_digest"] is None
        ):
            raise AtlasSemanticError("unsafe_binding", "focus binding is incomplete")
    if instance["contract_name"] == "atlas_deterministic_cursor":
        if _canonical_digest(instance["binding"]) != instance["digest"]:
            raise AtlasSemanticError(
                "invalid_cursor_binding", "cursor digest or binding changed"
            )
    if instance["contract_name"] == "atlas_depth_1_neighborhood":
        revision_id = instance["binding"]["viewed_revision"]["revision_id"]
        seen = set()
        for edge in instance["edges"]:
            expected = edge_id(
                revision_id, edge["source_record_id"], edge["occurrence_order"]
            )
            if edge["edge_id"] != expected or edge["edge_id"] in seen:
                raise AtlasSemanticError("unsafe_binding", "edge occurrence identity mismatch")
            seen.add(edge["edge_id"])
    if instance["contract_name"] == "atlas_approved_history_collection":
        ordinals = [item["revision"]["ordinal"] for item in instance["entries"]]
        if ordinals != sorted(ordinals) or len(ordinals) != len(set(ordinals)):
            raise AtlasSemanticError("unsafe_binding", "history ordinal order mismatch")
        for entry in instance["entries"]:
            if (entry["proposal_id"] is None) != (entry["proposal_version"] is None):
                raise AtlasSemanticError("unsafe_binding", "proposal provenance incomplete")


class AtlasContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads((CONTRACT_ROOT / "index.json").read_text(encoding="utf-8"))
        cls.schema = json.loads((CONTRACT_ROOT / cls.index["schema"]).read_text(encoding="utf-8"))
        cls.examples = json.loads((CONTRACT_ROOT / cls.index["examples"]).read_text(encoding="utf-8"))["examples"]

    def test_package_is_separate_indexed_closed_and_schema_valid(self) -> None:
        Draft202012Validator.check_schema(self.schema)
        aggregate = json.loads(
            (CONTRACT_ROOT.parents[1] / "index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            ["v1/index.json", "atlas/v1/index.json"],
            [item["index"] for item in aggregate["packages"]],
        )
        self.assertIn("does not mutate", self.index["compatibility"])
        self.assertEqual(5, len(self.index["negative_fixtures"]))
        self.assertTrue(all((CONTRACT_ROOT / item).is_file() for item in self.index["negative_fixtures"]))

    def test_all_contract_objects_are_closed_positive_and_unique(self) -> None:
        validator = Draft202012Validator(self.schema)
        names = []
        for instance in self.examples:
            with self.subTest(contract=instance["contract_name"]):
                self.assertEqual([], list(validator.iter_errors(instance)))
                validate_semantics(instance)
                names.append(instance["contract_name"])
        self.assertEqual(10, len(names))
        self.assertEqual(len(names), len(set(names)))

    def test_routes_pin_all_atlas_read_destinations_without_handlers(self) -> None:
        routes = json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))
        self.assertFalse(routes["implemented_by_this_package"])
        self.assertEqual(
            {
                "atlas_campaign_list",
                "atlas_overview",
                "atlas_record_library",
                "atlas_record_detail",
                "atlas_neighborhood",
                "atlas_approved_history",
                "atlas_workflow_summary",
            },
            {item["id"] for item in routes["routes"]},
        )
        self.assertTrue(
            all("invalid_cursor_binding" in json.dumps(item) for item in routes["routes"] if item["id"] in {"atlas_record_library", "atlas_neighborhood", "atlas_approved_history"})
        )

    def test_semantic_rules_pin_algorithms_and_boundaries(self) -> None:
        invariants = json.loads(
            (CONTRACT_ROOT / "semantic-invariants.json").read_text(encoding="utf-8")
        )
        self.assertTrue(invariants["mandatory_with_json_schema"])
        algorithms = invariants["algorithms"]
        for token in ("CRLF", "bare CR", "exact returned UTF-8"):
            self.assertIn(token, algorithms["content_digest"])
        self.assertIn("accepted remains preparation", algorithms["authority"])
        self.assertIn("one-based successful parse order", algorithms["edge_occurrence"])
        self.assertIn("unpadded base64url", algorithms["cursor"])
        self.assertIn("Unicode casefold", algorithms["record_search"])
        for excluded in ("Workflow", "audit", "table-fact", "Draft", "proposal-lifecycle"):
            self.assertIn(excluded, algorithms["history"])

    def test_every_negative_fixture_fails_at_expected_category(self) -> None:
        validator = Draft202012Validator(self.schema)
        for relative in self.index["negative_fixtures"]:
            fixture = json.loads((CONTRACT_ROOT / relative).read_text(encoding="utf-8"))
            instance = fixture["instance"]
            categories = []
            if list(validator.iter_errors(instance)):
                categories.append("unsafe_binding")
            else:
                try:
                    validate_semantics(instance)
                except AtlasSemanticError as exc:
                    categories.append(exc.category)
            with self.subTest(fixture=relative):
                self.assertIn(fixture["expected_category"], categories)


if __name__ == "__main__":
    unittest.main()
