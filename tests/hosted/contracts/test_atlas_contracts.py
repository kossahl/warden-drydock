from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import unittest
from urllib.parse import quote, unquote_to_bytes

from jsonschema import Draft202012Validator

from warden_drydock.hosted.projections.atlas_models import edge_id


ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "docs" / "contracts" / "hosted" / "http" / "atlas" / "v1"

_PUBLIC_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_DOMAIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_STATUS = {"idea", "draft", "review", "canon", "revealed", "archived", "accepted", "missing", "unknown"}
_AUTHORITY = {"preparation", "canon", "revealed"}


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


def _value_at_path(instance: dict, path: str) -> object:
    value = instance
    for part in path.split("."):
        value = value[part]
    return value


def _strict_decode(value: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise AtlasSemanticError("unsafe_binding", "malformed percent encoding")
    try:
        return unquote_to_bytes(value.replace("+", " ")).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise AtlasSemanticError("unsafe_binding", "malformed UTF-8 query encoding") from exc


def _parse_query(route: dict, raw_query: str, cursor_binding: dict | None = None) -> dict:
    specifications = {item["name"]: item for item in route["query_parameters"]}
    values: dict[str, list[str]] = {}
    if raw_query:
        for pair in raw_query.split("&"):
            if "=" not in pair:
                raise AtlasSemanticError("unsafe_binding", "query pair has no value")
            raw_name, raw_value = pair.split("=", 1)
            name, value = _strict_decode(raw_name), _strict_decode(raw_value)
            if name not in specifications:
                raise AtlasSemanticError("unsafe_binding", "unknown query parameter")
            values.setdefault(name, []).append(value)

    result = {}
    for name, specification in specifications.items():
        supplied = values.get(name, [])
        if specification["cardinality"] == "singleton" and len(supplied) > 1:
            raise AtlasSemanticError("unsafe_binding", "duplicate singleton parameter")
        if specification["required"] and (not supplied or supplied == [""]):
            raise AtlasSemanticError("unsafe_binding", "required query parameter is empty")
        if not supplied:
            if "default" in specification:
                result[name] = specification["default"]
            continue
        if specification["cardinality"] == "repeatable":
            if any("," in item for item in supplied):
                raise AtlasSemanticError("unsafe_binding", "comma-packed filter is invalid")
            parsed = [_parse_query_value(specification["type"], item) for item in supplied]
            result[name] = sorted(set(parsed))
        else:
            result[name] = _parse_query_value(specification["type"], supplied[0])

    if cursor_binding is not None:
        current = {key: value for key, value in result.items() if key != "cursor"}
        if current != cursor_binding:
            raise AtlasSemanticError("invalid_cursor_binding", "cursor query binding changed")
    return result


def _parse_query_value(value_type: str, value: str):
    if value_type in {"integer_1_or_greater", "integer_1_through_100", "constant_1"}:
        if not value.isascii() or not value.isdigit() or value.startswith("0"):
            raise AtlasSemanticError("unsafe_binding", "invalid integer query parameter")
        parsed = int(value)
        if parsed < 1 or value_type == "constant_1" and parsed != 1 or value_type == "integer_1_through_100" and parsed > 100:
            raise AtlasSemanticError("unsafe_binding", "integer query parameter is out of range")
        return parsed
    if value_type == "public_id" and not _PUBLIC_ID.fullmatch(value):
        raise AtlasSemanticError("unsafe_binding", "invalid public identifier")
    if value_type == "domain_id" and not _DOMAIN_ID.fullmatch(value):
        raise AtlasSemanticError("unsafe_binding", "invalid domain identifier")
    if value_type == "sha256" and not _DIGEST.fullmatch(value):
        raise AtlasSemanticError("unsafe_binding", "invalid digest")
    if value_type == "authority" and value not in _AUTHORITY:
        raise AtlasSemanticError("unsafe_binding", "invalid authority")
    if value_type == "raw_status_filter" and value not in _STATUS:
        raise AtlasSemanticError("unsafe_binding", "invalid status")
    if value_type == "history_direction" and value not in {"forward", "backward"}:
        raise AtlasSemanticError("unsafe_binding", "invalid history direction")
    if value_type == "generation_action" and value not in {"ask", "check", "generate"}:
        raise AtlasSemanticError("unsafe_binding", "invalid generation action")
    if value_type == "generation_status" and value not in {"pending", "complete", "failed", "cancelled"}:
        raise AtlasSemanticError("unsafe_binding", "invalid generation status")
    if value_type == "proposal_status" and value not in {"draft", "rejected", "conflict", "published", "quarantined"}:
        raise AtlasSemanticError("unsafe_binding", "invalid proposal status")
    if value_type == "text_4000" and len(value) > 4000:
        raise AtlasSemanticError("unsafe_binding", "query text is too long")
    if value_type == "cursor" and (not value or len(value) > 4096):
        raise AtlasSemanticError("unsafe_binding", "invalid cursor")
    return value


def _serialize_query(route: dict, values: dict) -> str:
    pairs = []
    for specification in route["query_parameters"]:
        name = specification["name"]
        if name not in values or values[name] == specification.get("default", object()):
            continue
        items = values[name] if specification["cardinality"] == "repeatable" else [values[name]]
        for value in items:
            pairs.append(f"{quote(name, safe='')}={quote(str(value), safe='')}")
    return "&".join(pairs)


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
        reverse = instance["direction"] == "backward"
        if ordinals != sorted(ordinals, reverse=reverse) or len(ordinals) != len(set(ordinals)):
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
        validator = Draft202012Validator(self.schema)
        Draft202012Validator.check_schema(self.schema)
        aggregate = json.loads(
            (CONTRACT_ROOT.parents[1] / "index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            ["v2/index.json", "atlas/v2/index.json"],
            [item["index"] for item in aggregate["packages"]],
        )
        self.assertIn("does not mutate", self.index["compatibility"])
        self.assertEqual(4, len(self.index["negative_fixtures"]))
        self.assertTrue(all((CONTRACT_ROOT / item).is_file() for item in self.index["negative_fixtures"]))
        first = json.loads(
            (CONTRACT_ROOT / self.index["negative_fixtures"][0]).read_text(encoding="utf-8")
        )
        instance = first["instance"]
        categories = []
        if list(validator.iter_errors(instance)):
            categories.append("unsafe_binding")
        else:
            try:
                validate_semantics(instance)
            except AtlasSemanticError as exc:
                categories.append(exc.category)
        self.assertIn(first["expected_category"], categories)
        broken = deepcopy(instance)
        broken["contract_version"] = 2
        self.assertTrue(list(validator.iter_errors(broken)))

    def test_all_contract_objects_are_closed_positive_and_unique(self) -> None:
        validator = Draft202012Validator(self.schema)
        names = []
        by_name = {}
        for instance in self.examples:
            with self.subTest(contract=instance["contract_name"]):
                self.assertEqual([], list(validator.iter_errors(instance)))
                validate_semantics(instance)
                names.append(instance["contract_name"])
                by_name[instance["contract_name"]] = instance
        self.assertEqual(11, len(names))
        self.assertEqual(len(names), len(set(names)))

        detail = deepcopy(by_name["atlas_record_detail"])
        detail["record"]["content"] += "\n# tampered\n"
        self.assertEqual([], list(validator.iter_errors(detail)))
        with self.assertRaises(AtlasSemanticError) as caught:
            validate_semantics(detail)
        self.assertEqual("content digest mismatch", str(caught.exception))

        result = deepcopy(by_name["atlas_record_library_result"])
        result["items"][0]["authority"] = "preparation"
        self.assertEqual([], list(validator.iter_errors(result)))
        with self.assertRaises(AtlasSemanticError) as caught:
            validate_semantics(result)
        self.assertEqual("status authority mismatch", str(caught.exception))

        renamed = deepcopy(by_name["atlas_record_detail"])
        renamed["contract_name"] = "atlas_made_up"
        self.assertTrue(list(validator.iter_errors(renamed)))

        routes = json.loads(
            (CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8")
        )["routes"]
        route = next(item for item in routes if item["id"] == "atlas_record_library")
        flat = {
            specification["name"]: _value_at_path(
                by_name["atlas_record_library_query"], specification["maps_to"]
            )
            for specification in route["query_parameters"]
        }
        self.assertEqual(flat, _parse_query(route, _serialize_query(route, flat)))

    def test_atlas_exposes_no_generation_http_contract(self) -> None:
        corpus = "\n".join(
            (CONTRACT_ROOT / relative).read_text(encoding="utf-8")
            for relative in ("atlas.schema.json", "routes.json", "examples.json", "semantic-invariants.json")
        )
        self.assertNotIn("atlas_contextual_generation", corpus)
        self.assertNotIn("contextual_generation", self.schema["$defs"])
        routes = json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))
        self.assertTrue(all("generation" not in item["path"] for item in routes["routes"]))

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
        by_id = {item["id"]: item for item in routes["routes"]}
        self.assertEqual(
            "atlas_depth_1_neighborhood_query", by_id["atlas_neighborhood"]["request"]
        )
        self.assertEqual(
            "atlas_approved_history_query", by_id["atlas_approved_history"]["request"]
        )

    def test_flat_query_parser_and_serializer_match_every_route(self) -> None:
        routes_document = json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))
        routes = {item["id"]: item for item in routes_document["routes"]}
        fixtures = json.loads(
            (CONTRACT_ROOT / self.index["query_serialization_fixtures"]).read_text(encoding="utf-8")
        )
        self.assertEqual(set(routes), {item["route"] for item in fixtures["positive"]})
        for fixture in fixtures["positive"]:
            route = routes[fixture["route"]]
            parsed = _parse_query(route, fixture["raw_query"])
            with self.subTest(fixture=fixture["name"]):
                self.assertEqual(fixture["expected"], parsed)
                self.assertEqual(parsed, _parse_query(route, _serialize_query(route, parsed)))

    def test_ambiguous_or_rebound_flat_queries_fail_closed(self) -> None:
        routes = {
            item["id"]: item
            for item in json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))["routes"]
        }
        fixtures = json.loads(
            (CONTRACT_ROOT / self.index["query_serialization_fixtures"]).read_text(encoding="utf-8")
        )
        for fixture in fixtures["negative"]:
            with self.subTest(fixture=fixture["name"]):
                with self.assertRaises(AtlasSemanticError) as caught:
                    _parse_query(
                        routes[fixture["route"]],
                        fixture["raw_query"],
                        fixture.get("cursor_binding"),
                    )
                self.assertEqual(fixture["expected_category"], caught.exception.category)

    def test_history_direction_and_summary_only_boundary_are_explicit(self) -> None:
        routes = {
            item["id"]: item
            for item in json.loads((CONTRACT_ROOT / "routes.json").read_text(encoding="utf-8"))["routes"]
        }
        history = {item["name"]: item for item in routes["atlas_approved_history"]["query_parameters"]}
        self.assertEqual("forward", history["direction"]["default"])
        self.assertTrue(history["limit"]["required"])
        self.assertEqual("summary_only_no_item_content", routes["atlas_workflow_summary"]["response_scope"])
        workflow = self.schema["$defs"]["workflow_summary"]
        self.assertNotIn("items", workflow["properties"])

        fixtures = json.loads(
            (CONTRACT_ROOT / self.index["query_serialization_fixtures"]).read_text(encoding="utf-8")
        )
        newest = next(item for item in fixtures["positive"] if item["name"] == "overview_newest_five_history")
        returned = sorted(newest["available_approved_ordinals"], reverse=True)[: newest["expected"]["limit"]]
        self.assertEqual(newest["expected_returned_ordinals"], returned)
        self.assertEqual(1, newest["http_page_requests"])

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
