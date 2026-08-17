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

POST_EVALUATION_PRODUCT_DECISION = {
    "decision_date": "2026-08-17",
    "decision_maker": "Warden",
    "scope": "local personal Windows prototype/pilot",
    "selected_model": "gpt-5.6-luna",
    "basis": "passed every hard gate, was sufficient for the prototype, had similar measured p50 latency to Terra, and cost about 9.3 times less in this run",
    "public_mvp_provider_selected": False,
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
    return {
        **body,
        "evidence_sha256": sha256(canonical_json(body)),
        "post_evaluation_product_decision": POST_EVALUATION_PRODUCT_DECISION,
    }

def render_markdown(evidence: dict[str, Any]) -> str:
    lines = [
        "# Provider evaluation: personal pilot decision", "",
        "This record separates two conclusions. The controlled bake-off did **not** select a winner: `claude-sonnet-5` was disqualified, while `gpt-5.6-terra` and `gpt-5.6-luna` presented an unresolved quality/cost tradeoff under the precommitted no-hidden-weight rule. After reviewing that evidence, the Warden explicitly selected `gpt-5.6-luna` for the local personal Windows pilot because it was sufficient for the prototype and materially cheaper.", "",
        "That product decision is intentionally narrower than the evaluation result. It does not establish Luna as the provider for a future public MVP.", "",
        "## What was tested", "",
        "The deterministic matrix used synthetic Erebos campaign data only. Each candidate ran each task three times, for 12 calls per model and 36 corrected calls total:", "",
        "- **Ask - current airlock state:** answer the current state of the Erebos airlock and identify what changed. This tested grounded retrieval, source citations, current-state synthesis, and Draft-only authority behavior.",
        "- **Check - Vale death claim:** determine whether the claim that Vale is dead is established when canon says no death is recorded, a preparation note predicts death, and a confirmed operation only records beacon activation. This tested authority ordering and resistance to treating predictions as facts.",
        "- **Generate - infirmary details:** produce three sensory details from the sparse fact that the infirmary is powered and available. This tested useful creative expansion, grounding, Draft labeling, and unsupported embellishment risk.",
        "- **Tool - beacon debrief:** emit a structured Draft proposal through the allowlisted tool contract. This tested strict schema compliance, source-set and revision binding, tool selection, and the prohibition on claiming that canon had been applied or promoted.", "",
        "## Runs and audit history", "",
        "An initial request-contract run produced 36 HTTP 400 responses, 12 per candidate. It reached no inference, returned no model identifiers or token usage, used no retries, cost `$0.00`, and is retained only as an unscored audit fact.", "",
        "After deterministic contract corrections and an independent pre-spend review, the same matrix restarted from zero. All 36 corrected calls completed in 36 attempts with no retries. Measured spend was `$0.123637400`, below the `$5.00` hard cap.", "",
        "## Measured comparison", "",
        "| Model | Runs | Hard gates | Cost USD | TTFT p50/p95 ms | Latency p50/p95 ms | Blind Generate scores; median; range |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for row in evidence["summaries"]:
        blind = row["blind_generate_utility"]
        gate = "failed: 1 disqualifying run" if row["hard_disqualifying_runs"] else "passed"
        blind_text = "n/a" if blind is None else f"{blind['scores']}; {blind['median']}; {blind['range']}"
        lines.append(f"| `{row['model']}` | {row['runs']} | {gate} | {row['actual_cost_usd']:.9f} | {row['ttft_ms']['p50']:.3f}/{row['ttft_ms']['p95']:.3f} | {row['latency_ms']['p50']:.3f}/{row['latency_ms']['p95']:.3f} | {blind_text} |")
    lines += ["",
        "Both OpenAI candidates passed every factual, grounding, authority, output-schema, and tool-contract hard gate. Terra's blinded Generate group had the higher median and tighter range. Luna cost `$0.004439400` versus Terra's `$0.041274000` - about 9.3 times less - while their measured p50 latency was effectively similar (`1953.682 ms` versus `1958.741 ms`). Terra had the better p95 latency (`2752.795 ms` versus `3987.669 ms`).", "",
        "### Sonnet hard disqualification", "",
        "`claude-sonnet-5` failed one of its three Generate repetitions. The response used the required object shape but returned an empty `details` array where the task required exactly three details. Deterministic validation therefore recorded `generation_contract_failure` and `invalid_output_schema`. Under the accepted protocol, one hard-disqualifying run disqualified the candidate; its other successful calls do not erase that failure.", "",
        "### Representative sanitized outputs", "",
        "These short excerpts illustrate the surviving candidates; they are examples, not an additional scoring pass:", "",
        "> Luna, Ask: \"The Erebos airlock is currently open. The change was that the crew opened it at 22:10 during the Relay breach, and it remains open.\"", "",
        "> Terra, Ask: \"The Erebos airlock is currently open. It was opened by the crew at 22:10 during the relay breach, and it has remained open since.\"", "",
        "> Luna, Generate: \"A low electrical hum suggests that the infirmary is powered.\"", "",
        "> Terra, Generate: \"A steady electrical hum underscores the powered infirmary.\"", "",
        "The blind review found neither group clearly dominant. Terra was more consistently evocative and varied; Luna included the strongest individually grounded output. Some of Terra's apparent quality edge depended on provisional sensory additions such as antiseptic scent or glowing equipment that were not present in the source fact.", "",
        "## Why the bake-off did not choose automatically", "",
        "The protocol deliberately prohibited inventing weights after seeing results. Terra led the blinded Generate median and p95 latency; Luna led cost by a large margin with similar p50 latency. Turning those dimensions into a single ranking would require a quality-versus-cost weighting that had not been defined before inference. The evaluation therefore preserved the tradeoff instead of retroactively choosing a convenient formula.", "",
        "## Product decision for the personal pilot", "",
        "The Warden subsequently selected `gpt-5.6-luna` for the local personal Windows prototype/pilot. This is an explicit product-priority decision, not a rewritten bake-off result. Luna passed every hard gate, was judged fully sufficient for the prototype, had p50 latency similar to Terra, and cost about 9.3 times less in this run.", "",
        "Scope boundary: this decision authorizes Luna for the personal pilot only. It does **not** establish Luna, OpenAI, or any other candidate as the provider for a future public MVP.", "",
        "## Public MVP revisit", "",
        "Before a public MVP chooses a provider, run a separate authorized evaluation using then-current model identifiers and pricing. The evaluation should include broader eligible candidates, more repetitions, and tasks representative of the public product. Candidate eligibility must account for API availability, privacy and retention controls, structured/tool support, operational fit, regional access, and a bounded cost envelope.", "",
        "Before inference, precommit the ranking or weighting policy and the decision thresholds. The policy should explicitly cover quality, grounding fidelity, authority behavior, structured/tool reliability, latency, cost, privacy, and operational constraints. This record does not choose those weights; doing so belongs to the future decision gate, before results are known.", "",
        "## Evidence limitations", "",
        "- The sample is small: three repetitions per task and 12 calls per model.",
        f"- {evidence['known_evidence_gap']}",
        "- The fixtures are synthetic and deliberately narrow. Results must not be generalized beyond this protocol.",
        "- Latency and pricing are observations from this dated run, not durable provider guarantees.", "",
        "## Evidence traceability", "",
        "The complete sanitized run record is [`tests/provider_eval/evidence/provider-bakeoff-2026-08-17.json`](../tests/provider_eval/evidence/provider-bakeoff-2026-08-17.json). It was first committed at `f491b48cf4331e5adadc52c0b323c41b2c51a879`.", "",
        "The machine-readable `decision` field and measured values retain the original evaluation conclusion. The distinct `post_evaluation_product_decision` field records the later Warden decision without changing that historical conclusion.", "",
        f"Measured-evaluation evidence digest: `{evidence['evidence_sha256']}`", "",
    ]
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
