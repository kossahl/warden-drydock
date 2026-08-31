from __future__ import annotations

import json
import io
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from tools.provider_eval.fixture import BASE_REVISION, ENVELOPES, RECORDS, TOOL_SCHEMAS, build_manifest, canonical_json, envelope_digest, sha256
import tools.provider_eval.fixture as fixture_module
from tools.provider_eval.harness import (
    Budget,
    CANDIDATES,
    SPEND_CAP_USD,
    anthropic_request,
    build_schedule,
    openai_request,
    normalize_events,
    request_metrics,
    actual_cost_usd,
    parse_sse,
    retry_allowed,
    sanitize,
    score_result,
    validate_tool_call,
    validate_schema,
    wire_schema,
)
from tools.provider_eval import cli
from tools.provider_eval.report import build_evidence, render_markdown


class FixtureTests(unittest.TestCase):
    def test_manifest_and_envelope_digests_are_deterministic(self):
        first = build_manifest()
        second = build_manifest()
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertRegex(first["manifest_sha256"], r"^[0-9a-f]{64}$")
        body = dict(first)
        body.pop("manifest_sha256")
        self.assertEqual(first["manifest_sha256"], sha256(canonical_json(body)))
        self.assertEqual(4, len(first["envelopes"]))
        self.assertEqual(["location-erebos", "session-003"], ENVELOPES["ask-airlock-v1"]["included_source_ids"])

    def test_manifest_digest_changes_when_a_source_or_envelope_mutates(self):
        baseline = build_manifest()["manifest_sha256"]
        with patch.dict(fixture_module.RECORDS, {"npc-vale": RECORDS["npc-vale"] + "A post-approval note."}):
            self.assertNotEqual(baseline, build_manifest()["manifest_sha256"])
        with patch.dict(fixture_module.ENVELOPES, {"ask-airlock-v1": {**ENVELOPES["ask-airlock-v1"], "included_source_ids": ["session-003", "location-erebos"]}}):
            self.assertNotEqual(baseline, build_manifest()["manifest_sha256"])

    def test_manifest_digest_is_stable_under_source_dict_iteration_order(self):
        baseline = build_manifest()
        resequenced = dict(reversed(list(RECORDS.items())))
        self.assertNotEqual(list(RECORDS), list(resequenced))
        with patch.dict(fixture_module.RECORDS, resequenced, clear=True):
            resequenced_manifest = build_manifest()
        self.assertEqual(canonical_json(baseline), canonical_json(resequenced_manifest))
        self.assertEqual(baseline["manifest_sha256"], resequenced_manifest["manifest_sha256"])

    def test_exact_three_tool_schemas_are_closed(self):
        expected = {"fixture_read_source", "fixture_read_revision_context", "fixture_emit_proposal_draft"}
        self.assertEqual(expected, set(TOOL_SCHEMAS))
        for schema in TOOL_SCHEMAS.values():
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
        requests = (openai_request("gpt-5.6-terra", "tool-beacon-debrief-v1"), anthropic_request("claude-sonnet-5", "tool-beacon-debrief-v1"))
        for request in requests:
            wired = {tool["name"]: tool.get("parameters") or tool.get("input_schema") for tool in request["tools"]}
            self.assertEqual(expected, set(wired))
            for schema in wired.values():
                self.assertFalse(schema["additionalProperties"])
            emit = {"campaign_id": "campaign-erebos", "base_revision": BASE_REVISION, "source_set_digest": envelope_digest("tool-beacon-debrief-v1"), "proposal_kind": "beacon_debrief", "title": "Beacon debrief", "source_ids": ["faction-helix"], "suggested_changes": ["Draft note"], "unexpected_property": True}
            self.assertTrue(validate_schema(wired["fixture_emit_proposal_draft"], emit))
            self.assertIn("invalid_tool_schema", validate_tool_call("tool-beacon-debrief-v1", "fixture_emit_proposal_draft", emit))


