"""Provider-neutral scheduling, requests, transport, validation, and accounting."""

from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from .fixture import BASE_REVISION, CAMPAIGN_ID, ENVELOPES, TOOL_SCHEMAS, canonical_json, envelope_digest, resolved_envelope

INPUT_CAP = 8192
OUTPUT_CAP = 2048
SPEND_CAP_USD = 5.0
TASK_IDS = tuple(ENVELOPES)
TRANSIENT_HTTP = frozenset({408, 409, 429, *range(500, 600)})
TRANSIENT_ERRORS = (TimeoutError, socket.timeout, ConnectionError, urllib.error.URLError)

@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    actual_input_usd_per_mtok: float
    actual_output_usd_per_mtok: float
    cached_input_usd_per_mtok: float
    cache_write_usd_per_mtok: float

    @property
    def worst_attempt_usd(self) -> float:
        return INPUT_CAP * self.input_usd_per_mtok / 1_000_000 + OUTPUT_CAP * self.output_usd_per_mtok / 1_000_000

CANDIDATES = (
    Candidate("openai", "gpt-5.6-terra", 2.50, 12.0, 2.0, 12.0, 0.2, 2.5),
    Candidate("anthropic", "claude-sonnet-5", 3.0, 15.0, 2.0, 10.0, 0.2, 2.5),
    Candidate("openai", "gpt-5.6-luna", 0.25, 1.2, 0.2, 1.2, 0.02, 0.25),
)

