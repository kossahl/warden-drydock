import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL_PATH = Path(__file__).parents[1] / ".codex" / "hooks" / "agent_experience_maintenance.py"
SPEC = importlib.util.spec_from_file_location("agent_experience_maintenance", TOOL_PATH)
TOOL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TOOL)


def event(version, filename, transcript=None):
    base = {
        "schema_version": version, "captured_at": "2026-08-10T00:00:00+00:00",
        "session_id": "s", "turn_id": "t", "hook_event_name": "Stop",
        "transcript_path": transcript, "cwd": "D:/repo", "model": "model",
        "permission_mode": "default",
    }
    if version == 2:
        base.update({
            "event_id": Path(filename).stem, "agent": None, "skills": [], "outcome": None,
            "test_status": None, "correction_count": None, "input_tokens": None,
            "output_tokens": None, "files_changed_count": None, "field_provenance": {},
        })
    return base


class AgentExperienceMaintenanceTests(unittest.TestCase):
    def write(self, root, name, value):
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_mixed_valid_queue_processing_and_repeatability(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / ".agent-experience"
            queue = runtime / "pending"
            queue.mkdir(parents=True)
            self.write(queue, "legacy.json", event(1, "legacy.json"))
            self.write(queue, "modern.json", event(2, "modern.json"))
            self.write(runtime, "processed.json", {
                "schema_version": 1,
                "events": {"legacy.json": {"status": "processed", "processed_at": "2026-08-10T01:00:00Z"}},
            })
            first = TOOL.audit_queue(queue)
            second = TOOL.audit_queue(queue)
            self.assertEqual(first, second)
            self.assertEqual(first["categories"]["valid_v1"], ["legacy.json"])
            self.assertEqual(first["categories"]["valid_v2"], ["modern.json"])
            self.assertEqual(first["processing"]["processed"], ["legacy.json"])
            self.assertEqual(first["processing"]["unprocessed"], ["modern.json"])

    def test_invalid_categories_and_stable_ordering(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            (queue / "z-malformed.json").write_text("{", encoding="utf-8")
            self.write(queue, "a-array.json", [])
            self.write(queue, "b-unknown.json", {"schema_version": 9})
            mismatch = event(2, "different.json")
            self.write(queue, "c-mismatch.json", mismatch)
            oversized = event(2, "d-oversized.json")
            oversized["padding"] = "x" * TOOL.MAX_V2_BYTES
            self.write(queue, "d-oversized.json", oversized)
            (queue / "ignored.txt").write_text("ignored", encoding="utf-8")
            (queue / "subdirectory").mkdir()
            report = TOOL.audit_queue(queue)
            self.assertEqual(report["categories"]["malformed_json"], ["z-malformed.json"])
            self.assertEqual(report["categories"]["non_object"], ["a-array.json"])
            self.assertEqual(report["categories"]["unknown_schema"], ["b-unknown.json"])
            self.assertEqual(report["categories"]["identity_mismatch"], ["c-mismatch.json"])
            self.assertEqual(report["categories"]["oversized_v2"], ["d-oversized.json"])
            self.assertEqual(report["categories"]["ignored"], ["ignored.txt", "subdirectory"])

    def test_invalid_v2_optional_field_is_unknown_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            invalid = event(2, "invalid.json")
            invalid["input_tokens"] = True
            self.write(queue, "invalid.json", invalid)
            nested = event(2, "nested.json")
            nested["agent"] = {"untrusted": "value"}
            self.write(queue, "nested.json", nested)
            report = TOOL.audit_queue(queue)
            self.assertEqual(report["categories"]["unknown_schema"], ["invalid.json", "nested.json"])

    def test_v2_rejects_producer_contract_violations(self):
        cases = {}
        too_long_session = event(2, "long-session.json")
        too_long_session["session_id"] = "s" * 300
        cases["long-session.json"] = too_long_session
        too_long_hook = event(2, "long-hook.json")
        too_long_hook["hook_event_name"] = "h" * 65
        cases["long-hook.json"] = too_long_hook
        invalid_time = event(2, "bad-time.json")
        invalid_time["captured_at"] = "2026-08-10 00:00:00"
        cases["bad-time.json"] = invalid_time
        false_provenance = event(2, "false-provenance.json")
        false_provenance["field_provenance"] = {"input_tokens": "hook_payload.input_tokens"}
        cases["false-provenance.json"] = false_provenance
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            for filename, value in cases.items():
                self.write(queue, filename, value)
            report = TOOL.audit_queue(queue)
            self.assertEqual(report["categories"]["unknown_schema"], sorted(cases))

    def test_v2_accepts_bounded_values_and_exact_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            valid = event(2, "bounded.json")
            valid.update({
                "session_id": "s" * 256, "turn_id": "t" * 256,
                "hook_event_name": "h" * 64, "permission_mode": "p" * 64,
                "model": "m" * 256, "transcript_path": "x" * 4096,
                "cwd": "c" * 4096, "agent": "reviewer",
                "skills": ["verify-drydock-change"], "outcome": "completed",
                "test_status": "passed", "input_tokens": 0,
                "field_provenance": {
                    "agent": "hook_payload.agent", "skills": "hook_payload.skills",
                    "outcome": "hook_payload.outcome", "test_status": "hook_payload.test_status",
                    "input_tokens": "hook_payload.input_tokens",
                },
            })
            self.write(queue, "bounded.json", valid)
            self.assertEqual(TOOL.audit_queue(queue)["categories"]["valid_v2"], ["bounded.json"])

    def test_missing_transcript_is_checked_but_never_read_or_mutated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue = root / "pending"
            queue.mkdir()
            existing = root / "existing.txt"
            existing.write_text("do not read", encoding="utf-8")
            missing = root / "missing.txt"
            self.write(queue, "existing.json", event(1, "existing.json", str(existing)))
            self.write(queue, "missing.json", event(1, "missing.json", str(missing)))
            malicious = root / "mutation-target.txt"
            self.write(queue, "malicious.json", event(1, "malicious.json", str(malicious)))
            report = TOOL.audit_queue(queue)
            self.assertEqual(report["missing_transcript"], ["malicious.json", "missing.json"])
            self.assertFalse(malicious.exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "do not read")

    def test_corrupt_processed_state_isolated_from_event_report(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            queue = runtime / "pending"
            queue.mkdir()
            self.write(queue, "valid.json", event(1, "valid.json"))
            (runtime / "processed.json").write_text("{", encoding="utf-8")
            report = TOOL.audit_queue(queue)
            self.assertEqual(report["processed_state_error"], "JSONDecodeError")
            self.assertEqual(report["processing"]["unprocessed"], ["valid.json"])

    def test_invalid_processed_entry_rejects_entire_state(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            queue = runtime / "pending"
            queue.mkdir()
            self.write(queue, "valid.json", event(1, "valid.json"))
            self.write(runtime, "processed.json", {
                "schema_version": 1, "events": {"../escape.json": {"status": "processed", "processed_at": "now"}},
            })
            report = TOOL.audit_queue(queue)
            self.assertEqual(report["processed_state_error"], "invalid_schema")
            self.assertEqual(report["processing"]["unprocessed"], ["valid.json"])

            self.write(runtime, "processed.json", {
                "schema_version": 1,
                "events": {"valid.json": {"status": "processed", "processed_at": "not-a-time"}},
            })
            self.assertEqual(TOOL.audit_queue(queue)["processed_state_error"], "invalid_schema")

    def test_retention_candidates_are_report_only(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp)
            path = self.write(queue, "old.json", event(1, "old.json"))
            old = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp()
            os.utime(path, (old, old))
            before = path.read_bytes()
            report = TOOL.audit_queue(
                queue, older_than_days=5, larger_than_bytes=1,
                now=dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(report["retention_candidates"], {"age": ["old.json"], "size": ["old.json"]})
            self.assertEqual(path.read_bytes(), before)

    def test_cli_outputs_json_and_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = Path(temp) / "pending"
            queue.mkdir()
            self.write(queue, "valid.json", event(2, "valid.json"))
            completed = subprocess.run(
                [sys.executable, str(TOOL_PATH), "--queue-root", str(queue)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["counts"]["valid_v2"], 1)
            self.assertFalse((queue.parent / "processed.json").exists())


if __name__ == "__main__":
    unittest.main()
