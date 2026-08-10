import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("agent_eval_benchmark", HERE / "benchmark.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def unavailable():
    return {"status": "unavailable", "value": None, "method": None}


def telemetry(value=None, status="unavailable", method=None):
    item = unavailable() if status == "unavailable" else {
        "status": status, "value": value, "method": method
    }
    return {
        "input_tokens": copy.deepcopy(item),
        "output_tokens": copy.deepcopy(item),
        "duration_ms": copy.deepcopy(item),
        "correction_rounds": copy.deepcopy(item),
    }


def report(case_id="routing-core-001", quality="pass", value=100):
    return {
        "schema_version": 1,
        "kind": "benchmark_report",
        "report_id": "benchmark-before",
        "dataset_refs": ["routing-v1"],
        "environment": {
            "model": None,
            "reasoning_effort": None,
            "configuration_ref": ".codex/agents@working-tree-before",
            "recorded_at": "2026-08-10T12:00:00+02:00",
        },
        "runs": [{
            "case_id": case_id,
            "attempt": 1,
            "outcome": "pass",
            "quality": {"routing": quality, "handoff": "not_applicable", "task": quality},
            "telemetry": telemetry(value, "estimated", "o200k_base via tiktoken 0.13.0"),
            "notes": None,
        }],
    }


class BenchmarkValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = benchmark.load_dataset_index()

    def assertInvalid(self, value, fragment):
        with self.assertRaisesRegex(benchmark.ValidationError, fragment):
            benchmark.validate_report(value, self.index)

    def test_valid_report(self):
        self.assertEqual("benchmark-before", benchmark.validate_report(report(), self.index)["report_id"])

    def test_configuration_reference_is_required_without_clean_tree_assumption(self):
        value = report()
        value["environment"]["configuration_ref"] = ""
        self.assertInvalid(value, "configuration_ref")

    def test_recorded_at_requires_timezone(self):
        value = report()
        value["environment"]["recorded_at"] = "2026-08-10T12:00:00"
        self.assertInvalid(value, "UTC offset")

    def test_case_must_exist_in_referenced_dataset(self):
        value = report("handoff-core-success")
        self.assertInvalid(value, "referenced dataset")

    def test_duplicate_case_and_attempt_is_rejected(self):
        value = report()
        value["runs"].append(copy.deepcopy(value["runs"][0]))
        self.assertInvalid(value, "duplicate run key")

    def test_not_run_requires_all_quality_not_evaluated(self):
        value = report()
        value["runs"][0]["outcome"] = "not_run"
        self.assertInvalid(value, "not_run requires")

    def test_unavailable_telemetry_cannot_have_a_value(self):
        value = report()
        value["runs"][0]["telemetry"]["input_tokens"] = {
            "status": "unavailable", "value": 0, "method": None
        }
        self.assertInvalid(value, "unavailable telemetry")

    def test_numeric_telemetry_requires_method(self):
        value = report()
        value["runs"][0]["telemetry"]["input_tokens"]["method"] = ""
        self.assertInvalid(value, "requires a non-negative")

    def test_unknown_aggregate_score_is_rejected(self):
        value = report()
        value["aggregate_score"] = 0.9
        self.assertInvalid(value, "unexpected")

    def test_cli_validation_returns_nonzero_for_invalid_report(self):
        value = report()
        value["runs"][0]["case_id"] = "unknown-case"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(2, benchmark.main(["--validate-only", str(path)]))


class BenchmarkComparisonTests(unittest.TestCase):
    def test_quality_and_comparable_telemetry_are_separate(self):
        before = report(quality="pass", value=100)
        after = report(quality="fail", value=80)
        after["report_id"] = "benchmark-after"
        result = benchmark.compare_reports(before, after)
        self.assertEqual(
            [{"before": "pass", "after": "fail", "count": 1}],
            result["quality_transitions"]["routing"],
        )
        self.assertEqual(-20, result["telemetry"]["input_tokens"]["comparable"][0]["delta"])
        self.assertNotIn("score", result)

    def test_unavailable_is_not_converted_to_zero(self):
        before = report()
        after = report()
        after["runs"][0]["telemetry"]["input_tokens"] = unavailable()
        result = benchmark.compare_reports(before, after)
        self.assertEqual([], result["telemetry"]["input_tokens"]["comparable"])
        item = result["telemetry"]["input_tokens"]["not_comparable"][0]
        self.assertIsNone(item["after"]["value"])

    def test_different_methods_are_not_comparable(self):
        before = report()
        after = report()
        after["runs"][0]["telemetry"]["duration_ms"]["method"] = "wall clock"
        result = benchmark.compare_reports(before, after)
        self.assertEqual([], result["telemetry"]["duration_ms"]["comparable"])

    def test_case_and_attempt_mismatches_are_explicit(self):
        before = report("routing-core-001")
        after = report("routing-core-002")
        result = benchmark.compare_reports(before, after)
        self.assertFalse(result["case_sets_match"])
        self.assertEqual(["routing-core-001"], result["missing_in_after"])
        self.assertEqual(["routing-core-002"], result["missing_in_before"])


if __name__ == "__main__":
    unittest.main()
