"""Create and validate controlled paired agent-evaluation experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

import benchmark


VERSION = 1
EFFORTS = ("medium", "high")
JUDGMENTS = {"pass", "fail", "not_applicable"}
EVALUATOR_TYPES = {"human", "model", "dual_review"}
SAFE_ID = re.compile(r"^[a-zA-Z0-9.][a-zA-Z0-9._/@+-]*$")
HASH_REF = re.compile(r"^sha256:[0-9a-f]{64}$")


def _timestamp(value, field):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise benchmark.ValidationError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise benchmark.ValidationError(f"{field} must include a UTC offset")


def _safe_reference(value, field):
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise benchmark.ValidationError(f"{field} must be a non-empty sanitized reference")
    if HASH_REF.fullmatch(value):
        return
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if windows.is_absolute() or posix.is_absolute() or ".." in windows.parts or ".." in posix.parts:
        raise benchmark.ValidationError(f"{field} must be repository-relative or a sha256 hash")
    if not SAFE_ID.fullmatch(value.replace("\\", "/")):
        raise benchmark.ValidationError(f"{field} contains unsafe characters")


def _load_datasets(paths=benchmark.DEFAULT_DATASETS):
    datasets = {}
    cases = {}
    for path in paths:
        data = benchmark.load_json(Path(path))
        datasets[data["dataset_id"]] = data
        for case in data["cases"]:
            cases[case["case_id"]] = (data["dataset_id"], case)
    return datasets, cases


def create_templates(
    experiment_id, case_ids, attempts, model, medium_config, high_config,
    rubric_version, created_at, output_dir, dataset_paths=benchmark.DEFAULT_DATASETS,
):
    if not benchmark.ID_PATTERN.fullmatch(experiment_id):
        raise benchmark.ValidationError("experiment_id is invalid")
    if attempts < 1:
        raise benchmark.ValidationError("attempts must be positive")
    _timestamp(created_at, "created_at")
    _safe_reference(medium_config, "medium_config")
    _safe_reference(high_config, "high_config")
    _safe_reference(rubric_version, "rubric_version")
    datasets, cases = _load_datasets(dataset_paths)
    selected = sorted(set(case_ids))
    if not selected or len(selected) != len(case_ids):
        raise benchmark.ValidationError("case IDs must be non-empty and unique")
    unknown = sorted(set(selected) - set(cases))
    if unknown:
        raise benchmark.ValidationError(f"unknown case IDs: {unknown}")
    refs = sorted({cases[case_id][0] for case_id in selected})
    run_keys = [
        {"case_id": case_id, "attempt": attempt}
        for case_id in selected for attempt in range(1, attempts + 1)
    ]
    manifest = {
        "contract_version": VERSION,
        "kind": "agent_experiment",
        "experiment_id": experiment_id,
        "dataset_refs": refs,
        "run_keys": run_keys,
        "model": model,
        "efforts": list(EFFORTS),
        "configuration_refs": {"medium": medium_config, "high": high_config},
        "fresh_context": {
            "required": True,
            "protocol": "new_context_per_case_attempt_effort",
        },
        "rubric_version": rubric_version,
        "created_at": created_at,
    }

    def make_report(effort):
        unavailable = {"status": "unavailable", "value": None, "method": None}
        return {
            "schema_version": 1,
            "kind": "benchmark_report",
            "report_id": f"{experiment_id}-{effort}",
            "dataset_refs": refs,
            "environment": {
                "model": model,
                "reasoning_effort": effort,
                "configuration_ref": manifest["configuration_refs"][effort],
                "recorded_at": created_at,
            },
            "runs": [{
                **key,
                "outcome": "not_run",
                "quality": {name: "not_evaluated" for name in benchmark.QUALITY_FIELDS},
                "telemetry": {
                    name: dict(unavailable) for name in benchmark.TELEMETRY_FIELDS
                },
                "notes": None,
            } for key in run_keys],
        }

    def make_evidence(effort):
        return {
            "contract_version": VERSION,
            "kind": "agent_experiment_observations",
            "experiment_id": experiment_id,
            "effort": effort,
            "observations": [],
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {"manifest.json": manifest}
    for effort in EFFORTS:
        artifacts[f"report-{effort}.json"] = make_report(effort)
        artifacts[f"evidence-{effort}.json"] = make_evidence(effort)
    for name, data in artifacts.items():
        (output_dir / name).write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return artifacts


def _exact(value, keys, field):
    benchmark._exact_keys(value, keys, field)


def validate_experiment(manifest, reports, evidence, dataset_paths=benchmark.DEFAULT_DATASETS):
    """Strongly validate a completed paired experiment and its provenance."""
    _exact(manifest, (
        "contract_version", "kind", "experiment_id", "dataset_refs", "run_keys", "model",
        "efforts", "configuration_refs", "fresh_context", "rubric_version", "created_at",
    ), "manifest")
    if manifest["contract_version"] != VERSION or manifest["kind"] != "agent_experiment":
        raise benchmark.ValidationError("unsupported experiment contract")
    if manifest["efforts"] != list(EFFORTS):
        raise benchmark.ValidationError("experiment efforts must be exactly medium and high")
    if not isinstance(manifest["model"], str) or not manifest["model"].strip():
        raise benchmark.ValidationError("manifest.model must be non-empty")
    _timestamp(manifest["created_at"], "manifest.created_at")
    _safe_reference(manifest["rubric_version"], "manifest.rubric_version")
    _exact(manifest["configuration_refs"], EFFORTS, "manifest.configuration_refs")
    for effort in EFFORTS:
        _safe_reference(manifest["configuration_refs"][effort], f"configuration_refs.{effort}")
    if manifest["fresh_context"] != {
        "required": True, "protocol": "new_context_per_case_attempt_effort"
    }:
        raise benchmark.ValidationError("fresh-context protocol is not pinned")
    keys = []
    for item in manifest["run_keys"]:
        _exact(item, ("case_id", "attempt"), "manifest.run_keys item")
        keys.append((item["case_id"], item["attempt"]))
    if not keys or len(keys) != len(set(keys)) or keys != sorted(keys):
        raise benchmark.ValidationError("manifest run keys must be non-empty, unique, and sorted")

    index = benchmark.load_dataset_index(dataset_paths)
    _, case_contracts = _load_datasets(dataset_paths)
    expected_keys = set(keys)
    for effort in EFFORTS:
        if effort not in reports or effort not in evidence:
            raise benchmark.ValidationError(f"missing {effort} report or evidence")
        report = benchmark.validate_report(reports[effort], index)
        env = report["environment"]
        if report["report_id"] != f"{manifest['experiment_id']}-{effort}":
            raise benchmark.ValidationError(f"{effort} report ID mismatch")
        if env["model"] != manifest["model"] or env["reasoning_effort"] != effort:
            raise benchmark.ValidationError(f"{effort} report model/effort mismatch")
        if env["configuration_ref"] != manifest["configuration_refs"][effort]:
            raise benchmark.ValidationError(f"{effort} report configuration mismatch")
        if report["dataset_refs"] != manifest["dataset_refs"]:
            raise benchmark.ValidationError(f"{effort} report dataset mismatch")
        report_runs = {(run["case_id"], run["attempt"]): run for run in report["runs"]}
        if set(report_runs) != expected_keys:
            raise benchmark.ValidationError(f"{effort} report case/attempt set mismatch")

        envelope = evidence[effort]
        _exact(envelope, ("contract_version", "kind", "experiment_id", "effort", "observations"),
               f"{effort} evidence")
        if (envelope["contract_version"] != VERSION
                or envelope["kind"] != "agent_experiment_observations"
                or envelope["experiment_id"] != manifest["experiment_id"]
                or envelope["effort"] != effort):
            raise benchmark.ValidationError(f"{effort} evidence envelope mismatch")
        observations = {}
        for position, observation in enumerate(envelope["observations"]):
            field = f"{effort} observations[{position}]"
            _exact(observation, (
                "case_id", "attempt", "effort", "model", "configuration_ref",
                "requested_agents", "requested_sequence", "observed_agents", "judgments",
                "evaluator", "rubric_version", "evaluated_at", "evidence_ref", "verification",
                "telemetry",
            ), field)
            key = (observation["case_id"], observation["attempt"])
            if key in observations:
                raise benchmark.ValidationError(f"duplicate {effort} observation: {key}")
            observations[key] = observation
            if (observation["effort"] != effort or observation["model"] != manifest["model"]
                    or observation["configuration_ref"] != manifest["configuration_refs"][effort]
                    or observation["rubric_version"] != manifest["rubric_version"]):
                raise benchmark.ValidationError(f"{field} experiment provenance mismatch")
            _timestamp(observation["evaluated_at"], f"{field}.evaluated_at")
            _safe_reference(observation["evidence_ref"], f"{field}.evidence_ref")
            for name in ("requested_agents", "observed_agents"):
                if not isinstance(observation[name], list) or any(
                    not isinstance(agent, str) or not agent for agent in observation[name]
                ):
                    raise benchmark.ValidationError(f"{field}.{name} must be a string array")
            sequence = observation["requested_sequence"]
            if sequence is not None and (not isinstance(sequence, list) or not sequence):
                raise benchmark.ValidationError(f"{field}.requested_sequence is invalid")
            case = case_contracts[observation["case_id"]][1]
            if "prompt" in case:
                expected = case["expected"]
                requested = expected["valid_agents"] if expected["delegation"] != "forbidden" else []
                if observation["requested_agents"] != requested:
                    raise benchmark.ValidationError(f"{field}.requested_agents does not match case")
                if observation["requested_sequence"] != expected["sequence"]:
                    raise benchmark.ValidationError(f"{field}.requested_sequence does not match case")
            elif observation["requested_agents"] != [case["agent"]] or sequence is not None:
                raise benchmark.ValidationError(f"{field} requested agent does not match handoff case")
            _exact(observation["judgments"], benchmark.QUALITY_FIELDS, f"{field}.judgments")
            if any(value not in JUDGMENTS for value in observation["judgments"].values()):
                raise benchmark.ValidationError(f"{field}.judgments contains an invalid value")
            _exact(observation["evaluator"], ("type", "identifier"), f"{field}.evaluator")
            if observation["evaluator"]["type"] not in EVALUATOR_TYPES:
                raise benchmark.ValidationError(f"{field}.evaluator.type is invalid")
            _safe_reference(observation["evaluator"]["identifier"], f"{field}.evaluator.identifier")
            _exact(observation["verification"], ("status", "reference"), f"{field}.verification")
            if observation["verification"]["status"] not in {"pass", "fail", "not_applicable"}:
                raise benchmark.ValidationError(f"{field}.verification.status is invalid")
            _safe_reference(observation["verification"]["reference"], f"{field}.verification.reference")
            benchmark._validate_telemetry(observation["telemetry"], f"{field}.telemetry")
        if set(observations) != expected_keys:
            raise benchmark.ValidationError(f"{effort} evidence case/attempt set mismatch")

        for key, run in report_runs.items():
            observation = observations[key]
            if run["outcome"] == "not_run":
                raise benchmark.ValidationError(f"{effort} report is incomplete at {key}")
            if run["notes"] is not None:
                raise benchmark.ValidationError(f"{effort} strong reports require null notes")
            if run["quality"] != observation["judgments"]:
                raise benchmark.ValidationError(f"{effort} quality/evidence mismatch at {key}")
            if run["telemetry"] != observation["telemetry"]:
                raise benchmark.ValidationError(f"{effort} telemetry/evidence mismatch at {key}")
            failed = "fail" in run["quality"].values()
            if run["outcome"] == "pass" and failed:
                raise benchmark.ValidationError(f"{effort} pass outcome contradicts quality at {key}")
            if run["outcome"] in {"fail", "blocked"} and not failed:
                raise benchmark.ValidationError(f"{effort} {run['outcome']} lacks failed quality at {key}")
            if run["quality"]["task"] == "pass" and observation["verification"]["status"] != "pass":
                raise benchmark.ValidationError(f"{effort} task pass lacks passed verification at {key}")
    return {"experiment_id": manifest["experiment_id"], "status": "valid_complete_pair"}


def _arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output-dir", required=True, type=Path)
    create.add_argument("--experiment-id", required=True)
    create.add_argument("--case", action="append", required=True)
    create.add_argument("--attempts", type=int, default=2)
    create.add_argument("--model", required=True)
    create.add_argument("--medium-config", required=True)
    create.add_argument("--high-config", required=True)
    create.add_argument("--rubric-version", required=True)
    create.add_argument("--created-at", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--manifest", required=True, type=Path)
    for effort in EFFORTS:
        validate.add_argument(f"--{effort}-report", required=True, type=Path)
        validate.add_argument(f"--{effort}-evidence", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = _arguments(argv)
    try:
        if args.command == "create":
            create_templates(
                args.experiment_id, args.case, args.attempts, args.model,
                args.medium_config, args.high_config, args.rubric_version,
                args.created_at, args.output_dir,
            )
            output = {"experiment_id": args.experiment_id, "status": "pending_created"}
        else:
            manifest = benchmark.load_json(args.manifest)
            reports = {
                effort: benchmark.load_json(getattr(args, f"{effort}_report")) for effort in EFFORTS
            }
            evidence = {
                effort: benchmark.load_json(getattr(args, f"{effort}_evidence")) for effort in EFFORTS
            }
            output = validate_experiment(manifest, reports, evidence)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except benchmark.ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