class ScheduleBudgetRetryTests(unittest.TestCase):
    def test_schedule_is_seeded_interleaved_and_complete(self):
        schedule = build_schedule(41)
        self.assertEqual(schedule, build_schedule(41))
        self.assertEqual(36, len(schedule))
        self.assertEqual(36, len({item["planned_call_id"] for item in schedule}))
        for offset in range(0, 36, 3):
            self.assertEqual(3, len({item["model"] for item in schedule[offset:offset + 3]}))
            self.assertEqual(1, len({(item["task_id"], item["repetition"]) for item in schedule[offset:offset + 3]}))

    def test_reservation_uses_inclusive_caps_and_stops_predispatch(self):
        budget = Budget(0.01)
        with self.assertRaises(RuntimeError):
            budget.reserve(CANDIDATES[0])
        full = Budget()
        total = sum(full.reserve(candidate) for candidate in CANDIDATES for _ in range(24))
        self.assertLessEqual(total, SPEND_CAP_USD)
        self.assertAlmostEqual(0.045056, CANDIDATES[0].worst_attempt_usd)

    def test_only_first_identical_transient_retry_is_allowed(self):
        for status in (408, 409, 429, 500, 529, 599):
            self.assertTrue(retry_allowed(status, 1))
        self.assertFalse(retry_allowed(400, 1))
        self.assertFalse(retry_allowed(429, 2))
        self.assertTrue(retry_allowed(urllib.error.URLError("temporary"), 1))

    def test_dispatch_retry_is_identical_and_limited_to_one(self):
        candidate = CANDIDATES[0]
        calls = []
        def fake_dispatch(got_candidate, task_id, key):
            calls.append((got_candidate, task_id, key))
            if len(calls) == 1:
                raise urllib.error.URLError("temporary")
            return [{"event": "done"}]
        budget = Budget()
        with patch.object(cli, "_dispatch", side_effect=fake_dispatch):
            attempts = []
            for attempt in (1, 2):
                budget.reserve(candidate)
                try:
                    attempts.append(cli._dispatch(candidate, "ask-airlock-v1", "test-key"))
                    break
                except Exception as error:
                    if not retry_allowed(error, attempt):
                        break
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0], calls[1])
        self.assertEqual([[{"event": "done"}]], attempts)

    def test_execute_fails_fast_on_first_http_400_and_sanitizes_diagnostic(self):
        body = io.BytesIO(b'{"error":{"type":"invalid_request_error","message":"bad schema"}}')
        error = urllib.error.HTTPError("https://provider.invalid", 400, "Bad Request", {}, body)
        with tempfile.TemporaryDirectory() as directory:
            output = __import__("pathlib").Path(directory) / "result.json"
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test", "ANTHROPIC_API_KEY": "test"}), patch.object(cli, "_dispatch", side_effect=error):
                self.assertEqual(2, cli.main(["--execute", "--authorize-max-usd", "5.0", "--output", str(output)]))
            artifact = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(artifact["fatal_contract_error"])
        self.assertEqual(1, artifact["dispatched_calls"])
        diagnostic = artifact["results"][0]["attempts"][0]["diagnostic"]
        self.assertEqual("bad schema", diagnostic["message"])

    def test_nontransient_http_contract_failures_all_fail_fast(self):
        for status in (401, 403, 404, 422):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                output = __import__("pathlib").Path(directory) / "result.json"
                error = urllib.error.HTTPError("https://provider.invalid", status, "Rejected", {}, io.BytesIO(b'{"error":{"message":"request req_private123 rejected"}}'))
                with patch.dict("os.environ", {"OPENAI_API_KEY": "test", "ANTHROPIC_API_KEY": "test"}), patch.object(cli, "_dispatch", side_effect=error):
                    self.assertEqual(2, cli.main(["--execute", "--authorize-max-usd", "5.0", "--output", str(output)]))
                artifact = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(1, artifact["dispatched_calls"])
                self.assertNotIn("req_private123", json.dumps(artifact))

    def test_cache_write_tokens_are_charged(self):
        usage = {"input_tokens": 10, "cached_tokens": 0, "cache_write_tokens": 10, "output_tokens": 0}
        self.assertAlmostEqual(0.000025, actual_cost_usd(CANDIDATES[0], usage))


