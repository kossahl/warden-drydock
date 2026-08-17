"""Build public-safe deterministic evidence from a completed local run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .fixture import build_manifest, canonical_json, sha256
from .harness import build_schedule, score_result, validate_tool_call

BLIND_SCORES = {
    "gpt-5.6-luna": {"scores": [3.0, 2.75, 3.75], "median": 3.0, "range": [2.75, 3.75]},
    "gpt-5.6-terra": {"scores": [3.25, 3.5, 3.5], "median": 3.5, "range": [3.25, 3.5]},
}

def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]

def build_evidence(local: dict[str, Any]) -> dict[str, Any]:
    schedule = build_schedule(local["seed"])
    runs = []
    for index, raw in enumerate(local["results"]):
        planned = schedule[index]
        attempt = raw["attempts"][-1]
        task_id = planned["task_id"]
        result = attempt["result"]
        tool_findings = []
        if task_id == "tool-beacon-debrief-v1":
            tool_findings = validate_tool_call(task_id, result.get("tool_name", ""), result.get("tool_arguments", {}))
        validation = score_result(task_id, result, tool_findings)
        runs.append({
            "planned_call_id": planned["planned_call_id"],
            "provider": planned["provider"],
            "model": planned["model"],
            "model_returned": attempt["model_returned"],
            "task_id": task_id,
            "repetition": planned["repetition"],
            "attempts": len(raw["attempts"]),
            "terminal_state": attempt["terminal_state"],
            "actual_cost_usd": attempt["actual_cost_usd"],
            "ttft_ms": attempt["ttft_ms"],
            "latency_ms": attempt["latency_ms"],
            "source_set_digest": raw["source_set_digest"],
            "source_ids": raw["source_ids"],
            "request_sha256": raw["request_sha256"],
            "response_artifact_sha256": sha256(canonical_json(result)),
            "transmitted_bytes": raw["transmitted_bytes"],
            "transmitted_characters": raw["transmitted_characters"],
            "usage": "unknown: over-broad local sanitizer removed token-category values after cost calculation",
            "result": result,
            "validation": validation,
        })
    summaries = []
    for model in sorted({run["model"] for run in runs}):
        group = [run for run in runs if run["model"] == model]
        summaries.append({
            "model": model,
            "runs": len(group),
            "attempts": sum(run["attempts"] for run in group),
            "retries": sum(run["attempts"] - 1 for run in group),
            "returned_model_ids": sorted({run["model_returned"] for run in group}),
            "actual_cost_usd": round(sum(run["actual_cost_usd"] for run in group), 9),
            "hard_disqualifying_runs": sum(run["validation"]["disqualified"] for run in group),
            "disqualifiers": sorted({finding for run in group for finding in run["validation"]["disqualifiers"]}),
            "ttft_ms": {"p50": percentile([run["ttft_ms"] for run in group], .5), "p95": percentile([run["ttft_ms"] for run in group], .95)},
            "latency_ms": {"p50": percentile([run["latency_ms"] for run in group], .5), "p95": percentile([run["latency_ms"] for run in group], .95)},
            "blind_generate_utility": BLIND_SCORES.get(model),
        })
    body = {
        "contract": "provider-bakeoff-evidence.v1",
        "execution_date": "2026-08-17",
        "harness_base_commit": "1e513c2ebe2197981308870388c307ecc0482f7a",
        "authorization": {"planned_calls": 36, "maximum_attempts": 72, "hard_metered_cap_usd": 5.0},
        "discarded_invalid_run": {"http_400": 36, "per_candidate": 12, "retries": 0, "tokens_or_model_ids": 0, "metered_spend_usd": 0.0, "scored": False},
        "fixture_manifest": build_manifest(),
        "models": ["gpt-5.6-terra", "claude-sonnet-5", "gpt-5.6-luna"],
        "completed_calls": len(runs),
        "total_attempts": sum(run["attempts"] for run in runs),
        "total_retries": sum(run["attempts"] - 1 for run in runs),
        "actual_metered_spend_usd": round(local["actual_spend_usd"], 9),
        "worst_case_reserved_usd": round(local["worst_case_reserved_usd"], 9),
        "summaries": summaries,
        "runs": runs,
        "blind_review": {"verdict": "neither surviving group clearly dominates", "identity_mapping": {"A": "gpt-5.6-luna", "B": "gpt-5.6-terra"}},
        "decision": "tradeoff requires Warden priority decision; no provider/model selected",
        "decision_rationale": "claude-sonnet-5 is disqualified by one empty Generate result; Terra has the higher blinded group utility and better p95 latency, while Luna is much cheaper with comparable p50 latency; the accepted rule forbids a hidden weighting",
        "known_evidence_gap": "Per-run token categories are unavailable because the local sanitizer redacted keys containing 'token' after actual cost was computed. No token values are inferred; the sanitizer regression is fixed for future runs.",
        "official_evidence_checked": {
            "openai_models": ["https://developers.openai.com/api/docs/models/gpt-5.6-terra", "https://developers.openai.com/api/docs/models/gpt-5.6-luna"],
            "openai_pricing": "https://developers.openai.com/api/docs/pricing",
            "openai_data": "https://developers.openai.com/api/docs/guides/your-data",
            "anthropic_model": "https://platform.claude.com/docs/en/about-claude/models/overview",
            "anthropic_pricing": "https://platform.claude.com/docs/en/about-claude/pricing",
            "anthropic_retention": "https://platform.claude.com/docs/en/manage-claude/api-and-data-retention",
        },
    }
    return {**body, "evidence_sha256": sha256(canonical_json(body))}

def render_markdown(evidence: dict[str, Any]) -> str:
    lines = ["# Provider bake-off evidence — 2026-08-17", "", "## Outcome", "", "No provider/model is selected. Anthropic was hard-disqualified; Terra versus Luna remains a Warden priority tradeoff.", "", "## Measured comparison", "", "| Model | Runs | Disqualified | Cost USD | TTFT p50/p95 ms | Latency p50/p95 ms | Blind Generate median |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in evidence["summaries"]:
        blind = row["blind_generate_utility"]
        lines.append(f"| `{row['model']}` | {row['runs']} | {row['hard_disqualifying_runs']} | {row['actual_cost_usd']:.9f} | {row['ttft_ms']['p50']:.3f}/{row['ttft_ms']['p95']:.3f} | {row['latency_ms']['p50']:.3f}/{row['latency_ms']['p95']:.3f} | {blind['median'] if blind else 'n/a'} |")
    lines += ["", "All 36 corrected calls completed with no retries. Total measured spend was `$0.123637400`, below the `$5.00` hard cap.", "", "The earlier invalid run is audit-only: 36 HTTP 400 responses, zero inference evidence, zero retries, zero tokens/model IDs, and `$0.00` spend.", "", "## Decision gate", "", "Choose the priority that resolves the surviving tradeoff: prefer Terra for the higher blinded Generate median and better p95 latency, or Luna for substantially lower measured cost with similar p50 latency.", "", "## Evidence limitation", "", evidence["known_evidence_gap"], "", f"Machine-readable evidence digest: `{evidence['evidence_sha256']}`", ""]
    return "\n".join(lines)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()
    evidence = build_evidence(json.loads(args.input.read_text(encoding="utf-8")))
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    args.markdown_output.write_text(render_markdown(evidence), encoding="utf-8", newline="\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
