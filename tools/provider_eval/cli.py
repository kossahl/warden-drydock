"""Command line entrypoint. Dry-run is default; execution is explicitly gated."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
from pathlib import Path
from typing import Any

from .fixture import ENVELOPES, build_manifest, canonical_json, envelope_digest
from .harness import Budget, CANDIDATES, SPEND_CAP_USD, actual_cost_usd, anthropic_request, build_schedule, normalize_events, openai_request, post_sse, request_metrics, retry_allowed, sanitize

def plan(seed: int) -> dict[str, Any]:
    schedule = build_schedule(seed)
    worst = sum(candidate.worst_attempt_usd * 24 for candidate in CANDIDATES)
    return {"mode": "dry-run", "seed": seed, "planned_calls": len(schedule), "maximum_attempts": len(schedule) * 2, "spend_cap_usd": SPEND_CAP_USD, "worst_case_reserved_usd": round(worst, 6), "fixture_manifest": build_manifest(), "schedule": schedule}

def _dispatch(candidate, task_id: str, key: str) -> list[dict[str, Any]]:
    if candidate.provider == "openai":
        request = openai_request(candidate.model, task_id)
        return list(post_sse("https://api.openai.com/v1/responses", {"Authorization": f"Bearer {key}"}, request))
    request = anthropic_request(candidate.model, task_id)
    return list(post_sse("https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}, request))

def _safe_http_error(error: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        body = json.loads(error.read(8192).decode("utf-8", errors="replace"))
    except (ValueError, OSError):
        body = {}
    detail = body.get("error") if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    message = str(detail.get("message", "provider rejected request"))
    message = __import__("re").sub(r"\b(?:org|proj|project|workspace|account|request|req)_[A-Za-z0-9_-]+\b", "[IDENTIFIER_REDACTED]", message, flags=__import__("re").I)
    message = __import__("re").sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "[IDENTIFIER_REDACTED]", message, flags=__import__("re").I)
    category = {400: "invalid_request", 401: "authentication_rejected", 403: "permission_rejected", 404: "model_or_endpoint_not_found", 422: "request_unprocessable"}.get(error.code, "provider_rejected")
    return sanitize({"http_status": error.code, "category": category, "message": message})

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic Warden provider bake-off harness")
    parser.add_argument("--seed", type=int, default=20260817)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--authorize-max-usd", type=float)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.execute:
        print(canonical_json(plan(args.seed)).decode("utf-8"))
        return 0
    if args.authorize_max_usd != SPEND_CAP_USD:
        parser.error("execution requires --authorize-max-usd 5.0")
    candidates = {candidate.model: candidate for candidate in CANDIDATES}
    budget = Budget(args.authorize_max_usd)
    results = []
    actual_spend = 0.0
    fatal_contract_error = False
    for item in build_schedule(args.seed):
        candidate = candidates[item["model"]]
        key = os.environ["OPENAI_API_KEY" if candidate.provider == "openai" else "ANTHROPIC_API_KEY"]
        payload = openai_request(candidate.model, item["task_id"]) if candidate.provider == "openai" else anthropic_request(candidate.model, item["task_id"])
        metrics = request_metrics(payload)
        attempts = []
        for attempt in (1, 2):
            budget.reserve(candidate)
            try:
                events = _dispatch(candidate, item["task_id"], key)
                normalized = normalize_events(candidate.provider, item["task_id"], events)
                charged = actual_cost_usd(candidate, normalized["usage"])
                actual_spend += charged
                attempts.append({"attempt": attempt, "terminal_state": "completed", "actual_cost_usd": charged, **normalized})
                break
            except Exception as error:
                status = error.code if isinstance(error, urllib.error.HTTPError) else error
                failure = {"attempt": attempt, "terminal_state": "failure", "error_type": type(error).__name__, "http_status": getattr(error, "code", None)}
                if isinstance(error, urllib.error.HTTPError):
                    failure["diagnostic"] = _safe_http_error(error)
                attempts.append(failure)
                if not retry_allowed(status, attempt):
                    fatal_contract_error = isinstance(status, int) and 400 <= status < 500 and status not in {408, 409, 429}
                    break
        results.append({**item, **metrics, "source_set_digest": envelope_digest(item["task_id"]), "source_ids": ENVELOPES[item["task_id"]]["included_source_ids"], "attempts": attempts})
        if fatal_contract_error:
            break
    artifact = sanitize({"execution_contract": "provider-bakeoff-execution.v1", "seed": args.seed, "planned_calls": 36, "dispatched_calls": len(results), "maximum_attempts": 72, "fatal_contract_error": fatal_contract_error, "spend_cap_usd": args.authorize_max_usd, "worst_case_reserved_usd": budget.reserved, "actual_spend_usd": actual_spend, "fixture_manifest": build_manifest(), "results": results})
    rendered = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    else:
        print(rendered)
    return 2 if fatal_contract_error else 0

if __name__ == "__main__":
    raise SystemExit(main())