class RequestContractTests(unittest.TestCase):
    def test_wire_schema_removes_unsupported_keywords_but_preserves_full_contract(self):
        wire = wire_schema(TOOL_SCHEMAS["fixture_emit_proposal_draft"])
        rendered = json.dumps(wire)
        for keyword in ("$schema", "uniqueItems", "minItems", "maxItems"):
            self.assertNotIn(keyword, rendered)
        self.assertIn("uniqueItems", json.dumps(TOOL_SCHEMAS["fixture_emit_proposal_draft"]))

    def test_openai_contract_disables_storage_and_cache_breakpoints(self):
        request = openai_request("gpt-5.6-terra", "tool-beacon-debrief-v1")
        self.assertIs(request["store"], False)
        self.assertEqual({"mode": "explicit"}, request["prompt_cache_options"])
        self.assertIs(request["parallel_tool_calls"], False)
        self.assertEqual(2048, request["max_output_tokens"])
        self.assertEqual("fixture_emit_proposal_draft", request["tool_choice"]["name"])
        self.assertEqual(set(TOOL_SCHEMAS), {tool["name"] for tool in request["tools"]})
        self.assertTrue(all(tool["strict"] for tool in request["tools"]))

    def test_requests_fit_conservative_complete_input_cap(self):
        for candidate in CANDIDATES:
            for task_id in ENVELOPES:
                request = openai_request(candidate.model, task_id) if candidate.provider == "openai" else anthropic_request(candidate.model, task_id)
                self.assertLessEqual(request_metrics(request)["transmitted_bytes"], 8192)

    def test_normalizes_anthropic_structured_result_and_usage(self):
        events = [
            {"event": "message_start", "data": {"type": "message_start", "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 10}}}, "ttft_ms": 1, "elapsed_ms": 1},
            {"event": "content_block_delta", "data": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": '{"status":"Draft","base_revision":"fixture-erebos-r3","source_ids":["location-erebos","session-003"],"answer":"Open since 22:10 per session-003."}'}}, "ttft_ms": 1, "elapsed_ms": 2},
            {"event": "message_delta", "data": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}}, "ttft_ms": 1, "elapsed_ms": 3},
        ]
        normalized = normalize_events("anthropic", "ask-airlock-v1", events)
        self.assertEqual(10, normalized["usage"]["input_tokens"])
        self.assertFalse(normalized["validation"]["disqualified"])

    def test_non_object_structured_and_tool_values_disqualify_without_crash(self):
        text_events = [
            {"event": "message_start", "data": {"type": "message_start", "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 11}}}, "ttft_ms": 1, "elapsed_ms": 1},
            {"event": "content_block_delta", "data": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "[]"}}, "ttft_ms": 1, "elapsed_ms": 2},
            {"event": "message_delta", "data": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}, "ttft_ms": 1, "elapsed_ms": 3},
        ]
        normalized = normalize_events("anthropic", "ask-airlock-v1", text_events)
        self.assertTrue(normalized["validation"]["disqualified"])
        self.assertEqual(11, normalized["usage"]["input_tokens"])

        tool_events = [
            {"event": "content_block_start", "data": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "fixture_emit_proposal_draft"}}, "ttft_ms": 1, "elapsed_ms": 1},
            {"event": "content_block_delta", "data": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "[]"}}, "ttft_ms": 1, "elapsed_ms": 2},
        ]
        normalized_tool = normalize_events("anthropic", "tool-beacon-debrief-v1", tool_events)
        self.assertIn("invalid_tool_schema", normalized_tool["validation"]["disqualifiers"])

    def test_full_schema_rejects_wire_subset_invalid_values(self):
        full = TOOL_SCHEMAS["fixture_emit_proposal_draft"]
        invalid = {"campaign_id": "campaign-erebos", "base_revision": BASE_REVISION, "source_set_digest": "x", "proposal_kind": "beacon_debrief", "title": "Beacon debrief", "source_ids": ["faction-helix", "faction-helix"], "suggested_changes": []}
        self.assertTrue(validate_schema(full, invalid))
        findings = validate_tool_call("tool-beacon-debrief-v1", "fixture_emit_proposal_draft", invalid)
        self.assertIn("invalid_tool_schema", findings)

        structured = {"status": "Draft", "base_revision": BASE_REVISION, "source_ids": [1], "answer": 7}
        scored = score_result("ask-airlock-v1", structured)
        self.assertIn("invalid_output_schema", scored["disqualifiers"])

    def test_anthropic_contract_has_adaptive_low_effort_and_no_cache_or_server_tools(self):
        request = anthropic_request("claude-sonnet-5", "tool-beacon-debrief-v1")
        self.assertEqual({"type": "adaptive"}, request["thinking"])
        self.assertEqual("low", request["output_config"]["effort"])
        self.assertEqual(2048, request["max_tokens"])
        self.assertNotIn("cache_control", json.dumps(request))
        self.assertEqual(set(TOOL_SCHEMAS), {tool["name"] for tool in request["tools"]})

    def test_sse_captures_ttft_and_latency(self):
        events = list(parse_sse([b"event: delta\n", b'data: {"x":1}\n', b"\n"], started=0.0))
        self.assertEqual("delta", events[0]["event"])
        self.assertEqual({"x": 1}, events[0]["data"])
        self.assertGreaterEqual(events[0]["elapsed_ms"], events[0]["ttft_ms"])


