"""Validate and compare Warden Drydock agent benchmark reports."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_DATASETS = (HERE / "routing_cases.json", HERE / "handoff_cases.json")
SCHEMA_PATH = HERE / "schema.json"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
QUALITY_FIELDS = ("routing", "handoff", "task")
QUALITY_VALUES = {"pass", "fail", "not_applicable", "not_evaluated"}
TELEMETRY_FIELDS = ("input_tokens", "output_tokens", "duration_ms", "correction_rounds")
TELEMETRY_STATUSES = {"measured", "estimated", "unavailable"}
OUTCOMES = {"pass", "fail", "blocked", "not_run"}
EFFORTS = {None, "low", "medium", "high", "xhigh", "max", "ultra"}


class ValidationError(ValueError):
    """Raised when a benchmark artifact violates the v1 contract."""


def load_json(path: Path):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load {path}: {exc}") from exc


def _exact_keys(value, required, location):
    if not isinstance(value, dict):
        raise ValidationError(f"{location} must be an object")
    actual = set(value)
    expected = set(required)
    if actual != expected:
        raise ValidationError(
            f"{location} fields differ: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )


def load_dataset_index(paths=DEFAULT_DATASETS):
    """Return dataset aliases mapped to their known case IDs."""
    aliases = {}
    for path_value in paths:
        path = Path(path_value).resolve()
        dataset = load_json(path)
        if dataset.get("schema_version") != 1 or dataset.get("kind") not in {
            "routing_cases", "handoff_cases"
        }:
            raise ValidationError(f"unsupported dataset: {path}")
        cases = dataset.get("cases")
        if not isinstance(cases, list):
            raise ValidationError(f"dataset cases must be an array: {path}")
        case_ids = {case.get("case_id") for case in cases if isinstance(case, dict)}
        if None in case_ids or len(case_ids) != len(cases):
            raise ValidationError(f"dataset has missing or duplicate case IDs: {path}")
        for alias in (dataset.get("dataset_id"), path.name, str(path), path.as_posix()):
            if not isinstance(alias, str) or not alias:
                raise ValidationError(f"dataset has no usable reference: {path}")
            existing = aliases.get(alias)
            if existing is not None and existing != case_ids:
                raise ValidationError(f"ambiguous dataset reference: {alias}")
            aliases[alias] = case_ids
    return aliases


def ensure_supported_schema(path=SCHEMA_PATH):
    """Load the repository schema and reject drift from the implemented v1 shape."""
    schema = load_json(Path(path))
    definitions = schema.get("$defs", {})
    expected = {
        "benchmarkReport": {
            "schema_version", "kind", "report_id", "dataset_refs", "environment", "runs"
        },
        "benchmarkRun": {"case_id", "attempt", "outcome", "quality", "telemetry", "notes"},
    }
    for name, fields in expected.items():
        definition = definitions.get(name, {})
        if set(definition.get("required", [])) != fields or set(
            definition.get("properties", {})
        ) != fields:
            raise ValidationError(f"unsupported schema-v1 {name} shape in {path}")
    return schema


def _validate_timestamp(value, location):
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{location} must be a non-empty date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{location} is not an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{location} must include a UTC offset")


def _validate_telemetry(value, location):
    _exact_keys(value, TELEMETRY_FIELDS, location)
    for field in TELEMETRY_FIELDS:
        item = value[field]
        item_location = f"{location}.{field}"
        _exact_keys(item, ("status", "value", "method"), item_location)
        status = item["status"]
        if status not in TELEMETRY_STATUSES:
            raise ValidationError(f"{item_location}.status is invalid")
        if status == "unavailable":
            if item["value"] is not None or item["method"] is not None:
                raise ValidationError(
                    f"{item_location} unavailable telemetry requires null value and method"
                )
        elif (
            not isinstance(item["value"], int)
            or isinstance(item["value"], bool)
            or item["value"] < 0
            or not isinstance(item["method"], str)
            or not item["method"].strip()
        ):
            raise ValidationError(
                f"{item_location} measured/estimated telemetry requires a non-negative "
                "integer value and non-empty method"
            )


def validate_report(report, dataset_index=None):
    """Validate report shape and cross-field semantics; return it unchanged."""
    ensure_supported_schema()
    dataset_index = dataset_index or load_dataset_index()
    _exact_keys(
        report,
        ("schema_version", "kind", "report_id", "dataset_refs", "environment", "runs"),
        "report",
    )
    if report["schema_version"] != 1 or report["kind"] != "benchmark_report":
        raise ValidationError("report must be a schema-v1 benchmark_report")
    if not isinstance(report["report_id"], str) or not ID_PATTERN.fullmatch(report["report_id"]):
        raise ValidationError("report.report_id is invalid")
    refs = report["dataset_refs"]
    if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)):
        raise ValidationError("report.dataset_refs must be a non-empty unique array")
    referenced_cases = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            raise ValidationError("report.dataset_refs entries must be non-empty strings")
        matches = dataset_index.get(ref)
        if matches is None:
            candidate = str(Path(ref).resolve())
            matches = dataset_index.get(candidate)
        if matches is None:
            raise ValidationError(f"unknown dataset reference: {ref}")
        referenced_cases.update(matches)

    environment = report["environment"]
    _exact_keys(
        environment,
        ("model", "reasoning_effort", "configuration_ref", "recorded_at"),
        "report.environment",
    )
    if environment["model"] is not None and not isinstance(environment["model"], str):
        raise ValidationError("report.environment.model must be a string or null")
    if environment["reasoning_effort"] not in EFFORTS:
        raise ValidationError("report.environment.reasoning_effort is invalid")
    if not isinstance(environment["configuration_ref"], str) or not environment[
        "configuration_ref"
    ].strip():
        raise ValidationError("report.environment.configuration_ref must be non-empty")
    _validate_timestamp(environment["recorded_at"], "report.environment.recorded_at")

    runs = report["runs"]
    if not isinstance(runs, list):
        raise ValidationError("report.runs must be an array")
    seen = set()
    for index, run in enumerate(runs):
        location = f"report.runs[{index}]"
        _exact_keys(run, ("case_id", "attempt", "outcome", "quality", "telemetry", "notes"), location)
        case_id = run["case_id"]
        if case_id not in referenced_cases:
            raise ValidationError(f"{location}.case_id is not in a referenced dataset: {case_id}")
        attempt = run["attempt"]
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValidationError(f"{location}.attempt must be a positive integer")
        key = (case_id, attempt)
        if key in seen:
            raise ValidationError(f"duplicate run key: {case_id} attempt {attempt}")
        seen.add(key)
        if run["outcome"] not in OUTCOMES:
            raise ValidationError(f"{location}.outcome is invalid")
        _exact_keys(run["quality"], QUALITY_FIELDS, f"{location}.quality")
        if any(run["quality"][field] not in QUALITY_VALUES for field in QUALITY_FIELDS):
            raise ValidationError(f"{location}.quality contains an invalid value")
        if run["outcome"] == "not_run" and any(
            run["quality"][field] != "not_evaluated" for field in QUALITY_FIELDS
        ):
            raise ValidationError(f"{location} not_run requires all quality fields not_evaluated")
        _validate_telemetry(run["telemetry"], f"{location}.telemetry")
        if run["notes"] is not None and not isinstance(run["notes"], str):
            raise ValidationError(f"{location}.notes must be a string or null")
    return report


def compare_reports(before, after):
    """Return a deterministic, non-scored comparison of two valid reports."""
    before_runs = {(run["case_id"], run["attempt"]): run for run in before["runs"]}
    after_runs = {(run["case_id"], run["attempt"]): run for run in after["runs"]}
    before_cases = {key[0] for key in before_runs}
    after_cases = {key[0] for key in after_runs}
    missing_after = sorted(before_cases - after_cases)
    missing_before = sorted(after_cases - before_cases)
    keys_before = set(before_runs)
    keys_after = set(after_runs)
    matched_keys = sorted(keys_before & keys_after)

    quality = {}
    for field in QUALITY_FIELDS:
        transitions = Counter(
            (before_runs[key]["quality"][field], after_runs[key]["quality"][field])
            for key in matched_keys
        )
        quality[field] = [
            {"before": pair[0], "after": pair[1], "count": count}
            for pair, count in sorted(transitions.items())
        ]

    telemetry = {}
    for field in TELEMETRY_FIELDS:
        comparable = []
        incompatible = []
        for key in matched_keys:
            old = before_runs[key]["telemetry"][field]
            new = after_runs[key]["telemetry"][field]
            key_object = {"case_id": key[0], "attempt": key[1]}
            if (
                old["status"] in {"measured", "estimated"}
                and old["status"] == new["status"]
                and old["method"] == new["method"]
            ):
                comparable.append(
                    {
                        **key_object,
                        "status": old["status"],
                        "method": old["method"],
                        "before": old["value"],
                        "after": new["value"],
                        "delta": new["value"] - old["value"],
                    }
                )
            else:
                incompatible.append(
                    {
                        **key_object,
                        "before": old,
                        "after": new,
                    }
                )
        telemetry[field] = {"comparable": comparable, "not_comparable": incompatible}

    return {
        "before_report": before["report_id"],
        "after_report": after["report_id"],
        "case_sets_match": before_cases == after_cases,
        "missing_in_after": missing_after,
        "missing_in_before": missing_before,
        "run_keys_missing_in_after": [
            {"case_id": key[0], "attempt": key[1]} for key in sorted(keys_before - keys_after)
        ],
        "run_keys_missing_in_before": [
            {"case_id": key[0], "attempt": key[1]} for key in sorted(keys_after - keys_before)
        ],
        "quality_transitions": quality,
        "telemetry": telemetry,
    }


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", metavar="REPORT", type=Path)
    mode.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), type=Path)
    parser.add_argument(
        "--dataset", action="append", type=Path, default=[],
        help="dataset path; defaults to routing_cases.json and handoff_cases.json",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    try:
        index = load_dataset_index(args.dataset or DEFAULT_DATASETS)
        if args.validate_only:
            report = validate_report(load_json(args.validate_only), index)
            output = {"report_id": report["report_id"], "status": "valid"}
        else:
            before = validate_report(load_json(args.compare[0]), index)
            after = validate_report(load_json(args.compare[1]), index)
            output = compare_reports(before, after)
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
