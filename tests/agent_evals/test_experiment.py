import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
BENCHMARK_SPEC = importlib.util.spec_from_file_location("benchmark", HERE / "benchmark.py")
benchmark = importlib.util.module_from_spec(BENCHMARK_SPEC)
BENCHMARK_SPEC.loader.exec_module(benchmark)
import sys
sys.modules["benchmark"] = benchmark
EXPERIMENT_SPEC = importlib.util.spec_from_file_location("experiment", HERE / "experiment.py")
experiment = importlib.util.module_from_spec(EXPERIMENT_SPEC)
EXPERIMENT_SPEC.loader.exec_module(experiment)


STAMP = "2026-08-10T15:00:00+02:00"


def completed_pair():
    with tempfile.TemporaryDirectory() as directory:
        artifacts = experiment.create_templates(
            "effort-core", ["routing-core-001"], 1, "gpt-example",
            ".codex/agents/core-medium.toml@sha256-aaaa",
            ".codex/agents/core-high.toml@sha256-bbbb",
            "handoff-rubric-v1", STAMP, Path(directory),
        )
    manifest = artifacts["manifest.json"]
    reports = {}
    evidence = {}
    for effort in experiment.EFFORTS:
        report = artifacts[f"report-{effort}.json"]
        run = report["runs"][0]
        run["outcome"] = "pass"
        run["quality"] = {"routing": "pass", "handoff": "not_applicable", "task": "pass"}
        reports[effort] = report
        evidence[effort] = {
            "contract_version": 1,
            "kind": "agent_experiment_observations",
            "experiment_id": "effort-core",
            "effort": effort,
            "observations": [{
                "case_id": "routing-core-001",
                "attempt": 1,
                "effort": effort,
                "model": "gpt-example",
                "configuration_ref": manifest["configuration_refs"][effort],
                "requested_agents": ["core_implementer"],
                "requested_sequence": None,
                "observed_agents": ["core_implementer"],
                "judgments": copy.deepcopy(run["quality"]),
                "evaluator": {"type": "human", "identifier": "reviewer-a"},
                "rubric_version": "handoff-rubric-v1",
                "evaluated_at": STAMP,
                "evidence_ref": "sha256:" + "1" * 64,
                "verification": {"status": "pass", "reference": "eval-runtime/result-001.json"},
                "telemetry": copy.deepcopy(run["telemetry"]),
            }],
        }
    return manifest, reports, evidence


class ExperimentTests(unittest.TestCase):
    def test_pending_templates_pin_identical_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = experiment.create_templates(
                "paired-run", ["handoff-core-complete-implementation", "routing-core-001"], 2,
                "gpt-example", "config/medium@one", "config/high@two",
                "handoff-rubric-v1", STAMP, Path(directory),
            )
        manifest = artifacts["manifest.json"]
        self.assertEqual(4, len(manifest["run_keys"]))
        medium = {(r["case_id"], r["attempt"]) for r in artifacts["report-medium.json"]["runs"]}
        high = {(r["case_id"], r["attempt"]) for r in artifacts["report-high.json"]["runs"]}
        self.assertEqual(medium, high)
        self.assertTrue(manifest["fresh_context"]["required"])

    def test_valid_sanitized_pair_passes(self):
        manifest, reports, evidence = completed_pair()
        result = experiment.validate_experiment(manifest, reports, evidence)
        self.assertEqual("valid_complete_pair", result["status"])

    def test_quality_pass_without_provenance_fails(self):
        manifest, reports, evidence = completed_pair()
        evidence["medium"]["observations"] = []
        with self.assertRaisesRegex(benchmark.ValidationError, "evidence case/attempt set mismatch"):
            experiment.validate_experiment(manifest, reports, evidence)

    def test_absolute_evidence_reference_fails(self):
        manifest, reports, evidence = completed_pair()
        evidence["medium"]["observations"][0]["evidence_ref"] = "C:/Users/person/raw-output.txt"
        with self.assertRaisesRegex(benchmark.ValidationError, "repository-relative"):
            experiment.validate_experiment(manifest, reports, evidence)

    def test_raw_multiline_reference_fails(self):
        manifest, reports, evidence = completed_pair()
        evidence["medium"]["observations"][0]["verification"]["reference"] = "raw output\nsecret"
        with self.assertRaisesRegex(benchmark.ValidationError, "sanitized reference"):
            experiment.validate_experiment(manifest, reports, evidence)

    def test_paired_case_sets_must_match(self):
        manifest, reports, evidence = completed_pair()
        reports["high"]["runs"] = []
        with self.assertRaisesRegex(benchmark.ValidationError, "case/attempt set mismatch"):
            experiment.validate_experiment(manifest, reports, evidence)

    def test_model_configuration_and_rubric_mismatches_fail(self):
        for mutation, fragment in (
            (lambda m, r, e: r["high"]["environment"].update(model="other"), "model/effort"),
            (lambda m, r, e: e["medium"]["observations"][0].update(rubric_version="v2"), "provenance"),
            (lambda m, r, e: e["high"]["observations"][0].update(configuration_ref="other"), "provenance"),
        ):
            with self.subTest(fragment=fragment):
                manifest, reports, evidence = completed_pair()
                mutation(manifest, reports, evidence)
                with self.assertRaisesRegex(benchmark.ValidationError, fragment):
                    experiment.validate_experiment(manifest, reports, evidence)


if __name__ == "__main__":
    unittest.main()