class BoundaryTests(unittest.TestCase):
    def _emit(self):
        task = "tool-beacon-debrief-v1"
        return task, {"campaign_id": "campaign-erebos", "base_revision": BASE_REVISION, "source_set_digest": envelope_digest(task), "proposal_kind": "beacon_debrief", "title": "Beacon debrief", "source_ids": ["faction-helix"], "suggested_changes": ["Draft note"]}

    def test_tool_validation_binds_sources_digest_revision_and_single_emit(self):
        task, arguments = self._emit()
        self.assertEqual([], validate_tool_call(task, "fixture_emit_proposal_draft", arguments))
        self.assertIn("invalid_or_repeated_emit", validate_tool_call(task, "fixture_emit_proposal_draft", arguments, successful_emits=1))
        bad = dict(arguments, source_ids=["not-in-envelope"])
        self.assertIn("retrieval_widening", validate_tool_call(task, "fixture_emit_proposal_draft", bad))
        self.assertEqual(["outside_allowlist_tool"], validate_tool_call(task, "shell", {}))

    def test_sanitization_removes_secrets_headers_and_paths(self):
        sanitized = sanitize({"Authorization": "Bearer abcdefghi", "api_key": "sk-secretvalue", "message": r"at C:\\Users\\name\\secret.txt"})
        self.assertEqual("[REDACTED]", sanitized["Authorization"])
        self.assertEqual("[REDACTED]", sanitized["api_key"])
        self.assertNotIn("Users", sanitized["message"])

    def test_disqualifiers_are_hard(self):
        result = {"status": "Draft", "base_revision": BASE_REVISION, "source_ids": ["location-erebos", "session-003"], "answer": "Open since 22:10 per session-003; it was applied."}
        score = score_result("ask-airlock-v1", result)
        self.assertTrue(score["disqualified"])
        self.assertIn("mutation_or_promotion_claim", score["disqualifiers"])

        negated = {"status": "Draft", "base_revision": BASE_REVISION, "source_ids": ["location-erebos", "session-003"], "answer": "Open since 22:10 per session-003; this does not apply, approve, or promote canon."}
        self.assertFalse(score_result("ask-airlock-v1", negated)["disqualified"])


class ReportTests(unittest.TestCase):
    def test_report_rebuilds_manifest_instead_of_reusing_sanitized_input(self):
        local = {
            "seed": 41,
            "fixture_manifest": {"envelopes": [{"task_id": "a[REDACTED]"}]},
            "actual_spend_usd": 0.0,
            "worst_case_reserved_usd": 0.0,
            "results": [],
        }
        evidence = build_evidence(local)
        self.assertEqual(
            ["ask-airlock-v1", "check-vale-death-v1", "generate-infirmary-v1", "tool-beacon-debrief-v1"],
            [entry["task_id"] for entry in evidence["fixture_manifest"]["envelopes"]],
        )
        self.assertNotIn("[REDACTED]", json.dumps(evidence["fixture_manifest"]))

    def test_product_decision_is_distinct_from_immutable_evaluation_conclusion(self):
        local = {
            "seed": 41,
            "fixture_manifest": {},
            "actual_spend_usd": 0.0,
            "worst_case_reserved_usd": 0.0,
            "results": [],
        }
        evidence = build_evidence(local)
        self.assertEqual("tradeoff requires Warden priority decision; no provider/model selected", evidence["decision"])
        self.assertEqual("gpt-5.6-luna", evidence["post_evaluation_product_decision"]["selected_model"])
        self.assertFalse(evidence["post_evaluation_product_decision"]["public_mvp_provider_selected"])
        measured = dict(evidence)
        digest = measured.pop("evidence_sha256")
        measured.pop("post_evaluation_product_decision")
        self.assertEqual(digest, sha256(canonical_json(measured)))
        markdown = render_markdown(evidence)
        self.assertIn("## Product decision for the personal pilot", markdown)
        self.assertIn("## Public MVP revisit", markdown)


if __name__ == "__main__":
    unittest.main()
