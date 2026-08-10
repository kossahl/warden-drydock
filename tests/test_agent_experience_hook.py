import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


HOOK_PATH = Path(__file__).parents[1] / ".codex" / "hooks" / "capture_agent_experience.py"
SPEC = importlib.util.spec_from_file_location("capture_agent_experience", HOOK_PATH)
HOOK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(HOOK)


class AgentExperienceHookTests(unittest.TestCase):
    def test_capture_writes_minimal_deduplicated_event(self):
        payload = {
            "session_id": "thread/123",
            "turn_id": "turn:456",
            "hook_event_name": "Stop",
            "transcript_path": "C:/state/transcript.jsonl",
            "cwd": "D:/repo",
            "model": "example-model",
            "permission_mode": "default",
            "last_assistant_message": "must not be persisted",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = HOOK.capture(payload, root)
            second = HOOK.capture(payload, root)

            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            event = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(event["schema_version"], 2)
            self.assertEqual(event["session_id"], "thread/123")
            self.assertEqual(event["turn_id"], "turn:456")
            self.assertNotIn("last_assistant_message", event)
            self.assertEqual(event["skills"], [])
            self.assertEqual(event["field_provenance"], {})
            self.assertEqual(len(list(first.parent.glob("*.json"))), 1)

    def test_direct_optional_values_are_allowlisted_with_provenance(self):
        payload = {
            "session_id": "s", "turn_id": "t", "agent": "reviewer",
            "skills": ["verify-drydock-change"], "outcome": "completed",
            "test_status": "passed", "correction_count": 1,
            "input_tokens": 100, "output_tokens": 20, "files_changed_count": 2,
            "unknown": "ignored", "prompt": "secret", "last_assistant_message": "private",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = HOOK.capture(payload, Path(temp_dir))
            event = json.loads(path.read_text(encoding="utf-8"))
        for key in ("agent", "skills", "outcome", "test_status", "correction_count",
                    "input_tokens", "output_tokens", "files_changed_count"):
            self.assertEqual(event["field_provenance"][key], "hook_payload." + key)
        self.assertEqual(event["skills"], ["verify-drydock-change"])
        for forbidden in ("unknown", "prompt", "last_assistant_message"):
            self.assertNotIn(forbidden, event)

    def test_identifier_shaped_unknown_agent_and_skills_are_not_persisted(self):
        payload = {
            "session_id": "s", "turn_id": "t", "agent": "PROMPT_SECRET",
            "skills": ["API_SECRET"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            event = json.loads(HOOK.capture(payload, Path(temp_dir)).read_text(encoding="utf-8"))
        self.assertIsNone(event["agent"])
        self.assertEqual(event["skills"], [])
        serialized = json.dumps(event)
        self.assertNotIn("PROMPT_SECRET", serialized)
        self.assertNotIn("API_SECRET", serialized)

    def test_wrong_optional_types_use_stable_defaults(self):
        payload = {
            "session_id": "s", "turn_id": "t", "agent": 7, "skills": ["valid", 3],
            "outcome": [], "test_status": False, "correction_count": True,
            "input_tokens": -1, "output_tokens": 2.5, "files_changed_count": "3",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            event = json.loads(HOOK.capture(payload, Path(temp_dir)).read_text(encoding="utf-8"))
        self.assertIsNone(event["agent"])
        self.assertEqual(event["skills"], [])
        for key in ("outcome", "test_status", "correction_count", "input_tokens", "output_tokens", "files_changed_count"):
            self.assertIsNone(event[key])
        self.assertEqual(event["field_provenance"], {})

    def test_nested_legacy_values_and_sensitive_optional_text_are_rejected(self):
        payload = {
            "session_id": {"prompt": "hidden"}, "turn_id": ["secret"],
            "hook_event_name": {"last_assistant_message": "hidden"},
            "transcript_path": {"canon": "hidden"}, "cwd": ["private"],
            "model": {"secret": "hidden"}, "permission_mode": ["hidden"],
            "agent": "secret value with spaces", "skills": ["valid", "secret/value"],
            "outcome": "contains-secret", "test_status": "unknown",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            event = json.loads(HOOK.capture(payload, Path(temp_dir)).read_text(encoding="utf-8"))
        for key in ("session_id", "turn_id", "hook_event_name", "transcript_path", "cwd", "model", "permission_mode", "agent", "outcome", "test_status"):
            self.assertIsNone(event[key], key)
        self.assertEqual(event["skills"], [])
        self.assertNotIn("hidden", json.dumps(event))

    def test_field_cardinality_and_numeric_bounds(self):
        payload = {
            "session_id": "s" * HOOK.MAX_ID_LENGTH,
            "turn_id": "t", "model": "m" * (HOOK.MAX_LABEL_LENGTH + 1),
            "transcript_path": "p" * (HOOK.MAX_PATH_LENGTH + 1),
            "skills": ["skill"] * (HOOK.MAX_SKILLS + 1),
            "input_tokens": HOOK.MAX_COUNT, "output_tokens": HOOK.MAX_COUNT + 1,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            event = json.loads(HOOK.capture(payload, Path(temp_dir)).read_text(encoding="utf-8"))
        self.assertEqual(len(event["session_id"]), HOOK.MAX_ID_LENGTH)
        self.assertIsNone(event["model"])
        self.assertIsNone(event["transcript_path"])
        self.assertEqual(event["skills"], [])
        self.assertEqual(event["input_tokens"], HOOK.MAX_COUNT)
        self.assertIsNone(event["output_tokens"])

    def test_sanitized_identity_collision_fails_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            HOOK.capture({"session_id": "a/b", "turn_id": "t"}, root)
            with self.assertRaisesRegex(ValueError, "collides"):
                HOOK.capture({"session_id": "a?b", "turn_id": "t"}, root)

    def test_corrupt_file_or_directory_target_fails_safely(self):
        for as_directory in (False, True):
            with self.subTest(as_directory=as_directory), tempfile.TemporaryDirectory() as temp_dir:
                target = Path(temp_dir) / ".agent-experience" / "pending" / "s--t.json"
                target.parent.mkdir(parents=True)
                target.mkdir() if as_directory else target.write_text("not json", encoding="utf-8")
                with self.assertRaises((ValueError, OSError)):
                    HOOK.capture({"session_id": "s", "turn_id": "t"}, Path(temp_dir))

    def test_python_cli_rejects_malformed_and_oversized_input(self):
        for body in ("{", json.dumps({"padding": "x" * HOOK.MAX_INPUT_BYTES})):
            completed = subprocess.run(
                [sys.executable, str(HOOK_PATH)], input=body, text=True,
                capture_output=True, timeout=3, check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("agent experience capture failed", completed.stderr)

    def test_capture_runtime_is_bounded_in_practice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            started = time.monotonic()
            HOOK.capture({"session_id": "runtime", "turn_id": "case"}, Path(temp_dir))
            self.assertLess(time.monotonic() - started, 1.0)

    def test_special_and_long_ids_produce_bounded_filename(self):
        payload = {"session_id": "ä/" + "x" * 200, "turn_id": "?" + "y" * 200}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = HOOK.capture(payload, Path(temp_dir))
        parts = path.stem.split("--")
        self.assertEqual([len(part) for part in parts], [120, 120])

    @unittest.skipUnless(shutil.which("powershell.exe"), "PowerShell is unavailable")
    def test_powershell_emits_same_schema_and_structured_values(self):
        script = HOOK_PATH.with_suffix(".ps1")
        payload = {"session_id": "parity", "turn_id": "case", "agent": "PROMPT_SECRET", "skills": ["API_SECRET"], "outcome": "unknown", "input_tokens": 4, "output_tokens": -1}
        project_root = HOOK_PATH.parents[2]
        destination = project_root / ".agent-experience" / "pending" / "parity--case.json"
        if destination.exists():
            destination.unlink()
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                input=json.dumps(payload), text=True, capture_output=True, timeout=10, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            event = json.loads(destination.read_text(encoding="utf-8-sig"))
            with tempfile.TemporaryDirectory() as temp_dir:
                expected = HOOK.capture({**payload, "turn_id": "python-case"}, Path(temp_dir))
                python_event = json.loads(expected.read_text(encoding="utf-8"))
            for key in python_event.keys() - {"captured_at", "turn_id", "event_id"}:
                self.assertEqual(event[key], python_event[key], key)
        finally:
            if destination.exists():
                destination.unlink()


if __name__ == "__main__":
    unittest.main()