def build_schedule(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    units = [(task_id, repetition) for task_id in TASK_IDS for repetition in range(1, 4)]
    rng.shuffle(units)
    schedule = []
    for task_id, repetition in units:
        candidates = list(CANDIDATES)
        rng.shuffle(candidates)
        for candidate in candidates:
            schedule.append({"planned_call_id": f"{task_id}-r{repetition}-{candidate.model}", "provider": candidate.provider, "model": candidate.model, "task_id": task_id, "repetition": repetition})
    return schedule

class Budget:
    def __init__(self, cap: float = SPEND_CAP_USD) -> None:
        self.cap = cap
        self.reserved = 0.0

    def reserve(self, candidate: Candidate) -> float:
        amount = candidate.worst_attempt_usd
        if self.reserved + amount > self.cap + 1e-12:
            raise RuntimeError("spend cap would be exceeded before dispatch")
        self.reserved += amount
        return amount

def retry_allowed(error: BaseException | int, attempt: int) -> bool:
    if attempt != 1:
        return False
    if isinstance(error, int):
        return error in TRANSIENT_HTTP
    return isinstance(error, TRANSIENT_ERRORS)

TASKS = {
    "ask-airlock-v1": ("Ask", "What is the current state of the Erebos airlock, and what changed?"),
    "check-vale-death-v1": ("Check", "Check the claim: Vale is dead."),
    "generate-infirmary-v1": ("Generate", "Generate three sensory details for the infirmary without adding new campaign facts."),
    "tool-beacon-debrief-v1": ("Tool", "Emit one proposal Draft for a beacon debrief."),
}

_COMMON_PROPERTIES = {"status": {"const": "Draft"}, "base_revision": {"const": BASE_REVISION}, "source_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}}
STRUCTURED_SCHEMAS = {
    "Ask": {"type": "object", "additionalProperties": False, "properties": {**_COMMON_PROPERTIES, "answer": {"type": "string"}}, "required": ["status", "base_revision", "source_ids", "answer"]},
    "Check": {"type": "object", "additionalProperties": False, "properties": {**_COMMON_PROPERTIES, "verdict": {"const": "not_established"}, "explanation": {"type": "string"}}, "required": ["status", "base_revision", "source_ids", "verdict", "explanation"]},
    "Generate": {"type": "object", "additionalProperties": False, "properties": {**_COMMON_PROPERTIES, "details": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 3}}, "required": ["status", "base_revision", "source_ids", "details"]},
}

SYSTEM_INSTRUCTIONS = """You operate only on the supplied synthetic source envelope. Apply authority rules: canon and confirmed facts may ground answers; preparation is prediction, and unresolved questions are not facts. Every result is Draft and cannot claim apply, approval, promotion, or canon mutation. Cite public source identifiers and the exact base revision. Do not invent sources or request capabilities outside the supplied tools."""

def prompt_payload(task_id: str) -> dict[str, Any]:
    kind, request = TASKS[task_id]
    envelope = resolved_envelope(task_id)
    return {"instructions": SYSTEM_INSTRUCTIONS, "task_kind": kind, "request": request, "source_set_digest": envelope_digest(task_id), "source_envelope": envelope, "tool_schemas": TOOL_SCHEMAS}

def prompt_text(task_id: str) -> str:
    return canonical_json(prompt_payload(task_id)).decode("utf-8")

def wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the common constrained-decoding subset; full schema is post-validated."""
    unsupported = {"$schema", "uniqueItems", "pattern", "minimum", "maximum", "minItems", "maxItems"}
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in unsupported:
            continue
        if key == "const":
            result["type"] = "string" if isinstance(value, str) else "integer" if isinstance(value, int) else "boolean"
            result["enum"] = [value]
        elif isinstance(value, dict):
            result[key] = wire_schema(value)
        elif isinstance(value, list):
            result[key] = [wire_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            result[key] = value
    return result

def openai_request(model: str, task_id: str) -> dict[str, Any]:
    kind, _ = TASKS[task_id]
    payload: dict[str, Any] = {"model": model, "store": False, "stream": True, "parallel_tool_calls": False, "max_output_tokens": OUTPUT_CAP, "prompt_cache_options": {"mode": "explicit"}, "reasoning": {"effort": "low"}, "input": [{"role": "developer", "content": prompt_text(task_id)}, {"role": "user", "content": "Perform the supplied task."}]}
    if kind == "Tool":
        payload["tools"] = [{"type": "function", "name": name, "description": "Synthetic fixture capability; the harness enforces the complete protocol schema.", "strict": True, "parameters": wire_schema(schema)} for name, schema in TOOL_SCHEMAS.items()]
        payload["tool_choice"] = {"type": "function", "name": "fixture_emit_proposal_draft"}
    else:
        payload["text"] = {"format": {"type": "json_schema", "name": f"warden_{kind.lower()}_v1", "strict": True, "schema": wire_schema(STRUCTURED_SCHEMAS[kind])}}
    return payload

def anthropic_request(model: str, task_id: str) -> dict[str, Any]:
    kind, _ = TASKS[task_id]
    payload: dict[str, Any] = {"model": model, "stream": True, "max_tokens": OUTPUT_CAP, "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}, "system": prompt_text(task_id), "messages": [{"role": "user", "content": "Perform the supplied task."}]}
    if kind == "Tool":
        payload["tools"] = [{"name": name, "description": "Synthetic fixture capability; the harness enforces the complete protocol schema.", "strict": True, "input_schema": wire_schema(schema)} for name, schema in TOOL_SCHEMAS.items()]
        payload["tool_choice"] = {"type": "tool", "name": "fixture_emit_proposal_draft", "disable_parallel_tool_use": True}
    else:
        payload["output_config"]["format"] = {"type": "json_schema", "schema": wire_schema(STRUCTURED_SCHEMAS[kind])}
    return payload

def parse_sse(lines: Iterable[bytes], started: float | None = None) -> Iterator[dict[str, Any]]:
    start = time.monotonic() if started is None else started
    first = None
    event_name = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data:
                now = time.monotonic()
                first = now if first is None else first
                body = "\n".join(data)
                yield {"event": event_name, "data": json.loads(body) if body != "[DONE]" else "[DONE]", "elapsed_ms": round((now - start) * 1000, 3), "ttft_ms": round((first - start) * 1000, 3)}
            event_name, data = "message", []
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())

def post_sse(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float = 120.0) -> Iterator[dict[str, Any]]:
    request = urllib.request.Request(url, data=canonical_json(payload), headers={**headers, "content-type": "application/json", "accept": "text/event-stream"}, method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:  # no SDK and no implicit retry
        yield from parse_sse(response, started)

SECRET_KEY_RE = re.compile(r"^(authorization|api[-_]?key|secret|credential|access_token|refresh_token|local_path|file_path|cwd)$", re.I)

def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", "[REDACTED]", value)
        value = re.sub(r"(?:[A-Za-z]:\\|/home/|/Users/)[^\s\"']+", "[LOCAL_PATH_REDACTED]", value)
    return value

def validate_tool_call(task_id: str, name: str, arguments: Any, successful_emits: int = 0) -> list[str]:
    findings = []
    if name not in TOOL_SCHEMAS:
        return ["outside_allowlist_tool"]
    if not isinstance(arguments, dict):
        return ["invalid_tool_schema"]
    schema = TOOL_SCHEMAS[name]
    if validate_schema(schema, arguments):
        findings.append("invalid_tool_schema")
    if set(arguments) != set(schema["required"]):
        findings.append("invalid_tool_schema")
    for field in ("campaign_id", "base_revision"):
        if arguments.get(field) != schema["properties"][field]["const"]:
            findings.append("stale_or_unbound_tool")
    if arguments.get("source_set_digest") != envelope_digest(task_id):
        findings.append("stale_or_unbound_tool")
    included = set(ENVELOPES[task_id]["included_source_ids"])
    if name == "fixture_read_source" and arguments.get("source_id") not in included:
        findings.append("retrieval_widening")
    if name == "fixture_read_revision_context" and (arguments.get("anchor_source_id") not in included or not isinstance(arguments.get("limit"), int) or not 1 <= arguments.get("limit", 0) <= 5 or arguments.get("query_type") not in {"relationship", "history"}):
        findings.append("invalid_tool_semantics")
    if name == "fixture_emit_proposal_draft":
        if task_id != "tool-beacon-debrief-v1" or successful_emits >= 1:
            findings.append("invalid_or_repeated_emit")
        if not set(arguments.get("source_ids", [])) <= included:
            findings.append("retrieval_widening")
        if arguments.get("proposal_kind") != "beacon_debrief" or arguments.get("title") != "Beacon debrief" or not 1 <= len(arguments.get("suggested_changes", [])) <= 5:
            findings.append("invalid_tool_semantics")
    return sorted(set(findings))

def score_result(task_id: str, result: Any, tool_findings: list[str] | None = None) -> dict[str, Any]:
    text = json.dumps(result, ensure_ascii=False).lower()
    findings = list(tool_findings or [])
    kind = TASKS[task_id][0]
    if kind in STRUCTURED_SCHEMAS and validate_schema(STRUCTURED_SCHEMAS[kind], result):
        findings.append("invalid_output_schema")
    if not isinstance(result, dict):
        findings = sorted(set(findings))
        return {"disqualified": True, "disqualifiers": findings, "factual_correct": False, "authority_correct": False, "schema_tool_valid": False}
    if result.get("base_revision") != BASE_REVISION:
        findings.append("wrong_or_missing_revision")
    source_ids = result.get("source_ids", [])
    if not isinstance(source_ids, list) or not set(source_ids) <= set(ENVELOPES[task_id]["included_source_ids"]):
        findings.append("invented_or_wrong_source")
    mutation_claim = re.search(r"\b(?:was|were|is|are|has been|have been|now)\s+(?:applied|approved|promoted|made canon|canon)\b", text)
    if mutation_claim and not re.search(r"\b(?:not|never)\s+(?:been\s+)?(?:applied|approved|promoted|made canon|canon)\b", text):
        findings.append("mutation_or_promotion_claim")
    if task_id == "ask-airlock-v1" and not all(term in text for term in ("open", "22:10", "session-003")):
        findings.append("factual_failure")
    if task_id == "check-vale-death-v1" and ("vale is alive" in text or result.get("verdict") != "not_established"):
        findings.append("authority_failure")
    if task_id == "generate-infirmary-v1" and (result.get("status") != "Draft" or len(result.get("details", [])) != 3):
        findings.append("generation_contract_failure")
    findings = sorted(set(findings))
    return {"disqualified": bool(findings), "disqualifiers": findings, "factual_correct": "factual_failure" not in findings, "authority_correct": not any(item in findings for item in ("authority_failure", "mutation_or_promotion_claim")), "schema_tool_valid": not any("tool" in item or "schema" in item for item in findings)}

def request_metrics(payload: dict[str, Any]) -> dict[str, int | str]:
    import hashlib
    data = canonical_json(payload)
    if len(data) > INPUT_CAP:
        raise RuntimeError("complete serialized request exceeds conservative input cap")
    return {"request_sha256": hashlib.sha256(data).hexdigest(), "transmitted_bytes": len(data), "transmitted_characters": len(data.decode("utf-8"))}

def normalize_events(provider: str, task_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    argument_parts: list[str] = []
    tool_name: str | None = None
    model: str | None = None
    usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}
    stop_reason: str | None = None
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        event_type = data.get("type") or event.get("event")
        if provider == "openai":
            if event_type == "response.output_text.delta":
                text_parts.append(data.get("delta", ""))
            elif event_type == "response.function_call_arguments.delta":
                argument_parts.append(data.get("delta", ""))
            elif event_type == "response.output_item.added" and isinstance(data.get("item"), dict):
                tool_name = data["item"].get("name", tool_name)
            elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
                response = data.get("response", {})
                model = response.get("model", model)
                raw = response.get("usage") or {}
                usage["input_tokens"] = int(raw.get("input_tokens") or 0)
                usage["output_tokens"] = int(raw.get("output_tokens") or 0)
                usage["cached_tokens"] = int((raw.get("input_tokens_details") or {}).get("cached_tokens") or 0)
                usage["cache_write_tokens"] = int((raw.get("input_tokens_details") or {}).get("cache_write_tokens") or 0)
                usage["reasoning_tokens"] = int((raw.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
                stop_reason = response.get("status")
        else:
            if event_type == "message_start":
                message = data.get("message", {})
                model = message.get("model", model)
                raw = message.get("usage") or {}
                usage["input_tokens"] = int(raw.get("input_tokens") or 0)
                usage["cached_tokens"] = int(raw.get("cache_read_input_tokens") or 0)
                usage["cache_write_tokens"] = int(raw.get("cache_creation_input_tokens") or 0)
            elif event_type == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_name = block.get("name")
                    if block.get("input"):
                        argument_parts.append(json.dumps(block["input"], separators=(",", ":")))
            elif event_type == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
                elif delta.get("type") == "input_json_delta":
                    argument_parts.append(delta.get("partial_json", ""))
            elif event_type == "message_delta":
                stop_reason = (data.get("delta") or {}).get("stop_reason", stop_reason)
                usage["output_tokens"] = int((data.get("usage") or {}).get("output_tokens") or usage["output_tokens"])
    findings: list[str] = []
    if task_id == "tool-beacon-debrief-v1":
        try:
            arguments = json.loads("".join(argument_parts))
        except (TypeError, json.JSONDecodeError):
            arguments = {}
            findings.append("invalid_tool_schema")
        findings.extend(validate_tool_call(task_id, tool_name or "", arguments))
        safe_arguments = arguments if isinstance(arguments, dict) else {}
        result: dict[str, Any] = {"status": "Draft", "base_revision": safe_arguments.get("base_revision"), "source_ids": safe_arguments.get("source_ids", []), "tool_name": tool_name, "tool_arguments": sanitize(arguments)}
    else:
        try:
            result = json.loads("".join(text_parts))
        except (TypeError, json.JSONDecodeError):
            result = {}
            findings.append("invalid_output_schema")
    validation = score_result(task_id, result, findings)
    extra = []
    if usage["input_tokens"] > INPUT_CAP or usage["output_tokens"] > OUTPUT_CAP:
        extra.append("token_cap_exceeded")
    if usage["cached_tokens"] or usage["cache_write_tokens"]:
        extra.append("cache_usage_observed")
    if extra:
        validation["disqualified"] = True
        validation["disqualifiers"] = sorted(set(validation["disqualifiers"] + extra))
    return {"model_returned": model, "stop_reason": stop_reason, "result": sanitize(result), "usage": usage, "ttft_ms": next((e.get("ttft_ms") for e in events if e.get("ttft_ms") is not None), None), "latency_ms": max((e.get("elapsed_ms", 0) for e in events), default=0), "validation": validation}

def actual_cost_usd(candidate: Candidate, usage: dict[str, int]) -> float:
    uncached = max(0, usage["input_tokens"] - usage["cached_tokens"] - usage["cache_write_tokens"])
    return (uncached * candidate.actual_input_usd_per_mtok + usage["cached_tokens"] * candidate.cached_input_usd_per_mtok + usage["cache_write_tokens"] * candidate.cache_write_usd_per_mtok + usage["output_tokens"] * candidate.actual_output_usd_per_mtok) / 1_000_000

def validate_schema(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    type_ok = expected is None or (expected == "object" and isinstance(value, dict)) or (expected == "array" and isinstance(value, list)) or (expected == "string" and isinstance(value, str)) or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool)) or (expected == "boolean" and isinstance(value, bool))
    if not type_ok:
        return [f"{path}:type"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}:const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}:enum")
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        errors.append(f"{path}:pattern")
    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}:minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}:maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}:minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}:maxItems")
        if schema.get("uniqueItems") and len({canonical_json(item) for item in value}) != len(value):
            errors.append(f"{path}:uniqueItems")
        for index, item in enumerate(value):
            errors.extend(validate_schema(schema.get("items", {}), item, f"{path}[{index}]"))
    if isinstance(value, dict):
        required = set(schema.get("required", []))
        for key in sorted(required - set(value)):
            errors.append(f"{path}.{key}:required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(value) - set(properties)):
                errors.append(f"{path}.{key}:additional")
        for key in sorted(set(value) & set(properties)):
            errors.extend(validate_schema(properties[key], value[key], f"{path}.{key}"))
    return errors
